import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}

    try:
        r = requests.post(url, json=payload, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/")
def home():
    return "✅ Webhook is LIVE", 200

@app.get("/test-telegram")
def test_telegram():
    result = send_telegram("✅ Telegram test from Railway is working.")
    return jsonify(result), (200 if result.get("ok") else 500)

@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}

    symbol     = data.get("symbol", "N/A")
    direction  = str(data.get("direction", "N/A")).upper()
    setup      = data.get("setup", "N/A")
    timeframe  = data.get("timeframe", "N/A")
    price      = data.get("price", "N/A")

    sl         = data.get("sl", "N/A")
    tp         = data.get("tp", "N/A")
    score      = data.get("score", "N/A")
    grade      = data.get("grade", "N/A")

    # Clean “real score” formatting
    def fmt(x):
        try:
            return f"{float(x):.0f}" if str(x).strip() != "" else "N/A"
        except:
            return str(x)

    score_fmt = fmt(score)

    msg = (
        f"📊 *TRADE ALERT*\n\n"
        f"*{symbol}* — *{direction}*\n"
        f"Setup: `{setup}`\n"
        f"TF: `{timeframe}`\n\n"
        f"Entry: `{price}`\n"
        f"SL: `{sl}`\n"
        f"TP: `{tp}`\n"
        f"Grade: *{grade}*  Score: *{score_fmt}*"
    )

    result = send_telegram(msg)
    return jsonify(result), (200 if result.get("ok") else 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
