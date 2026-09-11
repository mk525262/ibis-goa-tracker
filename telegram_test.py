import os
import requests


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        data = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
        for update in reversed(data.get("result", [])):
            message = update.get("message") or update.get("channel_post")
            if message and message.get("chat", {}).get("id") is not None:
                chat_id = str(message["chat"]["id"])
                break
    if not chat_id:
        raise SystemExit("No chat found. Open @Goa_IBIS_bot and send /start, then run this workflow again.")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": "✅ Goa Ibis Tracker Telegram connection is working.\n\nBot: @Goa_IBIS_bot\nTracker: ibis Styles Goa Calangute\nStay: 28 Nov → 2 Dec 2026"},
        timeout=30,
    )
    r.raise_for_status()
    print("Telegram test message sent successfully.")


if __name__ == "__main__":
    main()
