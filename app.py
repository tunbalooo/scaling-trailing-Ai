import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def send_telegram(text: str):
    """Send plain-text message to Telegram (no Markdown to avoid formatting failures)."""
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.route("/", methods=["GET"])
def home():
    return "Scaling & Trailing AI is LIVE", 200

@app.route("/test-telegram", methods=["GET"])
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    # Expected fields from TradingView
    typ       = str(data.get("type", "ENTRY")).upper()       # ENTRY / SCALE / TRAIL
    symbol    = str(data.get("symbol", "N/A"))
    direction = str(data.get("direction", "N/A")).upper()    # BUY / SELL
    price     = str(data.get("price", "N/A"))
    tf        = str(data.get("timeframe", "N/A"))
    setup     = str(data.get("setup", "EMA9"))
    score     = str(data.get("score", "N/A"))
    sl        = str(data.get("sl", "N/A"))
    tp        = str(data.get("tp", "N/A"))
    notes     = str(data.get("notes", ""))

    header = "📊 TRADE ALERT"
    if typ == "SCALE":
        header = "➕ SCALE ALERT"
    elif typ == "TRAIL":
        header = "🧹 TRAIL EXIT"

    msg = (
        f"{header}\n\n"
        f"{symbol} — {direction}\n"
        f"Type: {typ}\n"
        f"Setup: {setup}\n"
        f"TF: {tf}\n\n"
        f"Entry: {price}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"Score: {score}\n"
    )

    if notes:
        msg += f"\nNotes: {notes}"

    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

if __name__ == "__main__":
    # Local testing only. Railway uses gunicorn.
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
