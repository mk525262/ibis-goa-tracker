import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

HOTEL = "ibis Styles Goa Calangute"
CHECKIN = "2026-11-28"
CHECKOUT = "2026-12-03"
GUESTS = 2
BASELINE_TOTAL = 40698.00
CURRENCY = "INR"
ROOM_LABEL = "Standard Twin Room – Pool View"
RATE_LABEL = "Flexible Rate – Half Board"
HISTORY_FILE = Path("data/price_history.json")
DAILY_UPDATE_HOURS = {9, 11, 13, 15, 18, 21}
BOOKING_URL = (
    "https://all.accor.com/booking/en/accor/hotel/8562"
    f"?dateIn={CHECKIN}&dateOut={CHECKOUT}&nights=5&compositions=2"
    "&stayplus=false&snu=false&accessibleRooms=false&hideWDR=false"
    "&hideHotelDetails=false&currency=INR&countryMarket=IN"
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
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    r.raise_for_status()


def send_drop_alert(text):
    for i in range(5):
        send_telegram(text)
        if i < 4:
            time.sleep(0.8)


def parse_money(text):
    out = []
    for m in re.finditer(r"(?:₹|INR)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text, re.I):
        try:
            out.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return out


def norm(s):
    return re.sub(r"\s+", " ", str(s).replace("–", "-").replace("—", "-").lower()).strip()


def extract_target_from_text(text):
    """
    Extract the FINAL PAYABLE INR amount for the exact target room/rate.
    Accor's booking page shows room price, taxes, and then a final
    'To be paid at the hotel' amount. We must use that final amount,
    not the room-only amount or an intermediate API price.
    """
    n = norm(text)
    if "flexible rate" not in n or "half board" not in n:
        return None, "target room/rate not found"

    room = norm(ROOM_LABEL)
    if room not in n:
        return None, "target room/rate not found"

    # Most reliable: the final payable amount shown by the booking page.
    patterns = [
        r"to be paid at the hotel.{0,300}?(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
        r"to be paid at hotel.{0,300}?(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
        r"fees and taxes included.{0,300}?(?:₹|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, n, re.I)
        if m:
            return float(m.group(1).replace(",", "")), "ok"

    # Fallback for pages where the final-payment label is rendered separately:
    # use the last INR amount after the exact target rate section.
    rate_pos = n.find("flexible rate - half board")
    if rate_pos >= 0:
        vals = [v for v in parse_money(n[rate_pos:]) if 1000 <= v <= 200000]
        if vals:
            return vals[-1], "ok"

    return None, "final payable price not found"


def exact_offer_from_json(payload):
    """
    Extract the exact Standard Twin + Flexible Rate + Half Board member price
    from Accor's INR payload. Accor exposes the member room amount separately
    from taxes, so return room amount + the displayed tax amount.
    """
    matches = []

    def walk(node, path=""):
        if isinstance(node, dict):
            rate = node.get("rate")
            meal = node.get("mealPlan")
            product = node.get("product")
            rl = norm(rate.get("label")) if isinstance(rate, dict) else ""
            mc = norm(meal.get("code")) if isinstance(meal, dict) else ""
            ml = norm(meal.get("label")) if isinstance(meal, dict) else ""
            product_id = product.get("id") if isinstance(product, dict) else ""

            if (
                "flexible rate" in rl
                and (mc == "half_board" or "half board" in ml)
                and product_id == "TWC"
            ):
                pricing = node.get("pricing") or {}
                currency = norm(pricing.get("currency"))
                main = pricing.get("main") or {}
                amount = main.get("amount")

                if currency == "inr" and isinstance(amount, (int, float)):
                    tax_text = str(pricing.get("formattedTaxType") or "")
                    tax_values = parse_money(tax_text)
                    tax = tax_values[0] if tax_values else 0.0
                    matches.append((float(amount) + tax, path))

            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(payload)
    if not matches:
        return None, "target room/rate not found"

    matches.sort(key=lambda x: x[0])
    return matches[0][0], "ok"

def fetch_live_rate():
    captured_json = []
    captured_text = []

    def capture_response(response):
        try:
            ctype = (response.headers.get("content-type") or "").lower()
            body = response.text()
            if "json" in ctype:
                captured_json.append(body)
            elif "text" in ctype and len(body) < 5_000_000:
                captured_text.append(body)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"})
        context.add_cookies([
            {"name": "userCurrency", "value": "INR", "domain": "all.accor.com", "path": "/"},
            {"name": "userLocalization", "value": "IN", "domain": "all.accor.com", "path": "/"},
            {"name": "userLang", "value": "en", "domain": "all.accor.com", "path": "/"},
        ])
        page = context.new_page()
        page.on("response", capture_response)
        try:
            page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(22000)
            for _ in range(10):
                page.wait_for_timeout(1000)

            texts = []
            try:
                texts.append(page.locator("body").inner_text(timeout=30000))
            except Exception:
                pass

            for frame in page.frames:
                try:
                    v = frame.locator("body").inner_text(timeout=5000)
                    if v:
                        texts.append(v)
                except Exception:
                    pass

            # IMPORTANT: rendered page first, because it contains the actual
            # final payable INR amount including taxes.
            for text in texts + captured_text:
                total, status = extract_target_from_text(text)
                if total is not None:
                    return total, status

            # API JSON is only a fallback.
            for body in captured_json:
                try:
                    total, status = exact_offer_from_json(json.loads(body))
                    if total is not None:
                        return total, status
                except Exception:
                    pass

            return None, "target room/rate not found"
        finally:
            context.close()
            browser.close()


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
    total, status = fetch_live_rate()
    print(f"checked_at={checked} status={status} total={total}")

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

    history.append(
        {"checked_at": checked, "status": "ok", "total": total, "currency": CURRENCY}
    )

    if change < 0:
        send_drop_alert(
            "🚨 IBIS GOA PRICE DROP 🚨\n\n"
            f"💰 FINAL PAYABLE PRICE: ₹{total:,.2f}\n"
            f"📉 PRICE DROPPED BY: ₹{abs(change):,.2f}\n"
            f"Previous: ₹{previous:,.2f}\n\n"
            f"{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\n"
            f"Room: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\n"
            "Source: ALL Accor official booking page"
        )

    now = datetime.now().astimezone()
    slot = now.strftime("%Y-%m-%d-%H")
    already = any(
        isinstance(x, dict)
        and x.get("notification") == "daily"
        and x.get("slot") == slot
        for x in history[-30:]
    )

    if now.hour in DAILY_UPDATE_HOURS and now.minute < 10 and not already:
        direction = "↓" if change < 0 else "↑" if change > 0 else "="
        send_telegram(
            "🏨 IBIS GOA CURRENT PRICE\n\n"
            f"💰 FINAL PAYABLE PRICE: ₹{total:,.2f}\n"
            f"Change: {direction} ₹{abs(change):,.2f}\n"
            f"Baseline: ₹{BASELINE_TOTAL:,.2f}\n\n"
            f"{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\n"
            f"Room: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\n"
            "Source: ALL Accor official booking page"
        )
        history.append({"notification": "daily", "slot": slot, "sent_at": checked})

    HISTORY_FILE.write_text(
        json.dumps(history[-100:], indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
