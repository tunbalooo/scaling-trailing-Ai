import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# In-memory state (Railway restarts will reset this)
state = {
    "last_entry": {},   # symbol -> {"side": "BUY/SELL", "entry": float, "sl": float|None, "tp": float|None, "time": str}
    "stats": {}         # symbol -> {"wins": int, "losses": int}
}

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

def normalize_side(event: str, side_plot: float | None):
    e = (event or "").upper()
    if "BUY" in e:
        return "BUY"
    if "SELL" in e:
        return "SELL"
    # fallback from plot_5 (posDir): 1 buy, -1 sell
    if side_plot is not None:
        if side_plot > 0:
            return "BUY"
        if side_plot < 0:
            return "SELL"
    return "N/A"

def fmt_price(x):
    return "N/A" if x is None else f"{x:.2f}"

@app.get("/")
def home():
    return "OK"

@app.post("/webhook")
def webhook():
    # Ensure JSON
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Non-JSON body"}), 400

    event  = str(data.get("event", "")).strip()
    symbol = str(data.get("symbol", "N/A")).strip()
    tf     = str(data.get("tf", "N/A")).strip()

    price = to_float(data.get("price"))
    sl    = to_float(data.get("sl"))
    tp    = to_float(data.get("tp"))
    adds  = to_float(data.get("adds"))
    side_plot = to_float(data.get("side"))

    buyScore  = to_float(data.get("buyScore"))
    sellScore = to_float(data.get("sellScore"))

    side = normalize_side(event, side_plot)

    # choose the right score by side
    score_val = buyScore if side == "BUY" else sellScore if side == "SELL" else None
    score_num, score_grade = grade(score_val)

    # Init stats
    if symbol not in state["stats"]:
        state["stats"][symbol] = {"wins": 0, "losses": 0}

    # Store entries so we can label TRAIL as win/loss later
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Entry or Scale events
    if event.upper().startswith("ENTRY"):
        state["last_entry"][symbol] = {
            "side": side,
            "entry": price,
            "sl": sl,
            "tp": tp,
            "time": now
        }

        warning = ""
        if side == "BUY" and tp is not None and price is not None and tp <= price:
            warning = "\n⚠️ TP should be ABOVE entry for BUY"
        if side == "SELL" and tp is not None and price is not None and tp >= price:
            warning = "\n⚠️ TP should be BELOW entry for SELL"

        text = (
            f"📊 TRADE ALERT\n\n"
            f"{symbol} — {side}\n"
            f"Type: ENTRY\n"
            f"TF: {tf}\n\n"
            f"Entry: {fmt_price(price)}\n"
            f"SL: {fmt_price(sl)}\n"
            f"TP: {fmt_price(tp)}\n"
            f"Adds: {int(adds) if adds is not None else 0}\n"
            f"Score: {score_num} ({score_grade})\n"
            f"W/L: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}"
            f"{warning}"
        )
        send_telegram(text)
        return jsonify({"ok": True})

    if event.upper().startswith("SCALE"):
        text = (
            f"📈 SCALE ALERT\n\n"
            f"{symbol} — {side}\n"
            f"Type: SCALE\n"
            f"TF: {tf}\n\n"
            f"Price: {fmt_price(price)}\n"
            f"SL: {fmt_price(sl)}\n"
            f"TP: {fmt_price(tp)}\n"
            f"Adds: {int(adds) if adds is not None else 0}\n"
            f"Score: {score_num} ({score_grade})\n"
            f"W/L: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}"
        )
        send_telegram(text)
        return jsonify({"ok": True})

    # Trail exit = close trade + update W/L
    if event.upper().startswith("TRAIL"):
        last = state["last_entry"].get(symbol)
        outcome = "N/A"

        if last and last.get("entry") is not None and price is not None:
            entry = float(last["entry"])
            last_side = last.get("side", "N/A")

            if last_side == "BUY":
                win = price > entry
            elif last_side == "SELL":
                win = price < entry
            else:
                win = None

            if win is True:
                state["stats"][symbol]["wins"] += 1
                outcome = "WIN ✅"
            elif win is False:
                state["stats"][symbol]["losses"] += 1
                outcome = "LOSS ❌"

        text = (
            f"🏁 TRAIL EXIT\n\n"
            f"{symbol} — {side}\n"
            f"Type: TRAIL\n"
            f"TF: {tf}\n\n"
            f"Exit: {fmt_price(price)}\n"
            f"Result: {outcome}\n"
            f"W/L: {state['stats'][symbol]['wins']}/{state['stats'][symbol]['losses']}"
        )
        send_telegram(text)
        return jsonify({"ok": True})

    # Unknown event fallback
    send_telegram(f"⚠️ Unknown event\n{json.dumps(data, indent=2)}")
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
