import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─────────────────────────────────────────────
# ENV VARIABLES (Railway)
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ─────────────────────────────────────────────
# TELEGRAM SENDER
# ─────────────────────────────────────────────
def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ─────────────────────────────────────────────
# SAFE NUMBER FORMATTER
# ─────────────────────────────────────────────
def fnum(x, nd=2):
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return "N/A"

# ─────────────────────────────────────────────
# MESSAGE BUILDER
# ─────────────────────────────────────────────
def build_message(data: dict) -> str:

    # ---- Event ----
    event = data.get("event", {})
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except Exception:
            event = {}

    typ  = str(event.get("type", "ENTRY")).upper()
    side = str(event.get("side", "N/A")).upper()

    # ---- Core fields ----
    symbol = str(data.get("symbol", "N/A"))
    tf     = str(data.get("tf", "N/A"))

    price = fnum(data.get("price"))
    sl    = fnum(data.get("sl"))
    tp    = fnum(data.get("tp"))

    adds = fnum(data.get("adds"), 0)

    buyScore  = data.get("buyScore")
    sellScore = data.get("sellScore")

    score_raw = buyScore if side == "BUY" else sellScore
    score = fnum(score_raw, 0)

    # ---- Grade ----
    grade = "N/A"
    try:
        s = int(float(score))
        if s >= 80:
            grade = "A"
        elif s >= 65:
            grade = "B"
        elif s >= 50:
            grade = "C"
        else:
            grade = "SKIP"
    except Exception:
        pass

    # ---- Header ----
    header = "📊 TRADE ALERT"
    if typ == "SCALE":
        header = "➕ SCALE ALERT"
    elif typ == "TRAIL":
        header = "🏁 TRAIL EXIT"

    # ---- Final message ----
    msg = (
        f"{header}\n\n"
        f"{symbol} — {side}\n"
        f"Type: {typ}\n"
        f"TF: {tf}\n\n"
        f"Entry: {price}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"Adds: {adds}\n"
        f"Score: {score} ({grade})"
    )

    return msg

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def root():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        msg = build_message(data)
        r = send_telegram(msg)
        return jsonify(r), (200 if r.get("ok") else 500)

    return "Scaling & Trailing AI is LIVE", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    msg = build_message(data)
    r = send_telegram(msg)
    return jsonify(r), (200 if r.get("ok") else 500)


@app.route("/test-telegram", methods=["GET"])
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)


# ─────────────────────────────────────────────
# LOCAL RUN (Railway uses gunicorn)
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
