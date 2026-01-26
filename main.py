import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# In-memory storage (resets if Railway restarts)
last_trade = {}  # key: symbol -> dict(entry, side, tf, time)
stats = {
    "wins": 0,
    "losses": 0,
    "total": 0
}

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text})
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": "Telegram response not JSON", "raw": r.text}

def to_float(x):
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s.lower() in ("na", "n/a", "null", ""):
            return None
        return float(s)
    except Exception:
        return None

def fmt(x, nd=2):
    v = to_float(x)
    if v is None:
        return "N/A"
    return f"{v:.{nd}f}"

def build_message(payload: dict) -> str:
    event = payload.get("event", {}) if isinstance(payload.get("event", {}), dict) else {}
    typ = str(event.get("type", "ENTRY")).upper()
    side = str(event.get("side", "N/A")).upper()

    symbol = str(payload.get("symbol", "N/A"))
    tf     = str(payload.get("tf", "N/A"))

    price = to_float(payload.get("price"))
    sl    = to_float(payload.get("sl"))
    tp    = to_float(payload.get("tp"))
    score = to_float(payload.get("score"))
    adds  = payload.get("adds", 0)

    # Grade for display
    grade = "N/A"
    if score is not None:
        if score >= 80: grade = "A"
        elif score >= 65: grade = "B"
        elif score >= 50: grade = "C"
        else: grade = "SKIP"

    lines = []
    if typ == "TRAIL":
        lines.append("🏁 TRAIL EXIT")
    elif typ == "SCALE":
        lines.append("➕ SCALE ALERT")
    else:
        lines.append("📊 TRADE ALERT")

    lines.append("")
    lines.append(f"{symbol} — {side}")
    lines.append(f"Type: {typ}")
    lines.append(f"TF: {tf}")
    lines.append("")
    lines.append(f"Entry/Price: {fmt(price, 2)}")
    lines.append(f"SL: {fmt(sl, 2)}")
    lines.append(f"TP: {fmt(tp, 2)}")
    lines.append(f"Adds: {adds}")
    if score is None:
        lines.append(f"Score: N/A (N/A)")
    else:
        lines.append(f"Score: {int(score)} ({grade})")

    return "\n".join(lines)

def label_outcome(symbol: str, exit_price: float, exit_side: str):
    """
    Uses last stored ENTRY for that symbol.
    WIN logic:
      - If last entry was BUY, WIN if exit_price > entry_price
      - If last entry was SELL, WIN if exit_price < entry_price
    """
    t = last_trade.get(symbol)
    if not t:
        return None

    entry_price = t["entry_price"]
    entry_side  = t["side"]

    if entry_side == "BUY":
        win = exit_price > entry_price
    elif entry_side == "SELL":
        win = exit_price < entry_price
    else:
        return None

    stats["total"] += 1
    if win:
        stats["wins"] += 1
        result = "WIN ✅"
    else:
        stats["losses"] += 1
        result = "LOSS ❌"

    return {
        "result": result,
        "entry_side": entry_side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "symbol": symbol
    }

@app.get("/")
def home():
    return jsonify({"ok": True, "message": "Webhook is running. Use POST /webhook"})

@app.get("/test")
def test():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.get("/stats")
def get_stats():
    return jsonify(stats)

@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True)

    if payload is None:
        # If TradingView sent text instead of JSON
        raw = request.data.decode("utf-8", errors="ignore")
        send_telegram("⚠️ NON-JSON ALERT BODY:\n" + raw[:1800])
        return jsonify({"ok": False, "error": "Non-JSON body"}), 400

    # Parse event
    event = payload.get("event", {}) if isinstance(payload.get("event", {}), dict) else {}
    typ = str(event.get("type", "ENTRY")).upper()
    side = str(event.get("side", "N/A")).upper()

    symbol = str(payload.get("symbol", "N/A"))
    price  = to_float(payload.get("price"))

    # Store ENTRY as last trade (for win/loss labeling on TRAIL)
    if typ == "ENTRY" and price is not None and side in ("BUY", "SELL"):
        last_trade[symbol] = {
            "side": side,
            "entry_price": price,
            "time": datetime.utcnow().isoformat()
        }

    # If TRAIL, label outcome
    if typ == "TRAIL" and price is not None:
        outcome = label_outcome(symbol, price, side)
        if outcome:
            msg = (
                f"📌 OUTCOME SAVED\n\n"
                f"{outcome['symbol']} ({outcome['entry_side']})\n"
                f"Entry: {outcome['entry_price']:.2f}  Exit: {outcome['exit_price']:.2f}\n"
                f"Result: {outcome['result']}\n\n"
                f"Totals — Wins: {stats['wins']} | Losses: {stats['losses']} | Total: {stats['total']}"
            )
            send_telegram(msg)

    # Always send the alert summary
    msg = build_message(payload)
    send_telegram(msg)

    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
