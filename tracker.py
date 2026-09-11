import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

HOTEL = "ibis Styles Goa Calangute"
CHECKIN = "2026-11-28"
CHECKOUT = "2026-12-02"
GUESTS = 2
BASELINE_TOTAL = 37605.75
CURRENCY = "INR"
ROOM_LABEL = "Standard Twin Room – Pool View"
RATE_LABEL = "Flexible Rate – Half Board"
HISTORY_FILE = Path("data/price_history.json")
DAILY_UPDATE_HOURS = {9, 11, 13, 15, 18, 21}
BOOKING_URL = (
    "https://all.accor.com/booking/en/accor/hotel/8562"
    f"?dateIn={CHECKIN}&dateOut={CHECKOUT}&nights=4&compositions=2"
    "&stayplus=false&snu=false&accessibleRooms=false&hideWDR=false"
    "&hideHotelDetails=false"
)


def telegram_chat_id(token):
    configured = os.getenv("TELEGRAM_CHAT_ID")
    if configured:
        return configured
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    r.raise_for_status()
    for update in reversed(r.json().get("result", [])):
        message = update.get("message") or update.get("channel_post")
        if message and message.get("chat", {}).get("id") is not None:
            return str(message["chat"]["id"])
    raise RuntimeError("No Telegram chat found. Open @Goa_IBIS_bot and send /start first.")


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = telegram_chat_id(token)
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=30,
    )
    r.raise_for_status()


def send_drop_alert(text):
    for i in range(5):
        send_telegram(text)
        if i < 4:
            time.sleep(0.8)


