import os
import time
import requests


def find_chat(token, timeout_seconds=90):
    configured = os.getenv("TELEGRAM_CHAT_ID")
    if configured:
        return configured

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        data = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            timeout=30,
        )
        data.raise_for_status()
        for update in reversed(data.json().get("result", [])):
            message = update.get("message") or update.get("channel_post")
            if message and message.get("chat", {}).get("id") is not None:
                return str(message["chat"]["id"])
        print("Waiting for Telegram /start message...")
        time.sleep(5)
    return None


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = find_chat(token)
    if not chat_id:
        print("Telegram chat ID not found yet. Send /start to @Goa_IBIS_bot; price checking will continue without an alert until the chat is discovered.")
        return

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "✅ Goa Ibis Tracker Telegram connection is working.\n\nBot: @Goa_IBIS_bot\nTracker: ibis Styles Goa Calangute\nStay: 28 Nov → 2 Dec 2026",
        },
        timeout=30,
    )
    r.raise_for_status()
    print("Telegram test message sent successfully.")


if __name__ == "__main__":
    main()
