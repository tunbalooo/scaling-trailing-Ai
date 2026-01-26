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
    CREATE TABLE IF NOT EXISTS model (
      id INTEGER PRIMARY KEY CHECK (id=1),
      w0 REAL, w1 REAL, w2 REAL, w3 REAL, w4 REAL,
      n INTEGER
    )
    """)
    # seed model row
    cur = conn.execute("SELECT COUNT(*) FROM model WHERE id=1")
    if cur.fetchone()[0] == 0:
        conn.execute("INSERT INTO model (id,w0,w1,w2,w3,w4,n) VALUES (1,0,0,0,0,0,0)")
    conn.commit()
    conn.close()

init_db()

# ─────────────────────────────
# Online Logistic Regression (SGD)
# Features (x):
# x0=1
# x1=score_norm (0..1)
# x2=rr_clipped (0..3)
# x3=sl_dist_norm (sl_dist/price)
# x4=side (BUY=1, SELL=0)
# ─────────────────────────────
def sigmoid(z: float) -> float:
    # stable sigmoid
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    else:
        ez = math.exp(z)
        return ez / (1.0 + ez)

def load_model():
    conn = db()
    row = conn.execute("SELECT w0,w1,w2,w3,w4,n FROM model WHERE id=1").fetchone()
    conn.close()
    w = [float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])]
    n = int(row[5])
    return w, n

def save_model(w, n):
    conn = db()
    conn.execute(
        "UPDATE model SET w0=?,w1=?,w2=?,w3=?,w4=?,n=? WHERE id=1",
        (w[0], w[1], w[2], w[3], w[4], n)
    )
    conn.commit()
    conn.close()

def features_from_row(side, price, sl, tp, score):
    # safe defaults
    price = float(price) if price is not None else None
    sl    = float(sl)    if sl is not None else None
    tp    = float(tp)    if tp is not None else None
    score = float(score) if score is not None else None

    if price is None or sl is None or tp is None or score is None or price <= 0:
        return None

    sl_dist = abs(price - sl)
    tp_dist = abs(tp - price)
    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0

    score_norm = max(0.0, min(1.0, score / 100.0))
    rr_clip    = max(0.0, min(3.0, rr))
    sl_norm    = max(0.0, min(0.02, sl_dist / price))  # clamp
    side_bin   = 1.0 if side == "BUY" else 0.0

    x = [1.0, score_norm, rr_clip, sl_norm, side_bin]
    return x, rr, sl_dist, tp_dist

def predict_proba(x):
    w, n = load_model()
    z = sum(wi*xi for wi, xi in zip(w, x))
    p = sigmoid(z)
    return p, n

def update_model(x, y, lr=0.25):
    w, n = load_model()
    p = sigmoid(sum(wi*xi for wi, xi in zip(w, x)))
    # gradient for logloss
    # w := w + lr * (y - p) * x
    step = lr * (y - p)
    for i in range(len(w)):
        w[i] += step * x[i]
    n += 1
    save_model(w, n)
    return p, n

# ─────────────────────────────
# Helpers: tie OUTCOME to most recent unlabeled ENTRY
# ─────────────────────────────
def mark_latest_unlabeled(symbol: str, tf: str, label: int):
    conn = db()
    row = conn.execute("""
        SELECT id, side, price, sl, tp, score
        FROM trades
        WHERE symbol=? AND tf=? AND typ='ENTRY' AND label IS NULL
        ORDER BY id DESC
        LIMIT 1
    """, (symbol, tf)).fetchone()

    if not row:
        conn.close()
        return None

    trade_id, side, price, sl, tp, score = row
    conn.execute("UPDATE trades SET label=? WHERE id=?", (label, trade_id))
    conn.commit()
    conn.close()
    return {"id": trade_id, "side": side, "price": price, "sl": sl, "tp": tp, "score": score}

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

    # event parsing
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

    price = fnum(data.get("price"), 2)
    sl    = fnum(data.get("sl"), 2)
    tp    = fnum(data.get("tp"), 2)
    adds  = fnum(data.get("adds"), 0)

    buyScore  = fnum(data.get("buyScore"), 0)
    sellScore = fnum(data.get("sellScore"), 0)
    score = buyScore if side == "BUY" else sellScore

    # store raw event (ENTRY/SCALE/TRAIL/OUTCOME)
    ts = int(time.time())

    # OUTCOME event: label latest unlabeled ENTRY and update model
    if typ == "OUTCOME":
        label = 1 if result == "WIN" else 0 if result == "LOSS" else None
        linked = mark_latest_unlabeled(symbol, tf, label) if label is not None else None

        if linked and label is not None:
            x_pack = features_from_row(linked["side"], linked["price"], linked["sl"], linked["tp"], linked["score"])
            if x_pack:
                x, rr, sl_dist, tp_dist = x_pack
                p_before, n_before = predict_proba(x)
                update_model(x, label)
                msg = (
                    f"📌 OUTCOME SAVED\n\n{symbol} ({tf})\n"
                    f"Result: {'WIN ✅' if label==1 else 'LOSS ❌'}\n"
                    f"Model updated. Prev P(win): {p_before:.2f} | Samples: {n_before}"
                )
                send_telegram(msg)

        return jsonify({"ok": True, "type": "OUTCOME", "linked": linked is not None}), 200

    # ENTRY: run ML prediction and store with label NULL
    rr = sl_dist = tp_dist = None
    p = None
    samples = 0

    if typ == "ENTRY" and side in ("BUY", "SELL"):
        feat = features_from_row(side, price, sl, tp, score)
        if feat:
            x, rr, sl_dist, tp_dist = feat
            p, samples = predict_proba(x)

    conn = db()
    conn.execute("""
        INSERT INTO trades (ts,symbol,tf,side,typ,price,sl,tp,score,adds,rr,sl_dist,tp_dist,label)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
    """, (ts, symbol, tf, side, typ, price, sl, tp, score, adds, rr, sl_dist, tp_dist))
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
        ml_line = f"\nML P(win): {p:.2f}  (trained samples: {samples})"

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
    return jsonify({"ok": r.get("ok", False), "ml_p": p, "samples": samples}), (200 if r.get("ok") else 500)

# Allow POST to "/" too (prevents 405 if you forget /webhook)
@app.post("/")
def webhook_root():
    return webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
