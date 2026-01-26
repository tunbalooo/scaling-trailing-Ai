import os
import json
import time
import requests
from typing import Any, Dict, Optional
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DATA_FILE = "trade_state.json"  # stored on Railway container (resets if redeploy)

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def send_telegram(text: str) -> Dict[str, Any]:
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}

    try:
        r = requests.post(url, json=payload, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def load_state() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "open_trades": {},  # key: symbol -> trade dict
            "stats": {}         # key: modelKey -> {wins, losses, total}
        }

def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

def to_float(x: Any) -> Optional[float]:
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

def fmt(x: Any, nd: int = 2) -> str:
    v = to_float(x)
    return f"{v:.{nd}f}" if v is not None else "N/A"

def grade(score: Optional[float]) -> str:
    if score is None:
        return "N/A"
    if score >= 80: return "A"
    if score >= 65: return "B"
    if score >= 50: return "C"
    return "SKIP"

def safe_str(x: Any, fallback="N/A") -> str:
    if x is None:
        return fallback
    s = str(x).strip()
    return s if s else fallback

def get_event(data: Dict[str, Any]) -> Dict[str, str]:
    """
    Supports:
    - {"event":{"type":"ENTRY","side":"BUY"}, ...}
    - {"type":"ENTRY","side":"BUY", ...}  (fallback)
    """
    event = data.get("event")
    if isinstance(event, dict):
        typ = safe_str(event.get("type", "ENTRY")).upper()
        side = safe_str(event.get("side", "N/A")).upper()
        return {"type": typ, "side": side}

    # fallback
    typ = safe_str(data.get("type", "ENTRY")).upper()
    side = safe_str(data.get("side", "N/A")).upper()
    return {"type": typ, "side": side}

def choose_score(side: str, buyScore: Any, sellScore: Any) -> Optional[float]:
    """
    side BUY -> buyScore
    side SELL -> sellScore
    """
    if side == "BUY":
        return to_float(buyScore)
    if side == "SELL":
        return to_float(sellScore)
    # unknown side: choose whichever parses
    return to_float(buyScore) or to_float(sellScore)

def win_loss_from_exit(side: str, entry: float, exit_price: float) -> str:
    # BUY wins if exit > entry, SELL wins if exit < entry
    if side == "BUY":
        return "WIN" if exit_price > entry else "LOSS"
    if side == "SELL":
        return "WIN" if exit_price < entry else "LOSS"
    return "LOSS"

def model_key(symbol: str, session: str) -> str:
    return f"{symbol}||{session}"

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.route("/", methods=["GET"])
def home():
    return "Trade Bot is LIVE", 200

@app.route("/test-telegram", methods=["GET"])
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.route("/stats", methods=["GET"])
def stats():
    state = load_state()
    return jsonify(state.get("stats", {})), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    state = load_state()
    data = request.get_json(silent=True) or {}

    ev = get_event(data)
    typ = ev["type"]         # ENTRY / SCALE / TRAIL
    side = ev["side"]        # BUY / SELL

    symbol  = safe_str(data.get("symbol"))
    tf      = safe_str(data.get("tf"))
    session = safe_str(data.get("session", "N/A")).upper()

    price = to_float(data.get("price"))
    sl    = to_float(data.get("sl"))
    tp    = to_float(data.get("tp"))
    adds  = to_float(data.get("adds")) or 0.0

    buyScore  = data.get("buyScore")
    sellScore = data.get("sellScore")
    score = choose_score(side, buyScore, sellScore)

    g = grade(score)

    # ----------------------------
    # Save / update open trade
    # ----------------------------
    open_trades = state["open_trades"]

    if typ == "ENTRY":
        # store the new entry as the "current" position for this symbol
        if price is not None:
            open_trades[symbol] = {
                "side": side,
                "entry": price,
                "sl": sl,
                "tp": tp,
                "tf": tf,
                "session": session,
                "ts": int(time.time())
            }

    elif typ == "SCALE":
        # scaling doesn't change entry here (you can expand later)
        # but we keep it as info
        pass

    elif typ == "TRAIL":
        # on trail, label outcome if we have an open entry
        tr = open_trades.get(symbol)
        if tr and price is not None:
            entry_price = float(tr["entry"])
            entry_side  = tr["side"]
            result = win_loss_from_exit(entry_side, entry_price, float(price))

            key = model_key(symbol, tr.get("session", "N/A"))
            st = state["stats"].setdefault(key, {"wins": 0, "losses": 0, "total": 0})
            st["total"] += 1
            if result == "WIN":
                st["wins"] += 1
            else:
                st["losses"] += 1

            # remove open trade after it is closed by TRAIL
            open_trades.pop(symbol, None)

            msg = (
                f"🏁 TRAIL LABELED ({tr.get('session','N/A')})\n\n"
                f"{symbol} — {entry_side}\n"
                f"TF: {tr.get('tf','N/A')}\n\n"
                f"Entry: {entry_price:.2f}  Exit: {price:.2f}\n"
                f"Result: {result} {'✅' if result=='WIN' else '❌'}\n"
                f"Model: {key}\n"
                f"W/L: {st['wins']}/{st['losses']}  Total: {st['total']}"
            )
            send_telegram(msg)

    # persist
    save_state(state)

    # ----------------------------
    # Telegram message
    # ----------------------------
    header = "📊 TRADE ALERT"
    if typ == "SCALE": header = "➕ SCALE ALERT"
    if typ == "TRAIL": header = "🏁 TRAIL EXIT"

    # sanity warning for TP direction (helps you debug instantly)
    warn = ""
    if price is not None and tp is not None:
        if side == "BUY" and tp <= price:
            warn = "⚠️ TP should be ABOVE entry for BUY\n"
        if side == "SELL" and tp >= price:
            warn = "⚠️ TP should be BELOW entry for SELL\n"

    msg = (
        f"{header}\n\n"
        f"{symbol} — {side}\n"
        f"Type: {typ}\n"
        f"TF: {tf}\n"
        f"Session: {session}\n\n"
        f"Entry: {fmt(price)}\n"
        f"SL: {fmt(sl)}\n"
        f"TP: {fmt(tp)}\n"
        f"Adds: {int(adds)}\n"
        f"Score: {('N/A' if score is None else int(score))} ({g})\n"
        f"{warn}"
    )

    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
