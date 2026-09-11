import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

HOTEL = "ibis Styles Goa Calangute"
CHECKIN = "2026-11-28"
CHECKOUT = "2026-12-02"
GUESTS = 2
ROOM = "Standard Twin Room – Pool View"
RATE = "Flexible Rate – Half Board"
BASELINE_TOTAL = 37605.75

DATA_FILE = Path("data/price_history.json")
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_history():
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(history):
    DATA_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def send_telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=30)
    r.raise_for_status()


def fetch_rate():
    """Placeholder for the live Accor rate adapter.

    The tracker deliberately does not fabricate a price. Set LIVE_TOTAL through
    the workflow only after a real booking-page adapter has supplied the exact
    room/rate combination.
    """
    value = os.getenv("LIVE_TOTAL")
    if not value:
        return None
    return round(float(value), 2)


def main():
    history = load_history()
    live_total = fetch_rate()
    now = datetime.now(timezone.utc).isoformat()

    if live_total is None:
        print("No live rate supplied; no alert sent.")
        return

    previous = history[-1]["total"] if history else None
    record = {"checked_at": now, "total": live_total, "currency": "INR"}
    history.append(record)
    save_history(history)

    if previous is None or live_total != previous:
        delta = live_total - (previous if previous is not None else BASELINE_TOTAL)
        direction = "decreased" if delta < 0 else "increased"
        msg = (
            f"🏨 Goa Ibis Rate Alert\n\n"
            f"{HOTEL}\n"
            f"Stay: {CHECKIN} → {CHECKOUT}\n"
            f"Guests: {GUESTS}\n"
            f"Room: {ROOM}\n"
            f"Rate: {RATE}\n\n"
            f"Current total: ₹{live_total:,.2f}\n"
            f"Change: ₹{abs(delta):,.2f} {direction}\n"
            f"Baseline: ₹{BASELINE_TOTAL:,.2f}"
        )
        send_telegram(msg)


if __name__ == "__main__":
    main()
