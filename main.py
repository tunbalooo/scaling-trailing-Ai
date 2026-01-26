import os
import json
import math
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# -----------------------------
# In-memory trade store
# (per symbol: keeps last open trade)
# -----------------------------
OPEN_TRADES = {}   # { "NQ1!": {...}, "SI1!": {...} }

# -----------------------------
# Helpers
# -----------------------------
def now_ts():
    return int(time.time())

def safe_float(x):
    """Convert x to float safely; returns None if not possible."""
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "" or s.lower() in ("na", "n/a", "none", "null"):
            return None
        return float(s)
    except Exception:
        return None

def fmt(x, nd=2):
    f = safe_float(x)
    if f is None:
        return "N/A"
    return f"{f:.{nd}f}"

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=15)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def session_from_tf_payload(data: dict):
    """
    Session-aware ML bucket.
    We use TradingView's exchange time isn't available directly in webhook,
    so we bucket by UTC hour of server time.
    (Good enough for now)
    """
    h = time.gmtime().tm_hour  # UTC hour
    # Rough session buckets (UTC)
    # ASIA: 00-07, LONDON: 07-13, NY: 13-21, else: ASIA
    if 0 <= h < 7:
        return "ASIA"
    if 7 <= h < 13:
        return "LONDON"
    if 13 <= h < 21:
        return "NY"
    return "ASIA"

def get_event(data: dict):
    """
    Supports both:
    - {"event":{"type":"ENTRY","side":"BUY"}, ...}
    - {"type":"ENTRY","side":"BUY", ...}  (fallback)
    """
    event = data.get("event")
    if isinstance(event, dict):
        typ = str(event.get("type", "ENTRY")).upper()
        side = str(event.get("side", "N/A")).upper()
    else:
        typ = str(data.get("type", "ENTRY")).upper()
        side = str(data.get("side", "N/A")).upper()
    return typ, side

def pick_score(data: dict, side: str):
    buyScore  = data.get("buyScore")
    sellScore = data.get("sellScore")
    s = buyScore if side == "BUY" else sellScore
    f = safe_float(s)
    if f is None:
        return None
    return f

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

# -----------------------------
# Routes
# -----------------------------
@app.route("/", methods=["GET"])
def home():
    return "Scaling & Trailing AI is LIVE", 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "open_trades": list(OPEN_TRADES.keys())}), 200

@app.route("/test-telegram", methods=["GET"])
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    typ, side = get_event(data)

    symbol = str(data.get("symbol", "N/A"))
    tf     = str(data.get("tf", data.get("timeframe", "N/A")))
    price  = safe_float(data.get("price"))
    sl     = safe_float(data.get("sl"))
    tp     = safe_float(data.get("tp"))
    adds   = safe_float(data.get("adds")) or 0.0

    sess = str(data.get("session", "")).upper().strip()
    if not sess:
        sess = session_from_tf_payload(data)

    score_val = pick_score(data, side)
    score_g   = grade(score_val)

    # -----------------------------
    # ENTRY / SCALE => store/update open trade
    # -----------------------------
    if typ in ("ENTRY", "SCALE"):
        # For ENTRY create new trade record (overwrite)
        if typ == "ENTRY":
            OPEN_TRADES[symbol] = {
                "symbol": symbol,
                "side": side,             # BUY/SELL
                "entry": price,
                "sl": sl,
                "tp": tp,
                "tf": tf,
                "session": sess,
                "score": score_val,
                "adds": 0.0,
                "opened_at": now_ts(),
            }
        else:
            # SCALE: only if trade exists; just increments adds
            if symbol in OPEN_TRADES:
                OPEN_TRADES[symbol]["adds"] = adds
                # keep latest sl/tp if Pine sends updated values
                if sl is not None: OPEN_TRADES[symbol]["sl"] = sl
                if tp is not None: OPEN_TRADES[symbol]["tp"] = tp

        header = "📊 TRADE ALERT" if typ == "ENTRY" else "➕ SCALE ALERT"
        msg = (
            f"{header}\n\n"
            f"{symbol} — {side}\n"
            f"Type: {typ}\n"
            f"TF: {tf}\n\n"
            f"Entry: {fmt(price)}\n"
            f"SL: {fmt(sl)}\n"
            f"TP: {fmt(tp)}\n"
            f"Adds: {adds:.0f}\n"
            f"Score: {fmt(score_val, 0)} ({score_g})\n"
            f"Session: {sess}\n"
        )

        r = send_telegram(msg)
        return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

    # -----------------------------
    # TRAIL => label WIN/LOSS + send message
    # -----------------------------
    if typ == "TRAIL":
        exit_price = price  # Pine sends close as price
        trade = OPEN_TRADES.get(symbol)

        # If no stored trade, still send a trail exit alert
        if not trade:
            msg = (
                f"🏁 TRAIL EXIT\n\n"
                f"{symbol} — {side}\n"
                f"Type: TRAIL\n"
                f"TF: {tf}\n\n"
                f"Exit: {fmt(exit_price)}\n"
                f"(No stored entry found)\n"
            )
            r = send_telegram(msg)
            return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

        entry_side = trade.get("side")
        entry_price = trade.get("entry")
        entry_sess  = trade.get("session", sess)
        entry_tf    = trade.get("tf", tf)
        trade_id = f"{symbol}|{entry_tf}|{entry_sess}|{trade.get('opened_at','')}"

        # Determine WIN/LOSS from entry_side
        # BUY wins if exit > entry, SELL wins if exit < entry
        result = "LOSS"
        win = False
        if entry_price is not None and exit_price is not None:
            if entry_side == "BUY":
                win = exit_price > entry_price
            elif entry_side == "SELL":
                win = exit_price < entry_price
            result = "WIN" if win else "LOSS"

        icon = "✅" if result == "WIN" else "❌"

        msg = (
            f"🏁 TRAIL LABELED ({entry_sess})\n\n"
            f"{symbol} ({trade_id[-6:]}) {entry_side}\n"
            f"Entry: {fmt(entry_price)}  Exit: {fmt(exit_price)}\n"
            f"Result: {result} {icon}\n"
            f"Model: {symbol}!|{entry_sess}\n"
        )

        # Remove trade after trail
        OPEN_TRADES.pop(symbol, None)

        r = send_telegram(msg)
        return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

    # Unknown type
    msg = f"⚠️ Unknown event type received: {typ}\nRaw: {json.dumps(data)[:300]}"
    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
