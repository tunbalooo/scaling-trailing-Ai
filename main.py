import os
import json
import time
from datetime import datetime, timezone
from threading import Lock

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

STATE_FILE = "state.json"
_state_lock = Lock()

# In-memory state (loaded from disk on boot)
STATE = {
    "open_trades": {},   # key -> {symbol, side, entry, sl, tp, score, session, ts}
    "models": {},        # model_key -> {"w": int, "l": int}  (Beta prior handled in calc)
    "last_seen": {}      # symbol -> last ts
}

# -------------------------
# Helpers
# -------------------------
def now_utc():
    return datetime.now(timezone.utc)

def detect_session_utc(dt: datetime) -> str:
    """
    Session buckets (UTC):
      ASIA   : 00:00 - 07:00
      LONDON : 07:00 - 13:00
      NY     : 13:00 - 21:00
      OFF    : 21:00 - 24:00
    """
    h = dt.hour
    if 0 <= h < 7:
        return "ASIA"
    if 7 <= h < 13:
        return "LONDON"
    if 13 <= h < 21:
        return "NY"
    return "OFF"

def safe_str(x, default="N/A"):
    try:
        if x is None:
            return default
        s = str(x).strip()
        return s if s else default
    except:
        return default

def to_float(x):
    """
    Converts TradingView strings like "25607.00", "null", "N/A" safely.
    Returns None if not a valid float.
    """
    try:
        if x is None:
            return None
        s = str(x).strip().lower()
        if s in ("na", "n/a", "none", "null", ""):
            return None
        return float(s)
    except:
        return None

def to_int(x, default=0, clamp_min=None, clamp_max=None):
    try:
        if x is None:
            v = default
        else:
            s = str(x).strip().lower()
            if s in ("na", "n/a", "none", "null", ""):
                v = default
            else:
                v = int(float(s))
        if clamp_min is not None:
            v = max(clamp_min, v)
        if clamp_max is not None:
            v = min(clamp_max, v)
        return v
    except:
        return default

def fmt_num(x, nd=2):
    if x is None:
        return "N/A"
    try:
        return f"{float(x):.{nd}f}"
    except:
        return safe_str(x, "N/A")

def load_state():
    global STATE
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # merge safely
            STATE["open_trades"] = data.get("open_trades", {}) if isinstance(data.get("open_trades", {}), dict) else {}
            STATE["models"] = data.get("models", {}) if isinstance(data.get("models", {}), dict) else {}
            STATE["last_seen"] = data.get("last_seen", {}) if isinstance(data.get("last_seen", {}), dict) else {}
    except:
        # never crash on state load
        pass

def save_state():
    # best effort - do not crash
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
        os.replace(tmp, STATE_FILE)
    except:
        pass

def get_env_token_chat():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id

def send_telegram(text: str):
    token, chat_id = get_env_token_chat()
    if not token or not chat_id:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def fix_tp_if_wrong(side: str, price: float, sl: float, tp: float, rr: float):
    """
    Enforces:
      BUY : tp > price
      SELL: tp < price
    If tp missing or wrong, recompute from price & sl using rr.
    """
    if price is None or sl is None:
        return tp
    risk = abs(price - sl)
    if risk <= 0:
        return tp

    side = (side or "").upper()
    if side == "BUY":
        if tp is not None and tp > price:
            return tp
        return price + risk * rr

    if side == "SELL":
        if tp is not None and tp < price:
            return tp
        return price - risk * rr

    return tp

def grade_from_score(score: float) -> str:
    if score is None:
        return "N/A"
    try:
        s = float(score)
        if s >= 80:
            return "A"
        if s >= 65:
            return "B"
        if s >= 50:
            return "C"
        return "SKIP"
    except:
        return "N/A"

def model_key(symbol: str, session: str) -> str:
    return f"{symbol}||{session}"

def get_model_stats(mkey: str):
    m = STATE["models"].get(mkey)
    if not isinstance(m, dict):
        m = {"w": 0, "l": 0}
        STATE["models"][mkey] = m
    w = int(m.get("w", 0) or 0)
    l = int(m.get("l", 0) or 0)
    return w, l

def ml_pwin(mkey: str):
    # Beta(1,1) prior
    w, l = get_model_stats(mkey)
    return (w + 1) / (w + l + 2), (w + l)

def update_model(mkey: str, is_win: bool):
    w, l = get_model_stats(mkey)
    if is_win:
        w += 1
    else:
        l += 1
    STATE["models"][mkey] = {"w": w, "l": l}

def trade_key(symbol: str, side: str, session: str):
    return f"{symbol}||{side}||{session}"

