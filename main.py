import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def parse_type_side(data: dict):
    """
    Supports both:
    1) JSON: {"type":"ENTRY","direction":"BUY"}
    2) TradingView alert_message wrapper: {"event":{"type":"ENTRY","side":"BUY"}}
    3) Old string: "TYPE=ENTRY;SIDE=BUY" (if sent in 'message')
    """
    # Case A: event object
    event = data.get("event")
    if isinstance(event, dict):
        typ = event.get("type", data.get("type", "ENTRY"))
        side = event.get("side", data.get("direction", "N/A"))
        return str(typ).upper(), str(side).upper()

    # Case B: direct fields
    if "type" in data or "direction" in data:
        typ = data.get("type", "ENTRY")
        side = data.get("direction", "N/A")
        return str(typ).upper(), str(side).upper()

    # Case C: old semicolon string
    msg = data.get("message") or data.get("alert_message") or ""
    if isinstance(msg, str) and "TYPE=" in msg and "SIDE=" in msg:
        parts = msg.replace(" ", "").split(";")
        kv = {}
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                kv[k.upper()] = v.upper()
        return kv.get("TYPE", "ENTRY"), kv.get("SIDE", "N/A")

    return "ENTRY", "N/A"

def build_msg(data: dict):
    typ, direction = parse_type_side(data)

    symbol = str(data.get("symbol", data.get("ticker", "N/A")))
    tf     = str(data.get("tf", data.get("timeframe", data.get("interval", "N/A"))))
    price  = str(data.get("price", data.get("close", "N/A")))
    sl     = str(data.get("sl", "N/A"))
    tp     = str(data.get("tp", "N/A"))
    adds   = str(data.get("adds", data.get("addCount", "0")))

    # Score logic: if BUY use buyScore else sellScore else score
    buyScore  = data.get("buyScore")
    sellScore = data.get("sellScore")
    score     = data.get("score")

    if direction == "BUY" and buyScore is not None:
        score = buyScore
    elif direction == "SELL" and sellScore is not None:
        score = sellScore

    header = "📊 TRADE ALERT"
    if typ == "SCALE":
        header = "➕ SCALE ALERT"
    elif typ == "TRAIL":
        header = "🏁 TRAIL EXIT"

    return (
        f"{header}\n\n"
        f"{symbol} — {direction}\n"
        f"Type: {typ}\n"
        f"TF: {tf}\n\n"
        f"Entry: {price}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"Adds: {adds}\n"
        f"Score: {score}\n"
    )

@app.get("/")
def home():
    return jsonify({"status": "live", "routes": ["/", "/webhook", "/test-telegram"]})

@app.get("/test-telegram")
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

# ✅ Accept TradingView webhooks to BOTH endpoints
@app.post("/")
def webhook_root():
    data = request.get_json(silent=True) or {}
    msg = build_msg(data)
    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}
    msg = build_msg(data)
    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)
