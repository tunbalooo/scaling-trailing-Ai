import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# Config (Railway env vars)
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID).")
        return {"ok": False, "error": "telegram_not_configured"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print("Telegram send error:", e)
        return {"ok": False, "error": str(e)}

# -------------------------
# Helpers
# -------------------------
def _to_float(x):
    """Convert TradingView strings like 'null', 'na', '' to None or float."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().lower()
    if s in ("", "na", "n/a", "null", "none"):
        return None
    try:
        return float(s)
    except Exception:
        return None

def _to_int(x, default=0):
    if x is None:
        return default
    try:
        return int(float(x))
    except Exception:
        return default

def grade(score):
    if score is None:
        return "N/A"
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "SKIP"

def infer_side(data):
    """
    If you didn't send side, infer it:
    - If buyScore > sellScore => BUY
    - Else SELL
    """
    bs = _to_float(data.get("buyScore"))
    ss = _to_float(data.get("sellScore"))
    if bs is None and ss is None:
        return "N/A"
    if ss is None:
        return "BUY"
    if bs is None:
        return "SELL"
    return "BUY" if bs >= ss else "SELL"

def choose_score_and_grade(side, data):
    bs = _to_float(data.get("buyScore"))
    ss = _to_float(data.get("sellScore"))
    if side == "BUY":
        sc = bs
    elif side == "SELL":
        sc = ss
    else:
        sc = None
    return sc, grade(sc)

def fix_tp_direction(side, price, sl, tp):
    """
    Ensures TP is on the correct side of entry.
    If tp is wrong/missing, compute a safe TP using same RR as implied by SL distance (default 1.5R).
    """
    if price is None or sl is None:
        return tp  # can't fix

    rr_default = 1.5
    risk = abs(price - sl)
    if risk <= 0:
        return tp

    # If tp missing, set it
    if tp is None:
        if side == "BUY":
            return price + risk * rr_default
        if side == "SELL":
            return price - risk * rr_default
        return None

    # If tp exists but wrong side, flip it
    if side == "BUY" and tp <= price:
        return price + risk * rr_default
    if side == "SELL" and tp >= price:
        return price - risk * rr_default

    return tp

# =========================
# Routes
# =========================
@app.get("/")
def home():
    return "OK"

@app.get("/test_telegram")
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.post("/webhook")
def webhook():
    raw = request.get_data(as_text=True) or ""

    # 1) Try parse JSON safely
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        # Non-JSON payload (common when alert_message not quoted)
        msg = "⚠️ NON-JSON ALERT BODY RECEIVED:\n" + raw[:1500]
        send_telegram(msg)
        return jsonify({"ok": False, "error": "non_json", "raw": raw[:300]}), 400

    # 2) Normalize fields
    event = str(data.get("event", "")).strip().upper()  # ENTRY / SCALE / TRAIL
    symbol = str(data.get("symbol", "N/A"))
    tf     = str(data.get("tf", "N/A"))

    price  = _to_float(data.get("price"))
    sl     = _to_float(data.get("sl"))
    tp     = _to_float(data.get("tp"))
    adds   = _to_int(data.get("adds"), default=0)

    # If event came from alertcondition message, it will be ENTRY/SCALE/TRAIL
    if event not in ("ENTRY", "SCALE", "TRAIL"):
        # Sometimes people send "ENTRY BUY" etc — keep it readable
        if "ENTRY" in event:
            event = "ENTRY"
        elif "SCALE" in event:
            event = "SCALE"
        elif "TRAIL" in event:
            event = "TRAIL"
        else:
            event = "ENTRY"

    # 3) Side + score
    side = str(data.get("side") or "").upper().strip()
    if side not in ("BUY", "SELL"):
        side = infer_side(data)

    score, g = choose_score_and_grade(side, data)

    # 4) Fix TP direction if needed
    tp_fixed = fix_tp_direction(side, price, sl, tp)
    if tp is None and tp_fixed is not None:
        tp = tp_fixed
    elif tp_fixed is not None:
        tp = tp_fixed

    # 5) Build message
    def fmt(x, nd=2):
        if x is None:
            return "N/A"
        try:
            return f"{float(x):.{nd}f}"
        except Exception:
            return str(x)

    warning = ""
    if price is not None and tp is not None:
        if side == "BUY" and tp <= price:
            warning = "⚠️ TP should be ABOVE entry for BUY\n"
        if side == "SELL" and tp >= price:
            warning = "⚠️ TP should be BELOW entry for SELL\n"

    text = (
        f"📊 TRADE ALERT\n\n"
        f"{symbol} — {side}\n"
        f"Type: {event}\n"
        f"TF: {tf}\n\n"
        f"Entry/Price: {fmt(price, 2)}\n"
        f"SL: {fmt(sl, 2)}\n"
        f"TP: {fmt(tp, 2)}\n"
        f"Adds: {adds}\n"
        f"Score: {fmt(score, 0)} ({g})\n"
    )

    if warning:
        text += "\n" + warning

    r = send_telegram(text)
    return jsonify({"ok": True, "telegram": r})

# =========================
# Run
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
