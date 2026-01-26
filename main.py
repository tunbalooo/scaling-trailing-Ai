import os, json, time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATS_FILE = "stats.json"

# ---------------- Telegram ----------------
def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}

    try:
        r = requests.post(url, json=payload, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------- Helpers ----------------
def safe_str(x, default="N/A"):
    if x is None:
        return default
    s = str(x).strip()
    if s == "" or s.lower() in ("na", "n/a", "null", "none"):
        return default
    return s

def safe_float_str(x, nd=2, default="N/A"):
    s = safe_str(x, default=default)
    if s == default:
        return default
    try:
        return f"{float(s):.{nd}f}"
    except:
        return s

def grade(score):
    try:
        s = float(score)
    except:
        return "N/A"
    return "A" if s >= 80 else "B" if s >= 65 else "C" if s >= 50 else "SKIP"

def parse_payload():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    raw = request.data.decode("utf-8", errors="ignore").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except:
        return {"_raw": raw}

# ---------------- Stats ----------------
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"overall": {"wins": 0, "losses": 0}, "by_key": {}}

def save_stats(s):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)

def inc_result(symbol, session, result):  # result = "WIN" or "LOSS"
    s = load_stats()
    key = f"{symbol}||{session}"

    if key not in s["by_key"]:
        s["by_key"][key] = {"wins": 0, "losses": 0}

    if result == "WIN":
        s["overall"]["wins"] += 1
        s["by_key"][key]["wins"] += 1
    else:
        s["overall"]["losses"] += 1
        s["by_key"][key]["losses"] += 1

    save_stats(s)
    return s

# ---------------- Routes ----------------
@app.route("/", methods=["GET"])
def home():
    return "Scaling & Trailing AI is LIVE", 200

@app.route("/test-telegram", methods=["GET"])
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.route("/stats", methods=["GET"])
def stats():
    return jsonify(load_stats()), 200

# Optional manual save (if you ever want to force a WIN/LOSS)
@app.route("/save-outcome", methods=["POST"])
def save_outcome():
    data = parse_payload()
    symbol  = safe_str(data.get("symbol"))
    session = safe_str(data.get("session", "ALL"))
    result  = safe_str(data.get("result")).upper()
    if result not in ("WIN", "LOSS"):
        return jsonify({"ok": False, "error": "result must be WIN or LOSS"}), 400

    s = inc_result(symbol, session, result)
    send_telegram(f"📌 OUTCOME SAVED\n{symbol} ({session})\nResult: {result}\nOverall: {s['overall']}")
    return jsonify({"ok": True, "stats": s}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = parse_payload()

    if "_raw" in data:
        send_telegram("⚠️ NON-JSON ALERT BODY:\n\n" + data["_raw"][:800])
        return jsonify({"ok": True, "note": "non-json body"}), 200

    event = data.get("event", {})
    if not isinstance(event, dict):
        event = {}

    typ       = safe_str(event.get("type", "ENTRY")).upper()
    direction = safe_str(event.get("side", "N/A")).upper()

    symbol  = safe_str(data.get("symbol"))
    tf      = safe_str(data.get("tf"))
    session = safe_str(data.get("session", "N/A"))

    entry = safe_float_str(data.get("entry"), 2, default="N/A")
    price = safe_float_str(data.get("price"), 2, default="N/A")

    sl = safe_float_str(data.get("sl"), 2, default="N/A")
    tp = safe_float_str(data.get("tp"), 2, default="N/A")

    # NEW: break-even + trail stop levels (optional)
    be_sl     = safe_float_str(data.get("be_sl"), 2, default="N/A")
    trail_sl  = safe_float_str(data.get("trail_sl"), 2, default="N/A")

    adds = safe_float_str(data.get("adds"), 0, default="0")

    buyScore  = safe_str(data.get("buyScore"))
    sellScore = safe_str(data.get("sellScore"))
    score = buyScore if direction == "BUY" else sellScore

    score_num = safe_float_str(score, 0, default="N/A")
    g = grade(score)

    # NEW: outcome labeling (your Pine/logic can send this on exit)
    outcome = safe_str(data.get("outcome", "")).upper()  # WIN / LOSS / ""

    header = "📊 TRADE ALERT"
    if typ == "SCALE":
        header = "➕ SCALE ALERT"
    elif typ == "TRAIL":
        header = "🏁 TRAIL EXIT"
    elif typ == "BE":
        header = "🟡 MOVE SL TO BE"
    elif typ == "TRAIL_SL":
        header = "🟢 TRAIL SL UPDATE"

    shown_entry = entry if entry != "N/A" else price

    msg = (
        f"{header}\n\n"
        f"{symbol} — {direction}\n"
        f"Type: {typ}\n"
        f"TF: {tf}\n"
        f"Session: {session}\n\n"
        f"Entry: {shown_entry}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"BE SL: {be_sl}\n"
        f"Trail SL: {trail_sl}\n"
        f"Adds: {adds}\n"
        f"Score: {score_num} ({g})\n"
    )

    # If this alert includes WIN/LOSS, update counters + include totals
    if outcome in ("WIN", "LOSS"):
        s = inc_result(symbol, session, outcome)
        msg += f"\nResult: {outcome}\nOverall W/L: {s['overall']['wins']}/{s['overall']['losses']}\n"

        key = f"{symbol}||{session}"
        wk = s["by_key"].get(key, {"wins": 0, "losses": 0})
        msg += f"{symbol} {session} W/L: {wk['wins']}/{wk['losses']}\n"

    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "telegram": r}), (200 if r.get("ok") else 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
