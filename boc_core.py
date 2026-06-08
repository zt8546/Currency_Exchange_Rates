"""数据层：BOC 抓取 + Frankfurter 回填 + SQLite。desktop 与 web 共用。"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# 数据/配置文件位置：本地用源码目录；打包 exe 用 exe 旁边；云端用环境变量 DATA_DIR
if os.environ.get("DATA_DIR"):
    APP_DIR = Path(os.environ["DATA_DIR"])
    APP_DIR.mkdir(parents=True, exist_ok=True)
elif getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "rates.db"
CONFIG_PATH = APP_DIR / "config.json"

CURRENCIES = [
    {"code": "CAD", "boc": "加拿大元",   "unit": 1,   "label": "加元",   "fr": True},
    {"code": "USD", "boc": "美元",       "unit": 1,   "label": "美元",   "fr": True},
    {"code": "GBP", "boc": "英镑",       "unit": 1,   "label": "英镑",   "fr": True},
    {"code": "EUR", "boc": "欧元",       "unit": 1,   "label": "欧元",   "fr": True},
    {"code": "JPY", "boc": "日元",       "unit": 100, "label": "日元",   "fr": True},
    {"code": "KRW", "boc": "韩国元",     "unit": 100, "label": "韩元",   "fr": True},
    {"code": "HKD", "boc": "港币",       "unit": 1,   "label": "港币",   "fr": True},
    {"code": "TWD", "boc": "新台币",     "unit": 1,   "label": "新台币", "fr": False},
    {"code": "AUD", "boc": "澳大利亚元", "unit": 1,   "label": "澳元",   "fr": True},
    {"code": "RUB", "boc": "卢布",       "unit": 1,   "label": "卢布",   "fr": False},
]
BY_CODE = {c["code"]: c for c in CURRENCIES}

BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
BOC_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
BOC_ROW_RES = {
    c["code"]: re.compile(
        r"<tr[^>]*>((?:(?!</tr>).)*?" + re.escape(c["boc"]) + r"(?:(?!</tr>).)*?)</tr>",
        re.S,
    )
    for c in CURRENCIES
}

JSON_SOURCES = [
    ("open.er-api.com",
     "https://open.er-api.com/v6/latest/{code}",
     lambda j: j["rates"]["CNY"]),
    ("frankfurter.app",
     "https://api.frankfurter.app/latest?from={code}&to=CNY",
     lambda j: j["rates"]["CNY"]),
    ("exchangerate-api.com",
     "https://api.exchangerate-api.com/v4/latest/{code}",
     lambda j: j["rates"]["CNY"]),
]


def fetch_boc_all():
    bust = int(time.time() * 1000)
    url = f"{BOC_URL}?_={bust}"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Monitor/{bust}",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
        "Connection": "close",
    })
    with urllib.request.build_opener().open(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    def to_rate(s):
        try:
            return float(s) / 100.0
        except (TypeError, ValueError):
            return None

    out = {}
    boc_time = ""
    for code, row_re in BOC_ROW_RES.items():
        m = row_re.search(html)
        if not m:
            continue
        raw_tds = BOC_TD_RE.findall(m.group(1))
        tds = [TAG_STRIP_RE.sub("", t).strip() for t in raw_tds]
        if len(tds) < 6:
            continue
        out[code] = {
            "rate": to_rate(tds[5]),
            "buy_remit": to_rate(tds[1]),
            "buy_cash": to_rate(tds[2]),
            "sell_remit": to_rate(tds[3]),
            "sell_cash": to_rate(tds[4]),
        }
        if not boc_time:
            boc_time = tds[-1]
    if not out:
        raise RuntimeError("BOC 页面未解析到任何币种")
    return {"currencies": out, "boc_time": boc_time, "source": "中国银行牌价"}


def fetch_json_one(code):
    errs = []
    for name, url_tpl, extract in JSON_SOURCES:
        try:
            req = urllib.request.Request(
                url_tpl.format(code=code),
                headers={"User-Agent": "Monitor/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return {"rate": float(extract(data)), "source": name}
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise RuntimeError(" | ".join(errs))


def fetch_rate(current_code):
    """优先 BOC（一次拿全部币种）；BOC 挂了再用 JSON 源只抓当前币种。"""
    try:
        return fetch_boc_all()
    except Exception as boc_err:
        try:
            r = fetch_json_one(current_code)
            return {
                "currencies": {current_code: {"rate": r["rate"]}},
                "boc_time": "",
                "source": r["source"],
                "boc_error": str(boc_err),
            }
        except Exception as e:
            raise RuntimeError(f"BOC: {boc_err}; JSON: {e}")


def init_db(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rates "
        "(ts TEXT NOT NULL, rate REAL NOT NULL, source TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rates_ts ON rates(ts)")
    for col_def in (
        "buy_remit REAL", "buy_cash REAL",
        "sell_remit REAL", "sell_cash REAL",
        "currency TEXT NOT NULL DEFAULT 'CAD'",
    ):
        try:
            conn.execute(f"ALTER TABLE rates ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rates_curr_ts ON rates(currency, ts)")
    conn.commit()
    return conn


def backfill_history(conn, code, days=365, progress_cb=None):
    info = BY_CODE.get(code)
    if not info or not info["fr"]:
        if progress_cb:
            progress_cb(f"{code} 无可用历史源（Frankfurter 不支持）")
        return 0
    cutoff_iso = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) FROM rates "
        "WHERE currency = ? AND source LIKE 'frankfurter%' AND ts >= ?",
        (code, cutoff_iso),
    ).fetchone()
    if row and row[0] >= max(200, int(days * 0.6)):
        return 0
    end = datetime.now().date()
    start = end - timedelta(days=days)
    url = (
        f"https://api.frankfurter.app/{start.isoformat()}..{end.isoformat()}"
        f"?from={code}&to=CNY"
    )
    if progress_cb:
        progress_cb(f"正在回填 {code} 一年历史 ({start} -> {end})…")
    req = urllib.request.Request(url, headers={"User-Agent": "Monitor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rates_by_date = data.get("rates", {}) or {}
    inserted = 0
    for date_str, kv in sorted(rates_by_date.items()):
        cny = kv.get("CNY")
        if cny is None:
            continue
        ts = f"{date_str}T16:00:00"
        exists = conn.execute(
            "SELECT 1 FROM rates "
            "WHERE ts = ? AND currency = ? AND source LIKE 'frankfurter%' LIMIT 1",
            (ts, code),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO rates (ts, rate, source, currency) VALUES (?, ?, ?, ?)",
            (ts, float(cny), "frankfurter (历史)", code),
        )
        inserted += 1
    conn.commit()
    return inserted


def insert_snapshot(conn, ts, source, currencies_dict):
    """把一次抓取的所有币种写进 DB。currencies_dict: {code: {rate, buy_remit, ...}}"""
    for code, data in currencies_dict.items():
        if data.get("rate") is None:
            continue
        conn.execute(
            "INSERT INTO rates "
            "(ts, rate, source, buy_remit, buy_cash, sell_remit, sell_cash, currency)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, data["rate"], source,
             data.get("buy_remit"), data.get("buy_cash"),
             data.get("sell_remit"), data.get("sell_cash"), code),
        )
    conn.commit()


def load_config():
    default = {"current_currency": "CAD", "alerts": {}}
    if not CONFIG_PATH.exists():
        return default
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    except Exception:
        return default
    if "alert_high" in cfg or "alert_low" in cfg:
        cfg.setdefault("alerts", {})["CAD"] = {
            "high": cfg.pop("alert_high", None),
            "low": cfg.pop("alert_low", None),
        }
    cfg.setdefault("current_currency", "CAD")
    cfg.setdefault("alerts", {})
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                           encoding="utf-8")
