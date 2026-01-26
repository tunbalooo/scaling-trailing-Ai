import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Railway Variables MUST be named exactly like this:
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def send_telegram(text: str):
    """Send plain-text message to Telegram (no Markdown to avoid formatting failures)."""
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}

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

def build_message(data: dict) -> str:

    def fnum(x, nd=2):
    try:
        return f"{float(x):.{nd}f}"
    except:
        return str(x)

price = fnum(data.get("price", "N/A"), 2)
sl    = fnum(data.get("sl", "N/A"), 2)
tp    = fnum(data.get("tp", "N/A"), 2)

    """
    Expected TradingView payload (recommended):
    {
      "event": {"type":"ENTRY","side":"BUY"},
      "symbol":"NQ1!",
      "tf":"2",
      "price":"18000.25",
      "sl":"17980",
      "tp":"18030",
      "buyScore":"78",
      "sellScore":"55",
      "adds":"0"
    }
    """

    # --- Parse event/type/side ---
    event = data.get("event", {})
    if isinstance(event, str):
        # Sometimes event might come as a string; try to parse JSON
        try:
            event = json.loads(event)
        except Exception:
            event = {}

    typ = str(event.get("type", "ENTRY")).upper() if isinstance(event, dict) else "ENTRY"
    direction = str(event.get("side", "N/A")).upper() if isinstance(event, dict) else "N/A"

    # --- Main fields ---
    symbol = str(data.get("symbol", "N/A"))
    tf     = str(data.get("tf", "N/A"))
    price  = str(data.get("price", "N/A"))
    sl     = str(data.get("sl", "N/A"))
    tp     = str(data.get("tp", "N/A"))
    adds   = str(data.get("adds", 0))

    # --- Score selection ---
    buyScore  = data.get("buyScore")
    sellScore = data.get("sellScore")
    score = buyScore if direction == "BUY" else sellScore

    # Grade (optional)
    grade = "N/A"
    try:
        s = int(float(score))
        grade = "A" if s >= 80 else "B" if s >= 65 else "C" if s >= 50 else "SKIP"
    except Exception:
        pass

    # Header emoji by type
    header = "📊 TRADE ALERT"
    if typ == "SCALE":
        header = "➕ SCALE ALERT"
    elif typ == "TRAIL":
        header = "🏁 TRAIL EXIT"

    msg = (
        f"{header}\n\n"
        f"{symbol} — {direction}\n"
        f"Type: {typ}\n"
        f"TF: {tf}\n\n"
        f"Entry: {price}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"Adds: {adds}\n"
        f"Score: {score} ({grade})"
    )

    return msg

# ✅ Accept TradingView posts to both "/" and "/webhook" (bulletproof)
@app.route("/", methods=["POST"])
def webhook_root():
    data = request.get_json(silent=True) or {}
    msg = build_message(data)
    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    msg = build_message(data)
    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

if __name__ == "__main__":
    # Local testing only. Railway uses gunicorn.
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

