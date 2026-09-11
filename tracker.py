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
    "https://all.accor.com/booking/en/accor/hotel/C8562"
    f"?dateIn={CHECKIN}&nights=4&compositions=2&stayplus=false&snu=false&hideHotelDetails=true"
)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)
    r.raise_for_status()


def parse_inr(text):
    values = []
    for match in re.finditer(r"(?:₹|INR)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text, re.I):
        try:
            values.append(float(match.group(1).replace(",", "")))
        except ValueError:
            pass
    return values


def fetch_live_rate():
    """Read the official Accor booking page and fail closed if the exact
    target room/rate cannot be identified. This prevents false alerts."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="en-IN", timezone_id="Asia/Kolkata")
        try:
            page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(12000)
            text = page.locator("body").inner_text(timeout=30000)
            lower = text.lower()
            room_pos = lower.find("standard twin room with pool view")
            if room_pos < 0:
                room_pos = lower.find("standard twin room")
            if room_pos < 0:
                return None, "target room not found"
            window = text[max(0, room_pos - 1000): room_pos + 7000]
            wl = window.lower()
            if "half board" not in wl:
                return None, "half-board rate not found near target room"
            if "flexible rate" not in wl:
                return None, "flexible rate not found near target room"
            amounts = [x for x in parse_inr(window) if 1000 <= x <= 200000]
            if not amounts:
                return None, "no plausible INR total found"
            return max(amounts), "ok"
        finally:
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
    history.append({"checked_at": checked, "status": "ok", "total": total, "currency": CURRENCY})
    HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2), encoding="utf-8")

    if total != previous:
        change = total - previous
        direction = "↓" if change < 0 else "↑"
        send_telegram(
            "🏨 IBIS GOA PRICE ALERT\n\n"
            f"{HOTEL}\n"
            f"Stay: {CHECKIN} → {CHECKOUT}\n"
            f"Guests: {GUESTS}\n"
            f"Room: {ROOM_LABEL}\n"
            f"Rate: {RATE_LABEL}\n\n"
            f"Current total: ₹{total:,.2f}\n"
            f"Change: {direction} ₹{abs(change):,.2f}\n"
            f"Baseline: ₹{BASELINE_TOTAL:,.2f}\n\n"
            "Source: ALL Accor official booking page"
        )


if __name__ == "__main__":
    main()
