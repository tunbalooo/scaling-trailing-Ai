import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Optional behavior switches
LEARNING_MODE = os.getenv("LEARNING_MODE", "0").strip() == "1"   # default OFF
MIN_SCORE     = float(os.getenv("MIN_SCORE", "0").strip() or "0") # default 0

# In-memory state (Railway restarts will reset this)
state = {
    "stats": {},            # symbol -> {"wins": int, "losses": int}
    "open_trade": {},       # symbol -> trade_id
    "trades": {},           # trade_id -> trade dict
    "learn": {}             # symbol -> side -> bucket -> {"w":int,"l":int}
}

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def to_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().lower()
    if s in ("", "na", "n/a", "null", "none"):
        return None
    try:
        return float(s)
    except:
        return None

def fmt_price(x):
    return "N/A" if x is None else f"{x:.2f}"

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured. Message:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()

def grade(score: float | None):
    if score is None:
        return ("N/A", "N/A")
    s = float(score)
    if s >= 80: return (str(int(round(s))), "A")
    if s >= 65: return (str(int(round(s))), "B")
    if s >= 50: return (str(int(round(s))), "C")
    return (str(int(round(s))), "SKIP")

# --- Event parsing (supports your explicit event strings) ---
def normalize_event(event: str):
    e = (event or "").strip().upper().replace(" ", "_")
    # common variants
    e = e.replace("__", "_")
    return e

def side_from_event(e: str):
    # Explicit mapping for your preferred events
    if e.endswith("_BUY") or "_BUY" in e:
        return "BUY"
    if e.endswith("_SELL") or "_SELL" in e:
        return "SELL"
    # TRAIL_EXIT_LONG/SHORT mapping
    if "TRAIL_EXIT_LONG" in e:
        return "BUY"
    if "TRAIL_EXIT_SHORT" in e:
        return "SELL"
    return "N/A"

def is_entry(e: str): return e.startswith("ENTRY")
def is_scale(e: str): return e.startswith("SCALE")
def is_trail(e: str): return e.startswith("TRAIL")

# --- Fix SL/TP mixups automatically (common when plots are mapped wrong) ---
def auto_fix_sl_tp(side: str, price: float | None, sl: float | None, tp: float | None):
    # If we don't have enough info, do nothing
    if side not in ("BUY", "SELL") or price is None or sl is None or tp is None:
        return sl, tp

    # BUY: expected sl < price < tp
    # If reversed (sl > price and tp < price), swap
    if side == "BUY" and sl > price and tp < price:
        return tp, sl

    # SELL: expected tp < price < sl
    # If reversed (sl < price and tp > price), swap
    if side == "SELL" and sl < price and tp > price:
        return tp, sl

    return sl, tp

# --- Simple learning (optional) ---
def score_bucket(score: float | None):
    if score is None:
        return None
    s = float(score)
    if s >= 80: return "A"
    if s >= 65: return "B"
    if s >= 50: return "C"
    return "SKIP"

def learn_record(symbol: str, side: str, score: float | None, win: bool):
    if side not in ("BUY", "SELL"):
        return
    b = score_bucket(score)
    if b is None:
        return
    state["learn"].setdefault(symbol, {}).setdefault(side, {}).setdefault(b, {"w": 0, "l": 0})
    if win:
        state["learn"][symbol][side][b]["w"] += 1
    else:
        state["learn"][symbol][side][b]["l"] += 1

def learn_should_send(symbol: str, side: str, score: float | None):
    # Default: send everything
    if not LEARNING_MODE:
        return True

    # Hard floor if you want it
    if score is not None and float(score) < MIN_SCORE:
        return False

    # If we have learning stats, we can suppress SKIP bucket when it's losing a lot
    b = score_bucket(score)
    if b is None:
        return True

    data = state["learn"].get(symbol, {}).get(side, {}).get(b)
    if not data:
        return True

    w, l = data["w"], data["l"]
    total = w + l
    if total < 10:
        return True  # not enough history yet

    winrate = w / total
    # Example rule: suppress SKIP/C if winrate is poor
    if b in ("SKIP", "C") and winrate < 0.40:
        return False

    return True

# --- Trade ID ---
def new_trade_id(symbol: str):
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{symbol}-{ts}"

@app.get("/")
def home():
    return "OK"

