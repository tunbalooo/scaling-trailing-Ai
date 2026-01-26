import os, json, time, math, sqlite3
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DB_PATH   = os.getenv("DB_PATH", "trades.db")

# ─────────────────────────────
# Telegram
# ─────────────────────────────
def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        return {"ok": r.ok, "status": r.status_code, "text": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def fnum(x, nd=2):
    try:
        return round(float(x), nd)
    except Exception:
        return None

# ─────────────────────────────
# Session labeling (UTC-based)
# We cannot reliably know your exchange timezone from TV webhooks,
# so we do a clean UTC session bucket. It still works well.
#
# Asia   : 00:00–06:59 UTC
# London : 07:00–12:29 UTC
# NY     : 12:30–20:00 UTC
# Other  : rest
# ─────────────────────────────
def session_bucket(ts: int) -> str:
    g = time.gmtime(ts)
    mins = g.tm_hour * 60 + g.tm_min
    if 0 <= mins <= 419:
        return "ASIA"
    if 420 <= mins <= 749:
        return "LONDON"
    if 750 <= mins <= 1200:
        return "NY"
    return "OTHER"

# ─────────────────────────────
# DB
# ─────────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER,
      session TEXT,
      symbol TEXT,
      tf TEXT,
      side TEXT,          -- BUY/SELL
      typ TEXT,           -- ENTRY/SCALE/TRAIL/OUTCOME
      price REAL,
      sl REAL,
      tp REAL,
      score REAL,
      adds REAL,
      rr REAL,
      sl_dist REAL,
      tp_dist REAL,
      label INTEGER       -- 1=WIN, 0=LOSS, NULL=unknown
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS models (
      key TEXT PRIMARY KEY,     -- "NQ1|NY" etc
      w0 REAL, w1 REAL, w2 REAL, w3 REAL, w4 REAL,
      n INTEGER
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ─────────────────────────────
# Online Logistic Regression (SGD) PER (symbol|session)
# Features:
# x0=1
# x1=score_norm (0..1)
# x2=rr_clipped (0..3)
# x3=sl_dist_norm (sl_dist/price)
# x4=side (BUY=1, SELL=0)
# ─────────────────────────────
def sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    else:
        ez = math.exp(z)
        return ez / (1.0 + ez)

def model_key(symbol: str, sess: str) -> str:
    return f"{symbol}|{sess}"

def load_model(key: str):
    conn = db()
    row = conn.execute("SELECT w0,w1,w2,w3,w4,n FROM models WHERE key=?", (key,)).fetchone()
    if not row:
        conn.execute("INSERT INTO models (key,w0,w1,w2,w3,w4,n) VALUES (?,?,?,?,?,?,?)",
                     (key, 0.0,0.0,0.0,0.0,0.0, 0))
        conn.commit()
        row = (0.0,0.0,0.0,0.0,0.0,0)
    conn.close()
    w = [float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])]
    n = int(row[5])
    return w, n

def save_model(key: str, w, n):
    conn = db()
    conn.execute(
        "UPDATE models SET w0=?,w1=?,w2=?,w3=?,w4=?,n=? WHERE key=?",
        (w[0], w[1], w[2], w[3], w[4], n, key)
    )
    conn.commit()
    conn.close()

def features(side, price, sl, tp, score):
    if price is None or sl is None or tp is None or score is None:
        return None
    if price <= 0:
        return None

    sl_dist = abs(price - sl)
    tp_dist = abs(tp - price)
    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0

    score_norm = max(0.0, min(1.0, score / 100.0))
    rr_clip    = max(0.0, min(3.0, rr))
    sl_norm    = max(0.0, min(0.02, sl_dist / price))
    side_bin   = 1.0 if side == "BUY" else 0.0

    x = [1.0, score_norm, rr_clip, sl_norm, side_bin]
    return x, rr, sl_dist, tp_dist

def predict_proba(key: str, x):
    w, n = load_model(key)
    z = sum(wi*xi for wi, xi in zip(w, x))
    p = sigmoid(z)
    return p, n

