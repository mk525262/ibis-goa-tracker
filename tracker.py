import json
import os
import re
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
        window = ntext[max(0, room_pos - 2500): room_pos + 12000]
        has_pool = any(x in window for x in ("pool view", "pool-side", "pool side", "poolview"))
        if not has_pool:
            continue
        if "half board" not in window:
            continue
        if "flexible rate" not in window:
            continue

        total_patterns = [
            r"total[^₹0-9]{0,100}(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
            r"(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)[^\n]{0,80}total",
            r"stay[^₹0-9]{0,100}(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
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


def fetch_live_rate():
    """Read Accor's booking engine, including its dynamically loaded API data.
    Fail closed unless the exact target room/rate has a pool-view signal."""
    captured = []

    def capture_response(response):
        try:
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" in ctype or "text" in ctype:
                body = response.text()
                if any(k in body.lower() for k in ("half board", "standard twin", "pool view", "flexible rate")):
                    captured.append(body)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
        page = context.new_page()
        page.on("response", capture_response)
        try:
            page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(18000)

            for _ in range(8):
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(1200)

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

            for text in texts + captured:
                total, status = extract_target_from_text(text)
                if total is not None:
                    return total, status

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
    HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2), encoding="utf-8")

    # Send the current price on every successful check, not only when it changes.
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


if __name__ == "__main__":
    main()