@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Non-JSON body"}), 400

    raw_event = str(data.get("event", "")).strip()
    e = normalize_event(raw_event)

    symbol = str(data.get("symbol", "N/A")).strip()
    tf     = str(data.get("tf", "N/A")).strip()

    price = to_float(data.get("price"))
    sl    = to_float(data.get("sl"))
    tp    = to_float(data.get("tp"))
    adds  = to_float(data.get("adds"))

    buyScore  = to_float(data.get("buyScore"))
    sellScore = to_float(data.get("sellScore"))

    side = side_from_event(e)

    # choose the right score by side
    score_val = buyScore if side == "BUY" else sellScore if side == "SELL" else None
    score_num, score_grade = grade(score_val)

    # Init stats
    if symbol not in state["stats"]:
        state["stats"][symbol] = {"wins": 0, "losses": 0}

    # Auto-fix common SL/TP swap issues
    sl, tp = auto_fix_sl_tp(side, price, sl, tp)

    # ---------------------------------------------------------
    # ENTRY
    # ---------------------------------------------------------
    if is_entry(e):
        # create a new trade_id
        trade_id = new_trade_id(symbol)
        state["open_trade"][symbol] = trade_id

        state["trades"][trade_id] = {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "tf": tf,
            "entry": price,
            "sl": sl,
            "tp": tp,
            "adds": int(adds) if adds is not None else 0,
            "score": score_val,
            "opened_at": now_str(),
            "closed_at": None,
            "exit": None,
            "result": None
        }

        # sanity warnings
        warning = ""
        if side == "BUY" and tp is not None and price is not None and tp <= price:
            warning = "\n⚠️ TP should be ABOVE entry for BUY"
        if side == "SELL" and tp is not None and price is not None and tp >= price:
            warning = "\n⚠️ TP should be BELOW entry for SELL"

        # learning filter (optional)
        if learn_should_send(symbol, side, score_val):
            text = (
                f"📊 TRADE ALERT\n\n"
                f"{symbol} — {side}\n"
                f"Type: ENTRY\n"
                f"TF: {tf}\n"
                f"TradeID: {trade_id}\n\n"
                f"Entry: {fmt_price(price)}\n"
                f"SL: {fmt_price(sl)}\n"
                f"TP: {fmt_price(tp)}\n"
                f"Adds: {int(adds) if adds is not None else 0}\n"
                f"Score: {score_num} ({score_grade})\n"
                f"W/L: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}"
                f"{warning}"
            )
            send_telegram(text)

        return jsonify({"ok": True, "trade_id": trade_id})

    # ---------------------------------------------------------
    # SCALE
    # ---------------------------------------------------------
    if is_scale(e):
        trade_id = state["open_trade"].get(symbol)
        if trade_id and trade_id in state["trades"]:
            # attach scale to current trade
            state["trades"][trade_id]["adds"] = int(adds) if adds is not None else state["trades"][trade_id].get("adds", 0)

            if learn_should_send(symbol, side, score_val):
                text = (
                    f"📈 SCALE ALERT\n\n"
                    f"{symbol} — {side}\n"
                    f"Type: SCALE\n"
                    f"TF: {tf}\n"
                    f"TradeID: {trade_id}\n\n"
                    f"Price: {fmt_price(price)}\n"
                    f"SL: {fmt_price(sl)}\n"
                    f"TP: {fmt_price(tp)}\n"
                    f"Adds: {int(adds) if adds is not None else 0}\n"
                    f"Score: {score_num} ({score_grade})\n"
                    f"W/L: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}"
                )
                send_telegram(text)

            return jsonify({"ok": True, "trade_id": trade_id})

        # If scale arrives without an open trade, still notify as warning
        send_telegram(f"⚠️ SCALE received but no open trade found.\n{json.dumps(data, indent=2)}")
        return jsonify({"ok": True, "warning": "scale_without_open_trade"})

    # ---------------------------------------------------------
    # TRAIL EXIT
    # ---------------------------------------------------------
    if is_trail(e):
        trade_id = state["open_trade"].get(symbol)
        last = state["trades"].get(trade_id) if trade_id else None

        # IMPORTANT: show side from the linked entry (fixes N/A / wrong side)
        display_side = last.get("side") if last and last.get("side") in ("BUY", "SELL") else side

        outcome = "N/A"
        win_bool = None

        if last and last.get("entry") is not None and price is not None:
            entry = float(last["entry"])

            if display_side == "BUY":
                win_bool = price > entry
            elif display_side == "SELL":
                win_bool = price < entry

            if win_bool is True:
                state["stats"][symbol]["wins"] += 1
                outcome = "WIN ✅"
            elif win_bool is False:
                state["stats"][symbol]["losses"] += 1
                outcome = "LOSS ❌"

            # close trade
            last["closed_at"] = now_str()
            last["exit"] = price
            last["result"] = outcome

            # learning record (optional)
            if win_bool is not None:
                learn_record(symbol, display_side, last.get("score"), win_bool)

            # clear open trade
            state["open_trade"].pop(symbol, None)

        else:
            # If we can't link it to a real entry, DO NOT pretend win/loss.
            outcome = "N/A (no linked ENTRY)"

        text = (
            f"🏁 TRAIL EXIT\n\n"
            f"{symbol} — {display_side}\n"
            f"Type: TRAIL\n"
            f"TF: {tf}\n"
            f"TradeID: {trade_id or 'N/A'}\n\n"
            f"Exit: {fmt_price(price)}\n"
            f"Result: {outcome}\n"
            f"W/L: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}"
        )
        send_telegram(text)
        return jsonify({"ok": True, "trade_id": trade_id})

    # ---------------------------------------------------------
    # Unknown event fallback
    # ---------------------------------------------------------
    send_telegram(f"⚠️ Unknown event\n{json.dumps(data, indent=2)}")
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
