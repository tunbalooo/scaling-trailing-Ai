import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

LEARNING_MODE = os.getenv("LEARNING_MODE", "0").strip() == "1"   # default OFF
MIN_SCORE     = float(os.getenv("MIN_SCORE", "0").strip() or "0") # default 0

# ✅ Default tick + BE tolerance in ticks (we'll override tick by symbol)
DEFAULT_TICK = float(os.getenv("DEFAULT_TICK", "0.25"))  # NQ tick default
BE_EPS_TICKS = float(os.getenv("BE_EPS_TICKS", "1"))     # treat within 1 tick as BE

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

def to_int(x):
    if x is None:
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return int(x)
    s = str(x).strip().lower()
    if s in ("", "na", "n/a", "null", "none"):
        return None
    try:
        return int(float(s))
    except:
        return None

def fmt_price(x):
    return "N/A" if x is None else f"{x:.3f}" if abs(x) < 100 else f"{x:.2f}"

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

def normalize_event(event: str):
    e = (event or "").strip().upper().replace(" ", "_")
    e = e.replace("__", "_")
    return e

def side_from_payload(data, e: str):
    side = str(data.get("side", "")).strip().upper()
    if side in ("BUY", "SELL"):
        return side
    if e.endswith("_BUY") or "_BUY" in e:
        return "BUY"
    if e.endswith("_SELL") or "_SELL" in e:
        return "SELL"
    if "TRAIL_EXIT_LONG" in e:
        return "BUY"
    if "TRAIL_EXIT_SHORT" in e:
        return "SELL"
    return "N/A"

# ----- NEW EVENT HELPERS (scalping 1-6) -----
def is_watch(e: str): return e in ("WATCH_LONG", "WATCH_SHORT")
def is_ready(e: str): return e in ("READY_LONG", "READY_SHORT")
def is_entry(e: str): return e in ("ENTRY", "ENTRY_BUY", "ENTRY_SELL") or e.startswith("ENTRY")
def is_break_even(e: str): return e in ("BREAK_EVEN", "BE_ARM")  # accept both
def is_trim(e: str): return e == "TRIM"
def is_stop_hit(e: str): return e == "STOP_HIT"
def is_exit_flip(e: str): return e == "EXIT_TREND_FLIP"

# keep your old ones too (backwards compatible)
def is_scale(e: str): return e == "SCALE" or e.startswith("SCALE")
def is_trail_exit(e: str): return "TRAIL_EXIT" in e
def is_trail_update(e: str): return e == "TRAIL_UPDATE"
def is_be_arm(e: str): return e == "BE_ARM"

def auto_fix_sl_tp(side: str, price: float | None, sl: float | None, tp: float | None):
    if side not in ("BUY", "SELL") or price is None or sl is None or tp is None:
        return sl, tp
    if side == "BUY" and sl > price and tp < price:
        return tp, sl
    if side == "SELL" and sl < price and tp > price:
        return tp, sl
    return sl, tp

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
    if not LEARNING_MODE:
        return True
    if score is not None and float(score) < MIN_SCORE:
        return False
    b = score_bucket(score)
    if b is None:
        return True
    data = state["learn"].get(symbol, {}).get(side, {}).get(b)
    if not data:
        return True
    w, l = data["w"], data["l"]
    total = w + l
    if total < 10:
        return True
    winrate = w / total
    if b in ("SKIP", "C") and winrate < 0.40:
        return False
    return True

# In-memory state
state = {
    "stats": {},
    "open_trade": {},   # symbol -> trade_id
    "trades": {},       # trade_id -> trade dict
    "learn": {}
}

def tick_by_symbol(symbol: str):
    s = (symbol or "").upper()
    # NQ / MNQ
    if "NQ" in s:
        return 0.25
    # Micro Silver is often "SIL" (sometimes "SIL1!" on TV)
    if "SIL" in s:
        return 0.005
    return DEFAULT_TICK