def parse_inr(text):
    values = []
    for match in re.finditer(r"(?:₹|INR)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text, re.I):
        try:
            values.append(float(match.group(1).replace(",", "")))
        except ValueError:
            pass
    return values


def normalized(s):
    return re.sub(r"\s+", " ", s.replace("–", "-").replace("—", "-").lower()).strip()


def extract_target_from_text(text):
    ntext = normalized(text)
    room_markers = [
        "standard twin room with pool view",
        "standard twin room - pool view",
        "standard twin room, pool view",
        "standard twin room with balcony",
        "standard twin room",
    ]
    positions = []
    for marker in room_markers:
        start = 0
        while True:
            pos = ntext.find(marker, start)
            if pos < 0:
                break
            positions.append((pos, marker))
            start = pos + len(marker)
    positions.sort()

    for room_pos, marker in positions:
        window = ntext[max(0, room_pos - 5000): room_pos + 30000]
        has_pool = any(x in window for x in ("pool view", "pool-side", "pool side", "poolview"))
        has_half_board = "half board" in window or "half-board" in window or "halfboard" in window
        has_flexible = "flexible rate" in window or "flexible" in window
        if not has_pool or not has_half_board or not has_flexible:
            continue
        total_patterns = [
            r"(?:total|grand total|stay total)[^₹0-9]{0,150}(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
            r"(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)[^\n]{0,120}(?:total|grand total)",
            r"(?:stay|4 nights?)[^₹0-9]{0,150}(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
        ]
        for pattern in total_patterns:
            matches = re.findall(pattern, window, re.I)
            for raw in reversed(matches):
                value = float(raw.replace(",", ""))
                if 1000 <= value <= 200000:
                    return value, "ok"
        amounts = [x for x in parse_inr(window) if 1000 <= x <= 200000]
        if amounts:
            return max(amounts), "ok"
    return None, "target room/rate not found"


def _flatten_strings_and_numbers(value):
    strings = []
    numbers = []
    if isinstance(value, dict):
        for key, child in value.items():
            strings.append(str(key))
            s, n = _flatten_strings_and_numbers(child)
            strings.extend(s)
            numbers.extend(n)
    elif isinstance(value, list):
        for child in value:
            s, n = _flatten_strings_and_numbers(child)
            strings.extend(s)
            numbers.extend(n)
    elif isinstance(value, str):
        strings.append(value)
        numbers.extend(parse_inr(value))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        numbers.append(float(value))
    return strings, numbers


def extract_target_from_json(payload):
    """Find the target room/rate in Accor's JSON even when labels and price fields are separate."""
    candidates = []

    def walk(node):
        if isinstance(node, dict):
            strings, numbers = _flatten_strings_and_numbers(node)
            blob = normalized(" ".join(strings))
            has_twin = "standard twin" in blob or "twin room" in blob
            has_pool = "pool view" in blob or "pool-side" in blob or "pool side" in blob or "poolview" in blob
            has_half = "half board" in blob or "half-board" in blob or "halfboard" in blob
            has_flexible = "flexible rate" in blob or ("flexible" in blob and "rate" in blob)
            if has_twin and has_pool and has_half and has_flexible:
                plausible = [x for x in numbers if 1000 <= x <= 200000]
                if plausible:
                    # Prefer fields whose key explicitly indicates a stay total.
                    for key, value in node.items():
                        k = normalized(str(key))
                        if any(term in k for term in ("total", "grandtotal", "totalamount", "stayamount")):
                            try:
                                v = float(value)
                                if 1000 <= v <= 200000:
                                    candidates.append((0, v))
                            except (TypeError, ValueError):
                                pass
                    candidates.extend((1, x) for x in plausible)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    if candidates:
        candidates.sort(key=lambda x: (x[0], -x[1]))
        return candidates[0][1], "ok"
    return None, "target room/rate not found"


def fetch_live_rate():
    captured_json = []
    captured_text = []
    response_urls = []

    def capture_response(response):
        try:
            ctype = (response.headers.get("content-type") or "").lower()
            url = response.url
            if "json" in ctype:
                body = response.text()
                captured_json.append(body)
                if any(k in body.lower() for k in ("half board", "standard twin", "pool view", "flexible")):
                    response_urls.append(url)
            elif "text" in ctype and any(k in url.lower() for k in ("booking", "availability", "rate", "room")):
                body = response.text()
                if len(body) < 5_000_000:
                    captured_text.append(body)
                    if any(k in body.lower() for k in ("half board", "standard twin", "pool view", "flexible")):
                        response_urls.append(url)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
        page = context.new_page()
        page.on("response", capture_response)
        try:
            page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(22000)

            # Give the Accor booking application time to finish its XHR calls.
            for _ in range(10):
                page.wait_for_timeout(1000)

            texts = []
            try:
                texts.append(page.locator("body").inner_text(timeout=30000))
            except Exception:
                pass
            for frame in page.frames:
                try:
                    value = frame.locator("body").inner_text(timeout=5000)
                    if value:
                        texts.append(value)
                except Exception:
                    pass

            for body in captured_json:
                try:
                    payload = json.loads(body)
                    total, status = extract_target_from_json(payload)
                    if total is not None:
                        return total, status
                except Exception:
                    pass

            for text in texts + captured_text + captured_json:
                total, status = extract_target_from_text(text)
                if total is not None:
                    return total, status

            # Useful diagnostic only; never exposes cookies/tokens.
            print(f"diagnostic_responses_with_target_terms={len(response_urls)}")
            for url in response_urls[:20]:
                print(f"diagnostic_response_url={url[:500]}")
            print(f"diagnostic_json_responses={len(captured_json)} diagnostic_text_responses={len(captured_text)}")
            return None, "target room/rate not found"
        finally:
            context.close()
            browser.close()


def main():
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception:
        history = []

    now_ist = datetime.now().astimezone()
    checked = datetime.now(timezone.utc).isoformat()
    total, status = fetch_live_rate()
    print(f"checked_at={checked} status={status} total={total}")

    if total is None:
        history.append({"checked_at": checked, "status": status})
        HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2), encoding="utf-8")
        return

    prices = [x["total"] for x in history if isinstance(x, dict) and isinstance(x.get("total"), (int, float))]
    previous = prices[-1] if prices else BASELINE_TOTAL
    change = total - previous
    direction = "↓" if change < 0 else "↑" if change > 0 else "="
    history.append({"checked_at": checked, "status": "ok", "total": total, "currency": CURRENCY})

    if total < previous:
        drop_text = (
            "🚨 IBIS GOA PRICE DROP\n\n"
            f"{HOTEL}\n"
            f"Stay: {CHECKIN} → {CHECKOUT}\n"
            f"Guests: {GUESTS}\n"
            f"Room: {ROOM_LABEL}\n"
            f"Rate: {RATE_LABEL}\n\n"
            f"💰 NEW PRICE: ₹{total:,.2f}\n"
            f"📉 DROPPED BY: ₹{abs(change):,.2f}\n"
            f"Previous: ₹{previous:,.2f}\n\n"
            "Source: ALL Accor official booking page"
        )
        send_drop_alert(drop_text)

    slot_key = now_ist.strftime("%Y-%m-%d-%H")
    already_sent = any(
        isinstance(x, dict) and x.get("notification") == "daily" and x.get("slot") == slot_key
        for x in history[-30:]
    )
    if now_ist.hour in DAILY_UPDATE_HOURS and now_ist.minute < 10 and not already_sent:
        send_telegram(
            "🏨 IBIS GOA CURRENT PRICE\n\n"
            f"{HOTEL}\n"
            f"Stay: {CHECKIN} → {CHECKOUT}\n"
            f"Guests: {GUESTS}\n"
            f"Room: {ROOM_LABEL}\n"
            f"Rate: {RATE_LABEL}\n\n"
            f"💰 Current price: ₹{total:,.2f}\n"
            f"Change: {direction} ₹{abs(change):,.2f}\n"
            f"Baseline: ₹{BASELINE_TOTAL:,.2f}\n\n"
            "Source: ALL Accor official booking page"
        )
        history.append({"notification": "daily", "slot": slot_key, "sent_at": checked})

    HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
