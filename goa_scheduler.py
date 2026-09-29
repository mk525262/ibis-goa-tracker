import json
import os
import time
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

# Six daily updates between 9 AM and 9 PM IST.
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
            time.sleep(0.8)


def slot_key(date, hour, minute):
    return f"{date.isoformat()}-{hour:02d}{minute:02d}"


def due_regular_slots(now_ist, history):
    """
    Return every daily slot that is already due today and has not been sent.
    This intentionally does NOT use a 10-minute window: GitHub scheduled
    workflows can be delayed, so a missed slot must be caught up on the next run.
    """
    sent = {
        x.get("regular_slot")
        for x in history
        if isinstance(x, dict) and x.get("regular_sent") and x.get("regular_slot")
    }

    due = []
    for hour, minute in REGULAR_TIMES:
        key = slot_key(now_ist.date(), hour, minute)
        slot_time = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now_ist >= slot_time and key not in sent:
            due.append((key, hour, minute))

    return due


def current_message(total, change, scheduled_for):
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
        f"Change: {change_line}\n"
        f"Daily update: {scheduled_for}\n"
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
            json.dumps(history[-200:], indent=2), encoding="utf-8"
        )
        return

    prices = [
        x["total"]
        for x in history
        if isinstance(x, dict) and isinstance(x.get("total"), (int, float))
    ]
    previous = prices[-1] if prices else BASELINE_TOTAL
    change = total - previous

    record = {
        "checked_at": checked,
        "status": "ok",
        "total": total,
        "currency": CURRENCY,
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

    # Send every daily slot that is due but has not yet been sent.
    # This makes the six-message schedule resilient to delayed/missed
    # GitHub Actions runs.
    due_slots = due_regular_slots(now_ist, history)
    for key, hour, minute in due_slots:
        scheduled_for = f"{hour:02d}:{minute:02d} IST"
        send_telegram(current_message(total, change, scheduled_for))
        record_for_slot = {
            "checked_at": checked,
            "notification": "daily",
            "regular_slot": key,
            "regular_sent": True,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        history.append(record_for_slot)

    HISTORY_FILE.write_text(
        json.dumps(history[-200:], indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
