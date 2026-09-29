import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from tracker import (
    BASELINE_TOTAL,
    CHECKIN,
    CHECKOUT,
    CURRENCY,
    GUESTS,
    HISTORY_FILE,
    HOTEL,
    RATE_LABEL,
    ROOM_LABEL,
    fetch_live_rate,
    telegram_chat_id,
)

IST = ZoneInfo("Asia/Kolkata")

# Six daily price updates, spread across 9 AM to 9 PM IST.
REGULAR_TIMES = [
    (9, 0),
    (11, 25),
    (13, 50),
    (16, 15),
    (18, 40),
    (21, 0),
]

PRICE_ALERT_THRESHOLD = 35000


def send_telegram(text):
    import os

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = telegram_chat_id(token)
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    r.raise_for_status()


def send_repeated(text, count):
    for i in range(count):
        send_telegram(text)
        if i < count - 1:
            # Small pause keeps the messages back-to-back without hammering
            # Telegram's API as one instantaneous burst.
            import time

            time.sleep(0.8)


def regular_slot_key(now):
    now_minutes = now.hour * 60 + now.minute
    for hour, minute in REGULAR_TIMES:
        slot_minutes = hour * 60 + minute
        if slot_minutes <= now_minutes < slot_minutes + 10:
            return f"{now.date().isoformat()}-{hour:02d}{minute:02d}"
    return None


def current_message(total, change):
    if change < 0:
        change_line = f"Change: ↓ ₹{abs(change):,.2f}"
    elif change > 0:
        change_line = f"Change: ↑ ₹{abs(change):,.2f}"
    else:
        change_line = "Change: = ₹0.00"

    return (
        "🏨 IBIS GOA CURRENT PRICE\n\n"
        f"💰 FINAL PAYABLE PRICE: ₹{total:,.2f}\n\n"
        f"{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\n"
        f"Room: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\n"
        f"{change_line}\n"
        f"Baseline: ₹{BASELINE_TOTAL:,.2f}\n\n"
        "Source: ALL Accor official booking page"
    )


def price_change_message(total, previous, change):
    direction = "↓" if change < 0 else "↑"
    return (
        "🔔 IBIS GOA PRICE CHANGE 🔔\n\n"
        f"💰 FINAL PAYABLE PRICE: ₹{total:,.2f}\n"
        f"📊 Change: {direction} ₹{abs(change):,.2f}\n"
        f"Previous: ₹{previous:,.2f}\n\n"
        f"{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\n"
        f"Room: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\n"
        "Source: ALL Accor official booking page"
    )


def threshold_message(total, previous):
    return (
        "🚨🚨 IBIS GOA PRICE ALERT 🚨🚨\n\n"
        f"💰 FINAL PAYABLE PRICE: ₹{total:,.2f}\n"
        f"🔥 ₹35,000 OR BELOW!\n"
        f"Previous: ₹{previous:,.2f}\n\n"
        f"{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\n"
        f"Room: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\n"
        "Source: ALL Accor official booking page"
    )


def main():
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = (
            json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if HISTORY_FILE.exists()
            else []
        )
    except Exception:
        history = []

    checked = datetime.now(timezone.utc).isoformat()
    now_ist = datetime.now(IST)
    total, status = fetch_live_rate()
    print(
        f"checked_at={checked} ist={now_ist.isoformat()} "
        f"status={status} total={total}"
    )

    if total is None:
        history.append({"checked_at": checked, "status": status})
        HISTORY_FILE.write_text(
            json.dumps(history[-100:], indent=2), encoding="utf-8"
        )
        return

    prices = [
        x["total"]
        for x in history
        if isinstance(x, dict) and isinstance(x.get("total"), (int, float))
    ]
    previous = prices[-1] if prices else BASELINE_TOTAL
    change = total - previous
    slot = regular_slot_key(now_ist)

    record = {
        "checked_at": checked,
        "status": "ok",
        "total": total,
        "currency": CURRENCY,
        "regular_slot": slot,
        "regular_sent": False,
        "change_alert_sent": False,
        "threshold_alert_sent": False,
    }
    history.append(record)

    # Any genuine price change gets 5 immediate Telegram messages.
    # If the price crosses from above ₹35,000 to ₹35,000 or below,
    # send 20 messages instead of 5.
    if change != 0:
        crossed_threshold = (
            previous > PRICE_ALERT_THRESHOLD
            and total <= PRICE_ALERT_THRESHOLD
        )

        if crossed_threshold:
            send_repeated(threshold_message(total, previous), 20)
            record["threshold_alert_sent"] = True
        else:
            send_repeated(price_change_message(total, previous, change), 5)
            record["change_alert_sent"] = True

    # One regular current-price message in each of the six daily slots.
    if slot:
        already_sent = any(
            isinstance(x, dict)
            and x.get("regular_slot") == slot
            and x.get("regular_sent")
            for x in history[:-1]
        )
        if not already_sent:
            send_telegram(current_message(total, change))
            record["regular_sent"] = True

    HISTORY_FILE.write_text(
        json.dumps(history[-100:], indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