def parse_payload(data: dict):
    """
    Supports:
      - Recommended: {"event":{"type":"ENTRY","side":"BUY"}, "symbol":..., "tf":..., "price":..., ...}
      - Older flat:  {"type":"ENTRY","direction":"BUY", ...}
    """
    event = data.get("event", {})
    if isinstance(event, dict):
        typ = safe_str(event.get("type", "ENTRY"), "ENTRY").upper()
        side = safe_str(event.get("side", "N/A"), "N/A").upper()
    else:
        typ = safe_str(data.get("type", "ENTRY"), "ENTRY").upper()
        # user used direction before
        side = safe_str(data.get("direction", data.get("side", "N/A")), "N/A").upper()

    symbol = safe_str(data.get("symbol", data.get("ticker", "N/A")), "N/A")
    tf = safe_str(data.get("tf", data.get("timeframe", "N/A")), "N/A")

    price = to_float(data.get("price"))
    sl = to_float(data.get("sl"))
    tp = to_float(data.get("tp"))

    buy_score = to_float(data.get("buyScore"))
    sell_score = to_float(data.get("sellScore"))

    # Your adds has been coming as crazy numbers because plot_4 got misused sometimes.
    # Clamp it so telegram doesn't show "Adds: 62" etc.
    adds = to_int(data.get("adds", 0), default=0, clamp_min=0, clamp_max=5)

    return typ, side, symbol, tf, price, sl, tp, buy_score, sell_score, adds

# Load state at boot
with _state_lock:
    load_state()

# -------------------------
# Routes
# -------------------------
@app.route("/", methods=["GET"])
def home():
    return "✅ Webhook server is LIVE", 200

@app.route("/test-telegram", methods=["GET"])
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    try:
        typ, side, symbol, tf, price, sl, tp, buy_score, sell_score, adds = parse_payload(data)

        # Session-aware key
        session = detect_session_utc(now_utc())

        # Pick score based on side
        score = buy_score if side == "BUY" else sell_score
        grade = grade_from_score(score)

        # FIX TP if it is wrong direction or missing
        rr = float(os.getenv("RR_DEFAULT", "1.5"))
        tp = fix_tp_if_wrong(side, price, sl, tp, rr)

        # Model probability
        mkey = model_key(symbol, session)
        pwin, samples = ml_pwin(mkey)

        # Build telegram header
        if typ == "SCALE":
            header = "➕ SCALE ALERT"
        elif typ == "TRAIL":
            header = "🏁 TRAIL EXIT"
        else:
            header = "📊 TRADE ALERT"

        # ---- ENTRY/SCALE: store open trade ----
        tk = trade_key(symbol, side, session)

        with _state_lock:
            if typ in ("ENTRY", "SCALE"):
                # store/overwrite open trade snapshot
                STATE["open_trades"][tk] = {
                    "symbol": symbol,
                    "side": side,
                    "entry": price,
                    "sl": sl,
                    "tp": tp,
                    "score": score,
                    "session": session,
                    "ts": time.time()
                }
                save_state()

            # ---- TRAIL: auto-label win/loss and update model ----
            trail_result_text = ""
            if typ == "TRAIL":
                # find the open trade in same session+symbol for the opposite? (TRAIL side is the exit action)
                # We stored trade as entry side BUY/SELL.
                # Trail event side in your alerts is exit action; BUT we want entry direction for outcome.
                # Easiest: assume if TRAIL side=SELL => closing a BUY trade; if TRAIL side=BUY => closing a SELL trade.
                entry_side = "BUY" if side == "SELL" else "SELL" if side == "BUY" else None
                tk2 = trade_key(symbol, entry_side or "N/A", session)

                trade = STATE["open_trades"].get(tk2)
                exit_price = price  # webhook price = {{close}} at trail bar

                if trade and trade.get("entry") is not None and exit_price is not None and entry_side in ("BUY", "SELL"):
                    entry_price = float(trade["entry"])
                    exit_price = float(exit_price)

                    pnl = (exit_price - entry_price) if entry_side == "BUY" else (entry_price - exit_price)
                    is_win = pnl > 0

                    prev_p, prev_samples = ml_pwin(mkey)
                    update_model(mkey, is_win)

                    # remove open trade
                    STATE["open_trades"].pop(tk2, None)
                    save_state()

                    result_word = "WIN ✅" if is_win else "LOSS ❌"
                    trail_result_text = (
                        f"\n\n🏁 TRAIL LABELED ({session})\n"
                        f"{symbol} ({entry_side})\n"
                        f"Entry: {fmt_num(entry_price)}  Exit: {fmt_num(exit_price)}\n"
                        f"Result: {result_word}\n"
                        f"Model: {symbol}||{session}\n"
                        f"Prev P(win): {prev_p:.2f} | Samples: {prev_samples}"
                    )

        msg = (
            f"{header}\n\n"
            f"{symbol} — {side}\n"
            f"Type: {typ}\n"
            f"TF: {tf}\n\n"
            f"Entry: {fmt_num(price)}\n"
            f"SL: {fmt_num(sl)}\n"
            f"TP: {fmt_num(tp)}\n"
            f"Adds: {adds}\n"
            f"Score: {fmt_num(score, 1)} ({grade})\n"
            f"Session: {session}\n"
            f"ML P(win): {pwin:.2f} (samples: {samples})\n"
            f"Model: {symbol}||{session}"
        )

        # Send main alert
        r1 = send_telegram(msg)

        # Send trail labeled outcome if exists
        r2 = None
        if trail_result_text:
            r2 = send_telegram(trail_result_text)

        ok = bool(r1.get("ok"))
        return jsonify({"ok": ok, "telegram": r1, "trail_labeled": r2}), 200

    except Exception as e:
        # Never crash Railway; always return 200 so TradingView doesn't spam retries
        return jsonify({"ok": False, "error": str(e), "raw": data}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
