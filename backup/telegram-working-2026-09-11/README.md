# Telegram Working Backup — 2026-09-11

This folder preserves the known-working Telegram delivery configuration for the Goa Accor tracker.

## Preserved behavior
- Telegram delivery via `TELEGRAM_BOT_TOKEN` and optional `TELEGRAM_CHAT_ID` GitHub secrets.
- Chat ID can be auto-discovered through Telegram `getUpdates` after `/start` is sent to the bot.
- Price check workflow runs every 5 minutes (Asia/Kolkata).
- Regular current-price messages are sent only at 09:00, 11:00, 13:00, 15:00, 18:00 and 21:00 IST.
- A newly verified price drop triggers 5 back-to-back Telegram alerts.
- Same unchanged lower price does not repeatedly trigger the 5-alert sequence.
- Target: ibis Styles Goa Calangute, 28 Nov–2 Dec 2026, 2 guests, Standard Twin Room – Pool View, Flexible Rate – Half Board.
- Baseline total: INR 37,605.75.

## Important
Do not store or copy the Telegram bot token into this backup. The secret remains in GitHub Actions Secrets.

The backup files are:
- `goa_scheduler.py`
- `goa-tracker.yml`
- `tracker.py`

This backup was created so the working Telegram setup can be restored if later WhatsApp or other changes cause a problem.