def be_is_hit(entry: float, exit_price: float, symbol: str):
    tick = tick_by_symbol(symbol)
    eps = tick * BE_EPS_TICKS
    return abs(exit_price - entry) <= eps

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

    # Pine sends strings sometimes; we parse safely
    price = to_float(data.get("price"))

    # NEW Pine fields (scalper)
    entry = to_float(data.get("entry"))
    sl    = to_float(data.get("sl"))
    tp1   = to_float(data.get("tp1"))            # new target field
    be_tr = to_float(data.get("be_trigger"))     # break-even trigger level
    score = to_float(data.get("score"))
    contracts = to_int(data.get("contracts"))

    # Old fields (keep backward compatibility)
    tp_old = to_float(data.get("tp"))
    adds   = to_float(data.get("adds"))
    buyScore  = to_float(data.get("buyScore"))
    sellScore = to_float(data.get("sellScore"))

    side = side_from_payload(data, e)

    # Choose a "score" value:
    # 1) new Pine "score"
    # 2) old buyScore/sellScore
    score_val = score
    if score_val is None:
        score_val = buyScore if side == "BUY" else sellScore if side == "SELL" else None

    score_num, score_grade = grade(score_val)

    if symbol not in state["stats"]:
        state["stats"][symbol] = {"wins": 0, "losses": 0, "be": 0}

    # Choose tp:
    # 1) tp1 from scalper
    # 2) old tp
    tp = tp1 if tp1 is not None else tp_old

    # If ENTRY came without "entry", use "price" as entry
    if entry is None and is_entry(e):
        entry = price

    # auto-fix if needed
    sl, tp = auto_fix_sl_tp(side, entry, sl, tp)

    incoming_trade_id = str(data.get("trade_id", "")).strip() or None

    # ---------------------------------------------------------
    # 1) WATCH
    # ---------------------------------------------------------
    if is_watch(e):
        watch_level = to_float(data.get("watch_level"))
        w_price     = to_float(data.get("w_price"))
        buf_pts     = to_float(data.get("buffer_points"))
        direction = "LONG" if e.endswith("_LONG") else "SHORT"

        msg = (
            f"👀 WATCH {direction}\n"
            f"{symbol} | TF {tf}\n"
            f"Level: {fmt_price(watch_level)}\n"
            f"Price: {fmt_price(w_price)}\n"
            f"Buffer: {fmt_price(buf_pts)}"
        )
        send_telegram(msg)
        return jsonify({"ok": True})

    # ---------------------------------------------------------
    # 2) READY
    # ---------------------------------------------------------
    if is_ready(e):
        watch_level = to_float(data.get("watch_level"))
        w_price     = to_float(data.get("w_price"))
        buf_pts     = to_float(data.get("buffer_points"))
        direction = "LONG" if e.endswith("_LONG") else "SHORT"

        msg = (
            f"⚠️ READY {direction}\n"
            f"{symbol} | TF {tf}\n"
            f"Level: {fmt_price(watch_level)}\n"
            f"Price: {fmt_price(w_price)}\n"
            f"Buffer: {fmt_price(buf_pts)}\n"
            f"Prepare to execute…"
        )
        send_telegram(msg)
        return jsonify({"ok": True})

    # ---------------------------------------------------------
    # 3) ENTRY (ENTRY_BUY / ENTRY_SELL)
    # ---------------------------------------------------------
    if is_entry(e):
        trade_id = incoming_trade_id or f"{symbol}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        state["open_trade"][symbol] = trade_id

        state["trades"][trade_id] = {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "tf": tf,
            "entry": entry,
            "sl": sl,
            "tp": tp,                 # store tp1 as tp for compatibility
            "be_trigger": be_tr,      # new
            "contracts": contracts,   # new
            "adds": int(adds) if adds is not None else 0,
            "score": score_val,
            "be_armed": False,
            "opened_at": now_str(),
            "closed_at": None,
            "exit": None,
            "result": None,
            "exit_reason": None
        }

        warning = ""
        if side == "BUY" and tp is not None and entry is not None and tp <= entry:
            warning = "\n⚠️ TP should be ABOVE entry for BUY"
        if side == "SELL" and tp is not None and entry is not None and tp >= entry:
            warning = "\n⚠️ TP should be BELOW entry for SELL"

        if learn_should_send(symbol, side, score_val):
            text = (
                f"🚨 TAKE {side}\n\n"
                f"{symbol} | TF {tf}\n"
                f"TradeID: {trade_id}\n\n"
                f"Entry: {fmt_price(entry)}\n"
                f"SL: {fmt_price(sl)}\n"
                f"TP1: {fmt_price(tp)}\n"
                f"BE Trigger: {fmt_price(be_tr)}\n"
                f"Score: {score_num} ({score_grade})\n"
                f"Contracts: {contracts if contracts is not None else 'N/A'}\n"
                f"W/L/BE: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}/{state['stats'][symbol]['be']}"
                f"{warning}"
            )
            send_telegram(text)

        return jsonify({"ok": True, "trade_id": trade_id})

    # ---------------------------------------------------------
    # 4) BREAK_EVEN (your Pine sends BREAK_EVEN when BE trigger hit)
    # ---------------------------------------------------------
    if is_break_even(e):
        trade_id = incoming_trade_id or state["open_trade"].get(symbol)
        t = state["trades"].get(trade_id) if trade_id else None
        if t:
            t["be_armed"] = True
            # We DON'T change SL here because Pine can't know your manual move.
            # This is a management instruction alert.
            send_telegram(
                f"🔄 MOVE SL TO BREAK-EVEN\n\n"
                f"{symbol} — {t.get('side','N/A')} | TF {tf}\n"
                f"TradeID: {trade_id}\n\n"
                f"Entry: {fmt_price(t.get('entry'))}\n"
                f"Current Price: {fmt_price(price)}\n"
                f"BE Trigger: {fmt_price(t.get('be_trigger'))}"
            )
            return jsonify({"ok": True, "trade_id": trade_id})

        send_telegram(f"⚠️ BREAK_EVEN received but no open trade found.\n{json.dumps(data, indent=2)}")
        return jsonify({"ok": True, "warning": "be_without_open_trade"})

    # ---------------------------------------------------------
    # 5) TRIM
    # ---------------------------------------------------------
    if is_trim(e):
        trade_id = incoming_trade_id or state["open_trade"].get(symbol)
        t = state["trades"].get(trade_id) if trade_id else None
        if t:
            send_telegram(
                f"💰 TRIM HIT\n\n"
                f"{symbol} — {t.get('side','N/A')} | TF {tf}\n"
                f"TradeID: {trade_id}\n\n"
                f"TP1 reached: {fmt_price(t.get('tp'))}\n"
                f"Current Price: {fmt_price(price)}"
            )
            return jsonify({"ok": True, "trade_id": trade_id})

        send_telegram(f"⚠️ TRIM received but no open trade found.\n{json.dumps(data, indent=2)}")
        return jsonify({"ok": True, "warning": "trim_without_open_trade"})

    # ---------------------------------------------------------
    # 6) STOP_HIT  (close trade + classify LOSS / BE / WIN if you want)
    # ---------------------------------------------------------
    if is_stop_hit(e):
        trade_id = incoming_trade_id or state["open_trade"].get(symbol)
        t = state["trades"].get(trade_id) if trade_id else None

        if not t or t.get("entry") is None or price is None:
            send_telegram(
                f"❌ STOP HIT\n\n"
                f"{symbol} — {side}\n"
                f"TF: {tf}\n"
                f"TradeID: {trade_id or 'N/A'}\n\n"
                f"Exit: {fmt_price(price)}\n"
                f"Result: N/A (no linked ENTRY)"
            )
            return jsonify({"ok": True, "trade_id": trade_id})

        entry_px = float(t["entry"])
        display_side = t.get("side", side)

        # If BE armed and exit is within BE tolerance, count as BE
        if t.get("be_armed") and be_is_hit(entry_px, float(price), symbol):
            state["stats"][symbol]["be"] += 1
            outcome = "BREAKEVEN 🟦"
            win_bool = None
        else:
            state["stats"][symbol]["losses"] += 1
            outcome = "LOSS ❌"
            win_bool = False

        t["closed_at"] = now_str()
        t["exit"] = float(price)
        t["result"] = outcome
        t["exit_reason"] = e

        # learning record
        if win_bool is not None:
            learn_record(symbol, display_side, t.get("score"), win_bool)

        state["open_trade"].pop(symbol, None)

        send_telegram(
            f"❌ STOP HIT\n\n"
            f"{symbol} — {display_side}\n"
            f"TF: {tf}\n"
            f"TradeID: {trade_id}\n\n"
            f"Entry: {fmt_price(entry_px)}\n"
            f"Exit: {fmt_price(price)}\n"
            f"Result: {outcome}\n"
            f"W/L/BE: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}/{state['stats'][symbol]['be']}"
        )
        return jsonify({"ok": True, "trade_id": trade_id})

    # ---------------------------------------------------------
    # Optional: EXIT_TREND_FLIP (close trade with win/loss by entry vs exit)
    # ---------------------------------------------------------
    if is_exit_flip(e):
        trade_id = incoming_trade_id or state["open_trade"].get(symbol)
        t = state["trades"].get(trade_id) if trade_id else None

        if not t or t.get("entry") is None or price is None:
            send_telegram(f"🏁 EXIT (Trend Flip) but no linked trade.\n{json.dumps(data, indent=2)}")
            return jsonify({"ok": True, "trade_id": trade_id})

        entry_px = float(t["entry"])
        display_side = t.get("side", side)

        if display_side == "BUY":
            win_bool = float(price) > entry_px
        else:
            win_bool = float(price) < entry_px

        if win_bool:
            state["stats"][symbol]["wins"] += 1
            outcome = "WIN ✅"
        else:
            # treat as BE if within tolerance
            if be_is_hit(entry_px, float(price), symbol):
                state["stats"][symbol]["be"] += 1
                outcome = "BREAKEVEN 🟦"
                win_bool = None
            else:
                state["stats"][symbol]["losses"] += 1
                outcome = "LOSS ❌"

        t["closed_at"] = now_str()
        t["exit"] = float(price)
        t["result"] = outcome
        t["exit_reason"] = e

        if win_bool is not None:
            learn_record(symbol, display_side, t.get("score"), win_bool)

        state["open_trade"].pop(symbol, None)

        send_telegram(
            f"🏁 EXIT (Trend Flip)\n\n"
            f"{symbol} — {display_side}\n"
            f"TF: {tf}\n"
            f"TradeID: {trade_id}\n\n"
            f"Entry: {fmt_price(entry_px)}\n"
            f"Exit: {fmt_price(price)}\n"
            f"Result: {outcome}\n"
            f"W/L/BE: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}/{state['stats'][symbol]['be']}"
        )
        return jsonify({"ok": True, "trade_id": trade_id})

    # ---------------------------------------------------------
    # Keep your existing SCALE / TRAIL logic (backward compatibility)
    # ---------------------------------------------------------
    if is_scale(e):
        trade_id = incoming_trade_id or state["open_trade"].get(symbol)
        if trade_id and trade_id in state["trades"]:
            state["trades"][trade_id]["adds"] = int(adds) if adds is not None else state["trades"][trade_id].get("adds", 0)
            if learn_should_send(symbol, side, score_val):
                text = (
                    f"📈 SCALE ALERT\n\n"
                    f"{symbol} — {side}\n"
                    f"TF: {tf}\n"
                    f"TradeID: {trade_id}\n\n"
                    f"Price: {fmt_price(price)}\n"
                    f"SL: {fmt_price(sl)}\n"
                    f"TP: {fmt_price(tp)}\n"
                    f"Adds: {int(adds) if adds is not None else 0}\n"
                    f"Score: {score_num} ({score_grade})\n"
                    f"W/L/BE: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}/{state['stats'][symbol]['be']}"
                )
                send_telegram(text)
            return jsonify({"ok": True, "trade_id": trade_id})

        send_telegram(f"⚠️ SCALE received but no open trade found.\n{json.dumps(data, indent=2)}")
        return jsonify({"ok": True, "warning": "scale_without_open_trade"})

    if is_trail_update(e):
        trade_id = incoming_trade_id or state["open_trade"].get(symbol)
        t = state["trades"].get(trade_id) if trade_id else None
        if t:
            if sl is not None:
                t["sl"] = sl
            return jsonify({"ok": True, "trade_id": trade_id})
        return jsonify({"ok": True, "warning": "trail_update_without_trade"})

    if is_trail_exit(e):
        trade_id = incoming_trade_id or state["open_trade"].get(symbol)
        t = state["trades"].get(trade_id) if trade_id else None

        if not t or t.get("entry") is None or price is None:
            send_telegram(
                f"🏁 TRAIL EXIT\n\n"
                f"{symbol} — {side}\n"
                f"TF: {tf}\n"
                f"TradeID: {trade_id or 'N/A'}\n\n"
                f"Exit: {fmt_price(price)}\n"
                f"Result: N/A (no linked ENTRY)\n"
                f"W/L/BE: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}/{state['stats'][symbol]['be']}"
            )
            return jsonify({"ok": True, "trade_id": trade_id})

        entry_px = float(t["entry"])
        display_side = t.get("side", side)

        if be_is_hit(entry_px, float(price), symbol):
            state["stats"][symbol]["be"] += 1
            outcome = "BREAKEVEN 🟦"
            win_bool = None
        else:
            if display_side == "BUY":
                win_bool = float(price) > entry_px
            else:
                win_bool = float(price) < entry_px

            if win_bool:
                state["stats"][symbol]["wins"] += 1
                outcome = "WIN ✅"
            else:
                state["stats"][symbol]["losses"] += 1
                outcome = "LOSS ❌"

        t["closed_at"] = now_str()
        t["exit"] = float(price)
        t["result"] = outcome
        t["exit_reason"] = e

        if win_bool is not None:
            learn_record(symbol, display_side, t.get("score"), win_bool)

        state["open_trade"].pop(symbol, None)

        send_telegram(
            f"🏁 TRAIL EXIT\n\n"
            f"{symbol} — {display_side}\n"
            f"TF: {tf}\n"
            f"TradeID: {trade_id}\n\n"
            f"Entry: {fmt_price(entry_px)}\n"
            f"Exit: {fmt_price(price)}\n"
            f"Result: {outcome}\n"
            f"W/L/BE: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}/{state['stats'][symbol]['be']}"
        )
        return jsonify({"ok": True, "trade_id": trade_id})

    # ---------------------------------------------------------
    # Unknown event fallback
    # ---------------------------------------------------------
    send_telegram(f"⚠️ Unknown event\n{json.dumps(data, indent=2)}")
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