def update_model(key: str, x, y, lr=0.25):
    w, n = load_model(key)
    p = sigmoid(sum(wi*xi for wi, xi in zip(w, x)))
    step = lr * (y - p)
    for i in range(len(w)):
        w[i] += step * x[i]
    n += 1
    save_model(key, w, n)
    return p, n

# ─────────────────────────────
# Trade linking helpers
# ─────────────────────────────
def latest_unlabeled_entry(symbol: str, tf: str, sess: str):
    conn = db()
    row = conn.execute("""
        SELECT id, side, price, sl, tp, score, session
        FROM trades
        WHERE symbol=? AND tf=? AND typ='ENTRY' AND label IS NULL AND session=?
        ORDER BY id DESC
        LIMIT 1
    """, (symbol, tf, sess)).fetchone()
    conn.close()
    return row

def mark_entry_label(entry_id: int, label: int):
    conn = db()
    conn.execute("UPDATE trades SET label=? WHERE id=?", (label, entry_id))
    conn.commit()
    conn.close()

# ─────────────────────────────
# Routes
# ─────────────────────────────
@app.get("/")
def home():
    return jsonify({"status": "live", "endpoints": ["/webhook", "/test-telegram"]})

@app.get("/test-telegram")
def test_telegram():
    r = send_telegram("✅ Telegram test successful")
    return jsonify(r), (200 if r.get("ok") else 500)

