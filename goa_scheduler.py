import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from tracker import BASELINE_TOTAL, CHECKIN, CHECKOUT, CURRENCY, GUESTS, HISTORY_FILE, HOTEL, RATE_LABEL, ROOM_LABEL, fetch_live_rate, telegram_chat_id

IST = ZoneInfo("Asia/Kolkata")
# Six rounded daily updates: 9 AM, 11 AM, 1 PM, 3 PM, 6 PM, 9 PM IST.
REGULAR_TIMES = [(9, 0), (11, 0), (13, 0), (15, 0), (18, 0), (21, 0)]


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
        f"Baseline: ₹{BASELINE_TOTAL:,.2f}\n\nSource: ALL Accor official booking page"
    )


def main():
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception:
        history = []

    checked = datetime.now(timezone.utc).isoformat()
    now_ist = datetime.now(IST)
    total, status = fetch_live_rate()
    print(f"checked_at={checked} ist={now_ist.isoformat()} status={status} total={total}")

    if total is None:
        history.append({"checked_at": checked, "status": status})
        HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2), encoding="utf-8")
        return

    prices = [x["total"] for x in history if isinstance(x, dict) and isinstance(x.get("total"), (int, float))]
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
    }
    history.append(record)
    HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2), encoding="utf-8")

    # A verified price drop triggers five Telegram messages back-to-back immediately.
    if change < 0:
        alert = (
            "🚨 IBIS GOA PRICE DROP 🚨\n\n"
            f"💰 FINAL PAYABLE PRICE: ₹{total:,.2f}\n"
            f"📉 PRICE DROPPED BY: ₹{abs(change):,.2f}\n"
            f"Previous: ₹{previous:,.2f}\n\n"
            f"{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\n"
            f"Room: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\nSource: ALL Accor official booking page"
        )
        for _ in range(5):
            send_telegram(alert)

    # One regular current-price message in each daily slot.
    if slot:
        already_sent = any(
            isinstance(x, dict) and x.get("regular_slot") == slot and x.get("regular_sent")
            for x in history[:-1]
        )
        if not already_sent:
            send_telegram(current_message(total, change))
            record["regular_sent"] = True
            HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
