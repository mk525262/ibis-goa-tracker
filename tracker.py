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
# Accor's public hotel page uses hotel id 8562. Keep both check-in and
# check-out explicit so the booking engine cannot fall back to another stay.
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
    updates = r.json().get("result", [])
    for update in reversed(updates):
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


def fetch_live_rate():
    """Read the official Accor booking page and fail closed unless the exact
    target room and Half Board Flexible Rate can be identified."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
        page = context.new_page()
        try:
            page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=90000)
            # The Accor booking engine hydrates asynchronously. Give it time,
            # then scroll so lazy-loaded room cards are rendered.
            page.wait_for_timeout(15000)
            for _ in range(6):
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(1200)

            text = page.locator("body").inner_text(timeout=30000)
            ntext = normalized(text)

            room_variants = [
                "standard twin room with pool view",
                "standard twin room - pool view",
                "standard twin room, pool view",
                "standard twin room",
            ]
            room_pos = -1
            matched_room = None
            for variant in room_variants:
                pos = ntext.find(variant)
                if pos >= 0:
                    room_pos = pos
                    matched_room = variant
                    break

            if room_pos < 0:
                # As a final fallback, inspect rendered room-like elements;
                # Accor sometimes changes punctuation/labels between locales.
                candidates = page.locator("text=/Standard Twin Room/i")
                if candidates.count() > 0:
                    for i in range(min(candidates.count(), 10)):
                        try:
                            value = candidates.nth(i).inner_text().strip()
                            if "twin" in value.lower():
                                room_pos = ntext.find(normalized(value))
                                matched_room = normalized(value)
                                break
                        except Exception:
                            pass

            if room_pos < 0:
                return None, "target room not found"

            window = ntext[max(0, room_pos - 1500): room_pos + 9000]
            if "half board" not in window:
                return None, "half-board rate not found near target room"
            if "flexible rate" not in window:
                return None, "flexible rate not found near target room"

            amounts = [x for x in parse_inr(window) if 1000 <= x <= 200000]
            if not amounts:
                return None, "no plausible INR total found"

            # Prefer a displayed stay total if the page exposes one. Otherwise
            # use the largest plausible amount in the exact room/rate window.
            total_patterns = [
                r"total[^₹0-9]{0,80}(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
                r"(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)[^\n]{0,50}total",
            ]
            for pattern in total_patterns:
                matches = re.findall(pattern, window, re.I)
                for raw in reversed(matches):
                    value = float(raw.replace(",", ""))
                    if 1000 <= value <= 200000:
                        return value, "ok"

            return max(amounts), "ok"
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
