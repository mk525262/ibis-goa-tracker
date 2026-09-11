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
CHECKOUT = "2026-12-02"
GUESTS = 2
BASELINE_TOTAL = 37605.75
CURRENCY = "INR"
ROOM_LABEL = "Standard Twin Room – Pool View"
RATE_LABEL = "Flexible Rate – Half Board"
HISTORY_FILE = Path("data/price_history.json")
DAILY_UPDATE_HOURS = {9, 11, 13, 15, 18, 21}
BASELINE_EUR = 357.72
BASELINE_EUR_TO_INR = BASELINE_TOTAL / BASELINE_EUR
BOOKING_URL = (
    "https://all.accor.com/booking/en/accor/hotel/8562"
    f"?dateIn={CHECKIN}&dateOut={CHECKOUT}&nights=4&compositions=2"
    "&stayplus=false&snu=false&accessibleRooms=false&hideWDR=false"
    "&hideHotelDetails=false&currency=INR"
)


def telegram_chat_id(token):
    configured = os.getenv("TELEGRAM_CHAT_ID")
    if configured: return configured
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30); r.raise_for_status()
    for update in reversed(r.json().get("result", [])):
        message = update.get("message") or update.get("channel_post")
        if message and message.get("chat", {}).get("id") is not None: return str(message["chat"]["id"])
    raise RuntimeError("No Telegram chat found. Open @Goa_IBIS_bot and send /start first.")


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]; chat_id = telegram_chat_id(token)
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":chat_id,"text":text}, timeout=30); r.raise_for_status()


def send_drop_alert(text):
    for i in range(5):
        send_telegram(text)
        if i < 4: time.sleep(0.8)


def parse_money(text):
    out=[]
    for m in re.finditer(r"(?:₹|INR)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text, re.I):
        try: out.append(float(m.group(1).replace(",","")))
        except ValueError: pass
    return out


def norm(s): return re.sub(r"\s+"," ",str(s).replace("–","-").replace("—","-").lower()).strip()


def exact_offer_from_json(payload):
    """Find flexible + half-board offers and choose the one matching the known target commercial price."""
    matches=[]
    def walk(node, path=""):
        if isinstance(node, dict):
            rate=node.get("rate"); meal=node.get("mealPlan")
            rl=norm(rate.get("label")) if isinstance(rate,dict) else ""
            mc=norm(meal.get("code")) if isinstance(meal,dict) else ""
            ml=norm(meal.get("label")) if isinstance(meal,dict) else ""
            if "flexible rate" in rl and (mc=="half_board" or "half board" in ml):
                pricing=node.get("pricing") or {}
                currency=norm(pricing.get("currency"))
                main=pricing.get("main") or {}; alt=pricing.get("alternative") or {}
                for obj,kind in ((alt,"alternative"),(main,"main")):
                    if isinstance(obj,dict) and isinstance(obj.get("amount"),(int,float)):
                        amount=float(obj["amount"])
                        if currency=="inr": converted=amount
                        elif currency in ("eur","€"): converted=round(amount*BASELINE_EUR_TO_INR,2)
                        else: continue
                        matches.append((abs(converted-BASELINE_TOTAL),converted,amount,currency,kind,path))
            for k,v in node.items(): walk(v,f"{path}.{k}" if path else str(k))
        elif isinstance(node,list):
            for i,v in enumerate(node): walk(v,f"{path}[{i}]")
    walk(payload)
    if not matches: return None,"target room/rate not found"
    matches.sort(key=lambda x:x[0])
    return matches[0][1],"ok"


def extract_target_from_text(text):
    n=norm(text)
    if "flexible rate" not in n or "half board" not in n: return None,"target room/rate not found"
    vals=[v for v in parse_money(text) if 1000<=v<=200000]
    return (min(vals,key=lambda x:abs(x-BASELINE_TOTAL)),"ok") if vals else (None,"target room/rate not found")


def fetch_live_rate():
    captured_json=[]; captured_text=[]
    def capture_response(response):
        try:
            ctype=(response.headers.get("content-type") or "").lower(); body=response.text()
            if "json" in ctype: captured_json.append(body)
            elif "text" in ctype and len(body)<5_000_000: captured_text.append(body)
        except Exception: pass
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(locale="en-IN",timezone_id="Asia/Kolkata")
        page=context.new_page(); page.on("response",capture_response)
        try:
            page.goto(BOOKING_URL,wait_until="domcontentloaded",timeout=90000)
            page.wait_for_timeout(22000)
            for _ in range(10): page.wait_for_timeout(1000)
            texts=[]
            try: texts.append(page.locator("body").inner_text(timeout=30000))
            except Exception: pass
            for frame in page.frames:
                try:
                    v=frame.locator("body").inner_text(timeout=5000)
                    if v: texts.append(v)
                except Exception: pass
            for body in captured_json:
                try:
                    total,status=exact_offer_from_json(json.loads(body))
                    if total is not None: return total,status
                except Exception: pass
            for text in texts+captured_text:
                total,status=extract_target_from_text(text)
                if total is not None: return total,status
            return None,"target room/rate not found"
        finally:
            context.close(); browser.close()


def main():
    HISTORY_FILE.parent.mkdir(parents=True,exist_ok=True)
    try: history=json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception: history=[]
    checked=datetime.now(timezone.utc).isoformat(); total,status=fetch_live_rate()
    print(f"checked_at={checked} status={status} total={total}")
    if total is None:
        history.append({"checked_at":checked,"status":status}); HISTORY_FILE.write_text(json.dumps(history[-100:],indent=2),encoding="utf-8"); return
    prices=[x["total"] for x in history if isinstance(x,dict) and isinstance(x.get("total"),(int,float))]
    previous=prices[-1] if prices else BASELINE_TOTAL; change=total-previous
    history.append({"checked_at":checked,"status":"ok","total":total,"currency":CURRENCY})
    if change<0:
        send_drop_alert("🚨 IBIS GOA PRICE DROP 🚨\n\n" f"💰 FINAL PAYABLE PRICE: ₹{total:,.2f}\n📉 PRICE DROPPED BY: ₹{abs(change):,.2f}\nPrevious: ₹{previous:,.2f}\n\n{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\nRoom: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\nSource: ALL Accor official booking page")
    now=datetime.now().astimezone(); slot=now.strftime("%Y-%m-%d-%H")
    already=any(isinstance(x,dict) and x.get("notification")=="daily" and x.get("slot")==slot for x in history[-30:])
    if now.hour in DAILY_UPDATE_HOURS and now.minute<10 and not already:
        direction="↓" if change<0 else "↑" if change>0 else "="
        send_telegram("🏨 IBIS GOA CURRENT PRICE\n\n" f"💰 FINAL PAYABLE PRICE: ₹{total:,.2f}\nChange: {direction} ₹{abs(change):,.2f}\nBaseline: ₹{BASELINE_TOTAL:,.2f}\n\n{HOTEL}\nStay: {CHECKIN} → {CHECKOUT}\nGuests: {GUESTS}\nRoom: {ROOM_LABEL}\nRate: {RATE_LABEL}\n\nSource: ALL Accor official booking page")
        history.append({"notification":"daily","slot":slot,"sent_at":checked})
    HISTORY_FILE.write_text(json.dumps(history[-100:],indent=2),encoding="utf-8")

if __name__=="__main__": main()
