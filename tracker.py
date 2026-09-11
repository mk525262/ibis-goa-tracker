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
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)
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
    markers = ["standard twin room with pool view", "standard twin room - pool view", "standard twin room, pool view", "standard twin room"]
    for marker in markers:
        start = 0
        while True:
            room_pos = ntext.find(marker, start)
            if room_pos < 0:
                break
            start = room_pos + len(marker)
            # Accor can place the room, board and price in very large serialized sections.
            window = ntext[max(0, room_pos - 100000): room_pos + 100000]
            has_pool = any(x in window for x in ("pool view", "pool-side", "pool side", "poolview"))
            has_half = any(x in window for x in ("half board", "half-board", "halfboard"))
            has_flexible = any(x in window for x in ("flexible rate", "flexiblerate")) or ("flexible" in window and "rate" in window)
            if not (has_pool and has_half and has_flexible):
                continue
            patterns = [
                r"(?:total|grand total|stay total|total amount|totalamount|stay amount|stayamount)[^0-9]{0,300}(?:₹|inr)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
                r"(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)[^\n]{0,300}(?:total|grand total)",
            ]
            candidates = []
            for pattern in patterns:
                for raw in re.findall(pattern, window, re.I):
                    value = float(raw.replace(",", ""))
                    if 1000 <= value <= 200000:
                        candidates.append(value)
            if candidates:
                # Prefer the value closest to the known target baseline when several total-like values exist.
                return min(candidates, key=lambda x: abs(x - BASELINE_TOTAL)), "ok"
            amounts = [x for x in parse_inr(window) if 1000 <= x <= 200000]
            if amounts:
                return min(amounts, key=lambda x: abs(x - BASELINE_TOTAL)), "ok"
    return None, "target room/rate not found"


def _node_text(node):
    if isinstance(node, dict):
        parts = []
        for k, v in node.items():
            parts.append(str(k))
            if isinstance(v, str):
                parts.append(v)
        return normalized(" ".join(parts))
    if isinstance(node, list):
        return normalized(" ".join(_node_text(x) for x in node[:30]))
    return normalized(str(node))


def _number_values(node):
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((str(k), float(v)))
            elif isinstance(v, str):
                for x in parse_inr(v):
                    out.append((str(k), x))
            elif isinstance(v, (dict, list)):
                out.extend(_number_values(v))
    elif isinstance(node, list):
        for x in node:
            out.extend(_number_values(x))
    return out


def extract_target_from_json(payload):
    """Score JSON subtrees by the exact room/view/board/rate labels and choose a stay-total value."""
    scored = []

    def walk(node, path=""):
        if isinstance(node, dict):
            direct = _node_text(node)
            score = 0
            if "standard twin room" in direct or "twin room" in direct: score += 5
            if "pool view" in direct or "pool-side" in direct or "pool side" in direct: score += 5
            if "half board" in direct or "half-board" in direct or "halfboard" in direct: score += 5
            if "flexible rate" in direct or "flexiblerate" in direct: score += 5
            nums = _number_values(node)
            if score >= 15 and nums:
                for key, value in nums:
                    if 1000 <= value <= 200000:
                        keyn = normalized(key)
                        bonus = 20 if any(x in keyn for x in ("total", "grand", "stayamount", "totalamount", "price")) else 0
                        scored.append((score + bonus, abs(value - BASELINE_TOTAL), value, path, key))
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(payload)
    if scored:
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[0][2], "ok"
    return None, "target room/rate not found"


def fetch_live_rate():
    captured_json, captured_text, response_urls = [], [], []

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
            for _ in range(10): page.wait_for_timeout(1000)

            texts = []
            try: texts.append(page.locator("body").inner_text(timeout=30000))
            except Exception: pass
            for frame in page.frames:
                try:
                    value = frame.locator("body").inner_text(timeout=5000)
                    if value: texts.append(value)
                except Exception: pass

            for body in captured_json:
                try:
                    total, status = extract_target_from_json(json.loads(body))
                    if total is not None: return total, status
                except Exception: pass
            for text in texts + captured_text + captured_json:
                total, status = extract_target_from_text(text)
                if total is not None: return total, status

            # Print compact, non-secret snippets around each target marker so the next parser revision can be exact.
            print(f"diagnostic_json_responses={len(captured_json)} diagnostic_text_responses={len(captured_text)}")
            for idx, body in enumerate(captured_json):
                low = body.lower()
                hits = []
                for term in ("standard twin", "pool view", "half board", "flexible"):
                    pos = low.find(term)
                    if pos >= 0:
                        snippet = re.sub(r"\s+", " ", body[max(0, pos-700):pos+2200])
                        hits.append(f"TERM={term} SNIPPET={snippet[:2900]}")
                if hits:
                    print(f"DIAG_JSON_{idx} " + " || ".join(hits)[:12000])
            return None, "target room/rate not found"
        finally:
            context.close()
            browser.close()


def main():
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try: history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception: history = []
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
    history.append({"checked_at": checked, "status": "ok", "total": total, "currency": CURRENCY})
    if total < previous:
        drop_text = ("🚨 IBIS GOA PRICE DROP\n\n" f"{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\nRoom: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\n" f"💰 NEW PRICE: ₹{total:,.2f}\n📉 DROPPED BY: ₹{abs(change):,.2f}\nPrevious: ₹{previous:,.2f}\n\nSource: ALL Accor official booking page")
        send_drop_alert(drop_text)
    now_ist = datetime.now().astimezone()
    slot_key = now_ist.strftime("%Y-%m-%d-%H")
    already_sent = any(isinstance(x, dict) and x.get("notification") == "daily" and x.get("slot") == slot_key for x in history[-30:])
    if now_ist.hour in DAILY_UPDATE_HOURS and now_ist.minute < 10 and not already_sent:
        direction = "↓" if change < 0 else "↑" if change > 0 else "="
        send_telegram("🏨 IBIS GOA CURRENT PRICE\n\n" f"{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\nRoom: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\n" f"💰 Current price: ₹{total:,.2f}\nChange: {direction} ₹{abs(change):,.2f}\nBaseline: ₹{BASELINE_TOTAL:,.2f}\n\nSource: ALL Accor official booking page")
        history.append({"notification": "daily", "slot": slot_key, "sent_at": checked})
    HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2), encoding="utf-8")

if __name__ == "__main__": main()
