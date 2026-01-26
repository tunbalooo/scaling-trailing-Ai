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
    payload = {"chat_id": CHAT_ID, "text": text}

    try:
        r = requests.post(url, json=payload, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def safe_str(x, default="N/A"):
    if x is None:
        return default
    s = str(x).strip()
    if s == "" or s.lower() in ("na", "n/a", "null", "none"):
        return default
    return s

def safe_float_str(x, nd=2, default="N/A"):
    s = safe_str(x, default=default)
    if s == default:
        return default
    try:
        return f"{float(s):.{nd}f}"
    except:
        return s

def parse_payload():
    """
    TradingView can send:
    - JSON (application/json)
    - plain text that still contains JSON
    """
    # 1) Try JSON normally
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    # 2) Try raw body as JSON
    raw = request.data.decode("utf-8", errors="ignore").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except:
        # last resort: return something so you can see what came in
        return {"_raw": raw}

def grade(score):
    try:
        s = float(score)
    except:
        return "N/A"
    return "A" if s >= 80 else "B" if s >= 65 else "C" if s >= 50 else "SKIP"

@app.route("/", methods=["GET"])
def home():
    return "Scaling & Trailing AI is LIVE", 200

@app.route("/test-telegram", methods=["GET"])
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = parse_payload()

    # If TradingView sent raw non-json, show it in Railway logs
    # (and still return 200 so TradingView doesn't retry forever)
    if "_raw" in data:
        msg = "⚠️ Webhook received NON-JSON body:\n\n" + data["_raw"][:800]
        send_telegram(msg)
        return jsonify({"ok": True, "note": "non-json body received"}), 200

    event = data.get("event", {})
    if not isinstance(event, dict):
        event = {}

    typ       = safe_str(event.get("type", "ENTRY")).upper()
    direction = safe_str(event.get("side", "N/A")).upper()

    symbol = safe_str(data.get("symbol"))
    tf     = safe_str(data.get("tf"))
    # Prefer entry plot for ENTRY/SCALE, but fallback to price
    entry  = safe_float_str(data.get("entry"), 2, default="N/A")
    price  = safe_float_str(data.get("price"), 2, default="N/A")
    sl     = safe_float_str(data.get("sl"), 2, default="N/A")
    tp     = safe_float_str(data.get("tp"), 2, default="N/A")
    adds   = safe_float_str(data.get("adds"), 0, default="0")

    buyScore  = safe_str(data.get("buyScore"))
    sellScore = safe_str(data.get("sellScore"))

    # pick score based on direction
    score = buyScore if direction == "BUY" else sellScore
    score_num = safe_float_str(score, 0, default="N/A")
    g = grade(score)

    header = "📊 TRADE ALERT"
    if typ == "SCALE":
        header = "➕ SCALE ALERT"
    elif typ == "TRAIL":
        header = "🏁 TRAIL EXIT"

    # Use entry for ENTRY/SCALE, use price for TRAIL exit
    shown_entry = entry if entry != "N/A" else price
    shown_tp = tp

    msg = (
        f"{header}\n\n"
        f"{symbol} — {direction}\n"
        f"Type: {typ}\n"
        f"TF: {tf}\n\n"
        f"Entry: {shown_entry}\n"
        f"SL: {sl}\n"
        f"TP: {shown_tp}\n"
        f"Adds: {adds}\n"
        f"Score: {score_num} ({g})\n"
    )

    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r, "received": data}), (200 if r.get("ok") else 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
