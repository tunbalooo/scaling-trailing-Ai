import os
import json
import csv
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

import requests
from flask import Flask, request, jsonify

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
DATA_DIR = "data"
STATE_JSON = os.path.join(DATA_DIR, "trades.json")
TRADES_CSV = os.path.join(DATA_DIR, "trades.csv")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

os.makedirs(DATA_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def fnum(x) -> Optional[float]:
    """Convert TradingView strings/nulls to float or None."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().lower()
    if s in ("null", "na", "none", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None

def snum(x) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, int):
        return x
    s = str(x).strip().lower()
    if s in ("null", "na", "none", ""):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None

def now_ts() -> int:
    return int(time.time())

def send_telegram(text: str) -> None:
    if not SEND_TELEGRAM:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# Trade model
# ─────────────────────────────────────────────────────────────
@dataclass
class Trade:
    trade_id: str
    symbol: str
    tf: str
    side: str                 # "BUY" or "SELL"
    status: str               # "OPEN" or "CLOSED"

    entry_price: float
    entry_time: int

    sl: Optional[float] = None
    tp: Optional[float] = None

    adds: int = 0
    buyScore: Optional[float] = None
    sellScore: Optional[float] = None

    tp1_hit: bool = False
    be_armed: bool = False    # moved to break-even already or armed
    trail_active: bool = False

    exit_price: Optional[float] = None
    exit_time: Optional[int] = None
    exit_event: Optional[str] = None
    result: Optional[str] = None  # "WIN" or "LOSS"
    pnl_points: Optional[float] = None

# ─────────────────────────────────────────────────────────────
# State (in-memory + persisted)
# ─────────────────────────────────────────────────────────────
state: Dict[str, Any] = {
    "open_trades": {},   # key -> Trade dict
    "closed_trades": [], # list of Trade dict
    "wins": 0,
    "losses": 0,
}

def make_key(symbol: str, tf: str, side: str) -> str:
    return f"{symbol}|{tf}|{side}"

def load_state():
    global state
    if os.path.exists(STATE_JSON):
        try:
            with open(STATE_JSON, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

def save_state():
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def append_csv_row(trade: Trade):
    file_exists = os.path.exists(TRADES_CSV)
    headers = list(asdict(trade).keys())
    row = asdict(trade)

    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

# ─────────────────────────────────────────────────────────────
# Core logic
# ─────────────────────────────────────────────────────────────
def open_trade(payload: Dict[str, Any], side: str) -> Trade:
    symbol = str(payload.get("symbol", ""))
    tf = str(payload.get("tf", ""))
    price = fnum(payload.get("price")) or 0.0

    trade = Trade(
        trade_id=str(uuid.uuid4())[:8],
        symbol=symbol,
        tf=tf,
        side=side,
        status="OPEN",
        entry_price=price,
        entry_time=now_ts(),
        sl=fnum(payload.get("sl")),
        tp=fnum(payload.get("tp")),
        adds=snum(payload.get("adds")) or 0,
        buyScore=fnum(payload.get("buyScore")),
        sellScore=fnum(payload.get("sellScore")),
    )
    key = make_key(symbol, tf, side)
    state["open_trades"][key] = asdict(trade)
    save_state()
    return trade

def get_open_trade(payload: Dict[str, Any], side: str) -> Optional[Trade]:
    key = make_key(str(payload.get("symbol","")), str(payload.get("tf","")), side)
    t = state["open_trades"].get(key)
    return Trade(**t) if t else None

def update_open_trade(trade: Trade):
    key = make_key(trade.symbol, trade.tf, trade.side)
    state["open_trades"][key] = asdict(trade)
    save_state()

def close_trade(trade: Trade, exit_event: str, exit_price: float) -> Trade:
    trade.status = "CLOSED"
    trade.exit_event = exit_event
    trade.exit_price = exit_price
    trade.exit_time = now_ts()

    # PnL points
    if trade.side == "BUY":
        trade.pnl_points = exit_price - trade.entry_price
        trade.result = "WIN" if trade.pnl_points > 0 else "LOSS"
    else:
        trade.pnl_points = trade.entry_price - exit_price
        trade.result = "WIN" if trade.pnl_points > 0 else "LOSS"

    # Update counters
    if trade.result == "WIN":
        state["wins"] = int(state.get("wins", 0)) + 1
    else:
        state["losses"] = int(state.get("losses", 0)) + 1

    # Move to closed
    key = make_key(trade.symbol, trade.tf, trade.side)
    state["open_trades"].pop(key, None)
    state["closed_trades"].append(asdict(trade))
    save_state()
    append_csv_row(trade)
    return trade

# ─────────────────────────────────────────────────────────────
# Message formatting
# ─────────────────────────────────────────────────────────────
def grade(score: Optional[float]) -> str:
    if score is None:
        return "N/A"
    s = float(score)
    return "A" if s >= 80 else "B" if s >= 65 else "C" if s >= 50 else "SKIP"

def build_entry_msg(trade: Trade, event: str) -> str:
    score = trade.buyScore if trade.side == "BUY" else trade.sellScore
    return (
        f"📊 TRADE ALERT\n\n"
        f"{trade.symbol} — {trade.side}\n"
        f"Type: {event}\n"
        f"TF: {trade.tf}\n\n"
        f"Entry: {trade.entry_price}\n"
        f"SL: {trade.sl}\n"
        f"TP: {trade.tp}\n"
        f"Adds: {trade.adds}\n"
        f"Score: {score} ({grade(score)})\n"
        f"W/L: {state.get('wins',0)}/{state.get('losses',0)}\n"
        f"TradeID: {trade.trade_id}"
    )

def build_scale_msg(trade: Trade, event: str) -> str:
    return (
        f"➕ SCALE\n\n"
        f"{trade.symbol} — {trade.side}\n"
        f"Type: {event}\n"
        f"TF: {trade.tf}\n\n"
        f"Price: {fnum(trade.entry_price)}\n"
        f"Adds: {trade.adds}\n"
        f"W/L: {state.get('wins',0)}/{state.get('losses',0)}\n"
        f"TradeID: {trade.trade_id}"
    )

def build_exit_msg(trade: Trade) -> str:
    icon = "✅" if trade.result == "WIN" else "❌"
    return (
        f"🏁 EXIT ({trade.exit_event})\n\n"
        f"{trade.symbol} — {trade.side}\n"
        f"TF: {trade.tf}\n\n"
        f"Entry: {trade.entry_price}\n"
        f"Exit: {trade.exit_price}\n"
        f"Result: {trade.result} {icon}\n"
        f"W/L: {state.get('wins',0)}/{state.get('losses',0)}\n"
        f"TradeID: {trade.trade_id}"
    )

# ─────────────────────────────────────────────────────────────
# Flask webhook
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
load_state()

@app.post("/webhook")
def webhook():
    payload = request.get_json(force=True, silent=True) or {}
    event = str(payload.get("event", "")).upper().strip()

    # Normalize side based on event name
    if "BUY" in event:
        side = "BUY"
    elif "SELL" in event:
        side = "SELL"
    else:
        # TRAIL_LONG / TRAIL_SHORT still implies side by your system:
        # If you want, we can send explicit side from Pine.
        side = "BUY" if event == "TRAIL_LONG" else "SELL" if event == "TRAIL_SHORT" else "N/A"

    price = fnum(payload.get("price"))

    # ENTRY
    if event in ("ENTRY_BUY", "ENTRY_SELL"):
        trade = open_trade(payload, side)
        send_telegram(build_entry_msg(trade, "ENTRY"))
        return jsonify({"ok": True, "trade_id": trade.trade_id})

    # SCALE
    if event in ("SCALE_BUY", "SCALE_SELL"):
        trade = get_open_trade(payload, side)
        if not trade:
            # no open trade found -> ignore or open new (we ignore to avoid bad linking)
            return jsonify({"ok": False, "error": "No open trade to scale"}), 400
        trade.adds = snum(payload.get("adds")) or (trade.adds + 1)
        # update scores / sl/tp if provided
        trade.sl = fnum(payload.get("sl")) or trade.sl
        trade.tp = fnum(payload.get("tp")) or trade.tp
        trade.buyScore = fnum(payload.get("buyScore")) or trade.buyScore
        trade.sellScore = fnum(payload.get("sellScore")) or trade.sellScore
        update_open_trade(trade)
        send_telegram(build_scale_msg(trade, "SCALE"))
        return jsonify({"ok": True, "trade_id": trade.trade_id})

    # TP1 (optional event)
    if event in ("TP1_BUY", "TP1_SELL"):
        trade = get_open_trade(payload, side)
        if not trade:
            return jsonify({"ok": False, "error": "No open trade for TP1"}), 400
        trade.tp1_hit = True
        trade.be_armed = True
        trade.trail_active = True
        update_open_trade(trade)
        send_telegram(f"🎯 TP1 HIT\n{trade.symbol} {trade.side}\nTradeID: {trade.trade_id}\nBE armed ✅ Trail on ✅")
        return jsonify({"ok": True, "trade_id": trade.trade_id})

    # TRAIL EXIT (we treat as final exit)
    if event in ("TRAIL_LONG", "TRAIL_SHORT"):
        # infer side for trail events:
        trail_side = "BUY" if event == "TRAIL_LONG" else "SELL"
        trade = get_open_trade(payload, trail_side)
        if not trade:
            return jsonify({"ok": False, "error": "No open trade to exit"}), 400
        if price is None:
            return jsonify({"ok": False, "error": "Missing price for exit"}), 400
        trade = close_trade(trade, event, price)
        send_telegram(build_exit_msg(trade))
        return jsonify({"ok": True, "trade_id": trade.trade_id, "result": trade.result})

    return jsonify({"ok": False, "error": f"Unknown event: {event}"}), 400


@app.get("/")
def home():
    return "OK"

if __name__ == "__main__":
    # Railway uses PORT env var
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