@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}

    # Parse event
    event = data.get("event", {})
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except Exception:
            event = {}

    typ = str(event.get("type", "ENTRY")).upper()
    side = str(event.get("side", "N/A")).upper()
    result = str(event.get("result", "")).upper()  # WIN/LOSS for OUTCOME

    symbol = str(data.get("symbol", "N/A"))
    tf     = str(data.get("tf", "N/A"))

    ts = int(time.time())
    sess = session_bucket(ts)

    price = fnum(data.get("price"), 2)
    sl    = fnum(data.get("sl"), 2)
    tp    = fnum(data.get("tp"), 2)
    adds  = fnum(data.get("adds"), 0)

    buyScore  = fnum(data.get("buyScore"), 0)
    sellScore = fnum(data.get("sellScore"), 0)
    score = buyScore if side == "BUY" else sellScore

    # Clamp score to 0..100 (prevents crazy values)
    if score is not None:
        score = max(0.0, min(100.0, float(score)))

    # ─────────────────────────────
    # D) Label TRAIL exits automatically:
    # If TRAIL event arrives, we compare TRAIL exit price vs ENTRY price
    # BUY: exit > entry => WIN else LOSS
    # SELL: exit < entry => WIN else LOSS
    #
    # We label the most recent unlabeled ENTRY in SAME symbol/tf/session.
    # ─────────────────────────────
    if typ == "TRAIL":
        row = latest_unlabeled_entry(symbol, tf, sess)
        if row and price is not None:
            entry_id, entry_side, entry_price, entry_sl, entry_tp, entry_score, entry_sess = row
            entry_side = str(entry_side).upper()
            entry_price = float(entry_price) if entry_price is not None else None

            if entry_price is not None:
                if entry_side == "BUY":
                    label = 1 if price > entry_price else 0
                else:
                    label = 1 if price < entry_price else 0

                # mark label on ENTRY
                mark_entry_label(entry_id, label)

                # update model using entry values
                key = model_key(symbol, sess)
                feat = features(entry_side, entry_price, float(entry_sl), float(entry_tp), float(entry_score))
                if feat:
                    x, rr, sl_dist, tp_dist = feat
                    p_before, n_before = predict_proba(key, x)
                    update_model(key, x, label)

                    send_telegram(
                        f"🏁 TRAIL LABELED ({sess})\n\n"
                        f"{symbol} ({tf}) {entry_side}\n"
                        f"Entry: {round(entry_price,2)}  Exit: {price}\n"
                        f"Result: {'WIN ✅' if label==1 else 'LOSS ❌'}\n"
                        f"Model: {key}\nPrev P(win): {p_before:.2f} | Samples: {n_before}"
                    )

        # still store the TRAIL event itself (for history)
        conn = db()
        conn.execute("""
            INSERT INTO trades (ts,session,symbol,tf,side,typ,price,sl,tp,score,adds,rr,sl_dist,tp_dist,label)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
        """, (ts, sess, symbol, tf, side, typ, price, sl, tp, score, adds, None, None, None))
        conn.commit()
        conn.close()

        return jsonify({"ok": True, "type": "TRAIL", "session": sess}), 200

    # OUTCOME events still supported if you use TP HIT / SL HIT alerts
    if typ == "OUTCOME":
        label = 1 if result == "WIN" else 0 if result == "LOSS" else None
        row = latest_unlabeled_entry(symbol, tf, sess)
        if row and label is not None:
            entry_id, entry_side, entry_price, entry_sl, entry_tp, entry_score, entry_sess = row
            mark_entry_label(entry_id, label)

            key = model_key(symbol, sess)
            feat = features(str(entry_side).upper(), float(entry_price), float(entry_sl), float(entry_tp), float(entry_score))
            if feat:
                x, rr, sl_dist, tp_dist = feat
                p_before, n_before = predict_proba(key, x)
                update_model(key, x, label)

                send_telegram(
                    f"📌 OUTCOME SAVED ({sess})\n\n{symbol} ({tf})\n"
                    f"Result: {'WIN ✅' if label==1 else 'LOSS ❌'}\n"
                    f"Model: {key}\nPrev P(win): {p_before:.2f} | Samples: {n_before}"
                )

        # store OUTCOME event
        conn = db()
        conn.execute("""
            INSERT INTO trades (ts,session,symbol,tf,side,typ,price,sl,tp,score,adds,rr,sl_dist,tp_dist,label)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ts, sess, symbol, tf, side, typ, price, sl, tp, score, adds, None, None, None, label))
        conn.commit()
        conn.close()

        return jsonify({"ok": True, "type": "OUTCOME", "session": sess}), 200

    # ENTRY/SCALE normal storage + session-aware ML prediction on ENTRY
    rr = sl_dist = tp_dist = None
    p = None
    samples = 0
    key = model_key(symbol, sess)

    if typ == "ENTRY" and side in ("BUY", "SELL"):
        feat = features(side, price, sl, tp, score)
        if feat:
            x, rr, sl_dist, tp_dist = feat
            p, samples = predict_proba(key, x)

    # store event
    conn = db()
    conn.execute("""
        INSERT INTO trades (ts,session,symbol,tf,side,typ,price,sl,tp,score,adds,rr,sl_dist,tp_dist,label)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
    """, (ts, sess, symbol, tf, side, typ, price, sl, tp, score, adds, rr, sl_dist, tp_dist))
    conn.commit()
    conn.close()

    # Telegram message
    header = "📊 TRADE ALERT"
    if typ == "SCALE": header = "➕ SCALE ALERT"
    if typ == "TRAIL": header = "🏁 TRAIL EXIT"

    grade = "N/A"
    if score is not None:
        s = int(score)
        grade = "A" if s >= 80 else "B" if s >= 65 else "C" if s >= 50 else "SKIP"

    ml_line = ""
    if p is not None:
        ml_line = f"\nSession: {sess}\nML P(win): {p:.2f}  (samples: {samples})\nModel: {key}"
    else:
        ml_line = f"\nSession: {sess}\nML P(win): 0.50  (samples: 0)\nModel: {key}"

    msg = (
        f"{header}\n\n"
        f"{symbol} — {side}\n"
        f"Type: {typ}\n"
        f"TF: {tf}\n\n"
        f"Entry: {price}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"Adds: {adds}\n"
        f"Score: {score} ({grade})"
        f"{ml_line}"
    )

    r = send_telegram(msg)
    return jsonify({"ok": r.get("ok", False), "session": sess, "ml_p": p, "samples": samples, "model": key}), (200 if r.get("ok") else 500)

# Allow POST to "/" too (prevents 405 if user forgets /webhook)
@app.post("/")
def webhook_root():
    return webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
