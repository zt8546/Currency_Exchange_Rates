"""Flask web 版：BOC 牌价的网页/手机访问入口。"""
import os
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

from boc_core import (
    BY_CODE, CURRENCIES, backfill_history, fetch_rate, init_db,
    insert_snapshot,
)

POLL_SECONDS = 60

app = Flask(__name__)
conn = init_db()
_db_lock = threading.Lock()
_last_status = {"msg": "启动中…", "ts": ""}


def _set_status(msg):
    _last_status["msg"] = msg
    _last_status["ts"] = datetime.now().isoformat(timespec="seconds")
    print(f"[status] {msg}", flush=True)


def _background_poll():
    while True:
        try:
            result = fetch_rate("CAD")
            ts = datetime.now().isoformat(timespec="seconds")
            with _db_lock:
                insert_snapshot(conn, ts, result.get("source", "?"),
                                result["currencies"])
            _set_status(f"已抓取 {len(result['currencies'])} 个币种")
        except Exception as e:
            _set_status(f"抓取失败: {e}")
        time.sleep(POLL_SECONDS)


def _initial_backfill():
    for c in CURRENCIES:
        try:
            n = backfill_history(conn, c["code"], days=365)
            if n > 0:
                _set_status(f"{c['code']} 回填 {n} 条历史")
        except Exception as e:
            _set_status(f"{c['code']} 回填失败: {e}")
    _set_status("历史回填完成")


# 启动后台任务（应用一启动就开始干活）
_bg_started = False
_bg_lock = threading.Lock()


def _ensure_bg_started():
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
        threading.Thread(target=_initial_backfill, daemon=True).start()
        threading.Thread(target=_background_poll, daemon=True).start()


_ensure_bg_started()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/currencies")
def api_currencies():
    return jsonify([
        {"code": c["code"], "label": c["label"],
         "boc": c["boc"], "unit": c["unit"], "fr": c["fr"]}
        for c in CURRENCIES
    ])


@app.route("/api/latest")
def api_latest():
    code = request.args.get("code", "CAD")
    if code not in BY_CODE:
        return jsonify({"error": "未知币种"}), 400
    with _db_lock:
        row = conn.execute(
            "SELECT ts, rate, buy_remit, buy_cash, sell_remit, sell_cash, source "
            "FROM rates WHERE currency = ? ORDER BY ts DESC LIMIT 1",
            (code,),
        ).fetchone()
    if not row:
        return jsonify({"error": "无数据，请稍候"}), 404
    ts, rate, br, bc, sr, sc, src = row
    return jsonify({
        "ts": ts,
        "rate": rate,
        "buy_remit": br, "buy_cash": bc,
        "sell_remit": sr, "sell_cash": sc,
        "source": src,
        "unit": BY_CODE[code]["unit"],
        "status": _last_status,
    })


@app.route("/api/history")
def api_history():
    code = request.args.get("code", "CAD")
    rng = request.args.get("range", "24h")
    if code not in BY_CODE:
        return jsonify({"error": "未知币种"}), 400
    now = datetime.now()
    cutoff_map = {
        "1h":  now - timedelta(hours=1),
        "24h": now - timedelta(hours=24),
        "7d":  now - timedelta(days=7),
        "30d": now - timedelta(days=30),
        "1y":  now - timedelta(days=365),
    }
    cutoff = cutoff_map.get(rng)
    with _db_lock:
        if cutoff is None:
            rows = conn.execute(
                "SELECT ts, rate, buy_remit, sell_remit FROM rates "
                "WHERE currency = ? ORDER BY ts",
                (code,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, rate, buy_remit, sell_remit FROM rates "
                "WHERE currency = ? AND ts >= ? ORDER BY ts",
                (code, cutoff.isoformat(timespec="seconds")),
            ).fetchall()
    return jsonify({
        "unit": BY_CODE[code]["unit"],
        "points": [
            {"ts": r[0], "mid": r[1], "buy": r[2], "sell": r[3]}
            for r in rows
        ],
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        result = fetch_rate("CAD")
        ts = datetime.now().isoformat(timespec="seconds")
        with _db_lock:
            insert_snapshot(conn, ts, result.get("source", "?"),
                            result["currencies"])
        _set_status(f"手动刷新 ok")
        return jsonify({"ok": True, "ts": ts, "source": result.get("source")})
    except Exception as e:
        _set_status(f"手动刷新失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
