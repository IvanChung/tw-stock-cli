#!/usr/bin/env python3
"""TW stock-only portfolio tracker (no trust funds / bank funds).
讀取 portfolio.json 的 stocks 區塊；歷史快照寫入 history_stocks.json。"""

import argparse
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))

import requests


def _valid_float(v) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return None


PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"
HISTORY_PATH   = Path(__file__).parent / "history_stocks.json"
DATA_DIR       = Path(__file__).parent / "data"
HISTORY_KEEP   = 30
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0"


def update_history(today: str, current: float, cost: float) -> dict | None:
    history: list[dict] = []
    if HISTORY_PATH.exists():
        try: history = json.loads(HISTORY_PATH.read_text(encoding='utf-8'))
        except Exception: history = []
    prev = next((h for h in reversed(history) if h.get("date") != today), None)
    history = [h for h in history if h.get("date") != today]
    history.append({"date": today, "current": round(current, 2), "cost": round(cost, 2)})
    history = history[-HISTORY_KEEP:]
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
    return prev


def merge_history(snapshots: list[dict]) -> None:
    existing: list[dict] = []
    if HISTORY_PATH.exists():
        try: existing = json.loads(HISTORY_PATH.read_text(encoding='utf-8'))
        except Exception: existing = []
    by_date = {h["date"]: h for h in existing}
    for s in snapshots:
        by_date[s["date"]] = s
    merged = sorted(by_date.values(), key=lambda h: h["date"])[-HISTORY_KEEP:]
    HISTORY_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding='utf-8')


# ── Formatting helpers ────────────────────────────────────────────────────────

def cjk_ljust(s: str, width: int) -> str:
    import unicodedata
    def dw(t): return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in t)
    if dw(s) > width:
        out, used = [], 0
        for c in s:
            cw = 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
            if used + cw > width - 1:
                out.append('…'); used += 1; break
            out.append(c); used += cw
        s = ''.join(out)
    return s + ' ' * max(0, width - dw(s))


def cjk_rjust(s: str, width: int) -> str:
    import unicodedata
    def dw(t): return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in t)
    return ' ' * max(0, width - dw(s)) + s


def colored_cell(value_str: str, color: str, width: int, right: bool = True) -> str:
    """Pad value_str to `width` visual cols (CJK aware), then wrap with ANSI color."""
    padded = cjk_rjust(value_str, width) if right else cjk_ljust(value_str, width)
    return f"{color}{padded}{_RESET}" if color else padded

_RED   = "\033[91m"
_GREEN = "\033[92m"
_RESET = "\033[0m"

def _colorize(s: str, change: float) -> str:
    if change > 0: return f"{_RED}{s}{_RESET}"
    if change < 0: return f"{_GREEN}{s}{_RESET}"
    return s

def twd(amount: float) -> str:
    return f"NT${amount:>10,.0f}"

def pnl_str(pnl: float, cost: float) -> str:
    pct = pnl / cost * 100 if cost else 0
    s = f"NT${pnl:>10,.0f} ({pct:+6.1f}%)"
    return _colorize(s, pnl)

# Match paid-stock tail width: 13 + 2sep + 13 + 2sep + 23 (損益 placeholder) = 53 visual cols
TRACKING_CELLS = f"{'─':>13}  {'─':>13}  " + cjk_ljust("─       追蹤標的", 23)


# ── CMoney APIs (dividends + daily K-line + realtime) ────────────────────────

CMONEY_API = "https://www.cmoney.tw/MobileService/ashx/GetDtnoData.ashx"

def get_cmoney_dividends(ticker: str, div_year: int) -> list[tuple[str, float]]:
    try:
        r = SESSION.get(CMONEY_API, params={
            "action": "getdtnodata", "DtNo": "59444834",
            "ParamStr": f"AssignID={ticker};MTPeriod=3;DTMode=0;DTRange=20;DTOrder=1;MajorTable=M810;",
            "AssignSPID": ticker, "FilterNo": "0",
        }, timeout=10)
        year_str = str(div_year)
        divs: list[tuple[str, float]] = []
        for row in r.json().get("Data", []):
            ex_raw, amt_raw = row[3], row[1]
            if not ex_raw or not ex_raw.startswith(year_str): continue
            amt = _valid_float(amt_raw)
            if not amt or amt <= 0: continue
            ex_date = f"{ex_raw[:4]}/{ex_raw[4:6]}/{ex_raw[6:8]}"
            divs.append((ex_date, round(amt, 4)))
        divs.sort(key=lambda x: x[0])
        return divs
    except Exception:
        return []


# ── Realtime quote sources ───────────────────────────────────────────────────

TWSE_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
ANUE_API = "https://ws.api.cnyes.com/ws/api/v1/quote/quotes/"

def get_twse_realtime(tickers: list[str]) -> dict[str, tuple[float, float] | None]:
    ex_ch = "|".join(f"{prefix}_{t}.tw" for t in tickers for prefix in ("tse", "otc"))
    try:
        r = SESSION.get(TWSE_API, params={"ex_ch": ex_ch, "json": "1", "delay": "0"}, timeout=10)
        results: dict[str, tuple[float, float] | None] = {t: None for t in tickers}
        for item in r.json().get("msgArray", []):
            code = item.get("c", "")
            if code not in results or results[code] is not None: continue
            prev  = _valid_float(item.get("y"))
            price = _valid_float(item.get("z"))
            if price is None: price = prev
            if price is not None and prev is not None:
                results[code] = (price, prev)
        return results
    except Exception:
        return {t: None for t in tickers}


def get_anue_realtime(tickers: list[str]) -> dict[str, tuple[float, float] | None]:
    syms = ",".join(f"TWS:{t}:STOCK" for t in tickers)
    try:
        r = SESSION.get(ANUE_API + syms, params={"column": "FORMAT"}, timeout=10)
        results: dict[str, tuple[float, float] | None] = {t: None for t in tickers}
        for item in r.json().get("data", []):
            code = item.get("200010", "")
            if code not in results: continue
            price = _valid_float(item.get("200026"))
            prev  = _valid_float(item.get("200031"))
            if price is None: price = prev
            if price is not None and prev is not None:
                results[code] = (price, prev)
        return results
    except Exception:
        return {t: None for t in tickers}


CMONEY_QUOTE_API = "https://www.cmoney.tw/finance/ashx/mainpage.ashx"
CMONEY_QUOTE_REFERER = "https://www.cmoney.tw/finance/2412/f00025"

def _get_cmoney_cmkey() -> str | None:
    try:
        r = SESSION.get(CMONEY_QUOTE_REFERER, timeout=10)
        m = re.search(r"cmkey='([^']+)'\s+page='f00025'", r.text)
        return m.group(1) if m else None
    except Exception:
        return None

def _cmoney_one_quote(ticker: str, cmkey: str) -> tuple[float, float] | None:
    try:
        r = SESSION.get(CMONEY_QUOTE_API,
            params={"action": "GetStockListLatestSaleData", "stockId": ticker, "cmkey": cmkey},
            headers={"Referer": CMONEY_QUOTE_REFERER}, timeout=10)
        d = r.json().get("commSaleData") or {}
        price = _valid_float(d.get("SalePr"))
        change = _valid_float(d.get("Cf"))
        if price is None or change is None: return None
        return price, round(price - change, 4)
    except Exception:
        return None

def get_cmoney_realtime(tickers: list[str]) -> dict[str, tuple[float, float] | None]:
    cmkey = _get_cmoney_cmkey()
    if not cmkey:
        return {t: None for t in tickers}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {t: ex.submit(_cmoney_one_quote, t, cmkey) for t in tickers}
    return {t: fut.result() for t, fut in futures.items()}


REALTIME_SOURCES = {
    "twse":   ("TWSE",       get_twse_realtime),
    "anue":   ("Anue 鉅亨",  get_anue_realtime),
    "cmoney": ("CMoney",     get_cmoney_realtime),
}


def get_twse_intraday(ticker: str) -> dict | None:
    """Today's intraday OHLC + volume from TWSE MIS (only source exposing OHLC).
    Returns {date, open, high, low, close, volume} or None.
    Volume converted 張→股 (×1000) to match CMoney units."""
    ex_ch = f"tse_{ticker}.tw|otc_{ticker}.tw"
    try:
        r = SESSION.get(TWSE_API, params={"ex_ch": ex_ch, "json": "1", "delay": "0"}, timeout=10)
        for item in r.json().get("msgArray", []):
            if item.get("c") != ticker:
                continue
            d = item.get("d") or ""
            o = _valid_float(item.get("o"))
            h = _valid_float(item.get("h"))
            lo = _valid_float(item.get("l"))
            z = _valid_float(item.get("z"))
            v = _valid_float(item.get("v"))
            if not d or None in (o, h, lo, z):
                return None
            return {"date": d, "open": o, "high": h, "low": lo, "close": z,
                    "volume": int((v or 0) * 1000)}
    except Exception:
        pass
    return None


# ── K-line chart ─────────────────────────────────────────────────────────────

def get_daily_kline(ticker: str) -> list[tuple[str, float, float, float, float]]:
    try:
        r = SESSION.get(CMONEY_API, params={
            "action": "getdtnodata", "DtNo": "5389",
            "ParamStr": f"AssignID={ticker};MTPeriod=0",
            "AssignSPID": ticker, "FilterNo": "0",
        }, timeout=15)
        out = []
        for row in r.json().get("Data", []):
            try:
                out.append((row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4])))
            except Exception: continue
        return sorted(out)
    except Exception:
        return []


def get_stock_name(ticker: str) -> str:
    try:
        r = SESSION.get(ANUE_API + f"TWS:{ticker}:STOCK", params={"column": "FORMAT"}, timeout=10)
        data = r.json().get("data", [])
        return data[0].get("200009", "") if data else ""
    except Exception:
        return ""


def _sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i, _ in enumerate(values):
        if i < window - 1: out.append(None)
        else: out.append(sum(values[i - window + 1:i + 1]) / window)
    return out


def _kdj(ohlc: list[tuple], n: int = 9) -> tuple[list, list, list]:
    K_list: list[float | None] = []
    D_list: list[float | None] = []
    J_list: list[float | None] = []
    K_prev = D_prev = 50.0
    for i in range(len(ohlc)):
        if i < n - 1:
            K_list.append(None); D_list.append(None); J_list.append(None); continue
        window = ohlc[i - n + 1:i + 1]
        h_max = max(c[2] for c in window)
        l_min = min(c[3] for c in window)
        c_now = ohlc[i][4]
        rsv = 50.0 if h_max == l_min else (c_now - l_min) / (h_max - l_min) * 100
        K_curr = 2 / 3 * K_prev + 1 / 3 * rsv
        D_curr = 2 / 3 * D_prev + 1 / 3 * K_curr
        J_curr = 3 * K_curr - 2 * D_curr
        K_list.append(K_curr); D_list.append(D_curr); J_list.append(J_curr)
        K_prev, D_prev = K_curr, D_curr
    return K_list, D_list, J_list


_YEL = "\033[93m"; _CYA = "\033[96m"; _MAG = "\033[95m"; _BLU = "\033[94m"
_WHT = "\033[97m"; _DIM = "\033[2m"


def compute_tech(ticker: str) -> tuple[float | None, str | None]:
    """Return (J_value, ma30_status) for a ticker. status: 'up' | 'down' | None."""
    ohlc = get_daily_kline(ticker)
    if not ohlc or len(ohlc) < 30:
        return None, None
    closes = [c[4] for c in ohlc]
    ma30 = _sma(closes, 30)[-1]
    _, _, j_list = _kdj(ohlc, n=9)
    j = j_list[-1]
    last_close = closes[-1]
    status = 'up' if ma30 is not None and last_close >= ma30 else 'down'
    return j, status


def fmt_j(j: float | None) -> str:
    if j is None:
        return f"{'─':>3}"
    color = _RED if j > 80 else (_GREEN if j < 20 else "")
    s = f"{j:>3.0f}"
    return f"{color}{s}{_RESET}" if color else s


def fmt_ma_status(status: str | None) -> str:
    if status is None:
        return f"{'─':>4}"
    sym = '↑' if status == 'up' else '↓'
    color = _RED if status == 'up' else _GREEN
    return f"{color}{sym:>4}{_RESET}"


def _fetch_daily_full(ticker: str) -> list[list]:
    """Return full daily rows (incl. foreign flow at idx 6/7) from CMoney."""
    try:
        r = SESSION.get(CMONEY_API, params={
            "action": "getdtnodata", "DtNo": "5389",
            "ParamStr": f"AssignID={ticker};MTPeriod=0",
            "AssignSPID": ticker, "FilterNo": "0",
        }, timeout=15)
        return sorted(r.json().get("Data", []))
    except Exception:
        return []


def get_realtime_bar(ticker: str, source: str = "anue") -> dict | None:
    """Synthesize today's bar from a realtime quote (price + prev_close).
    open=prev_close, close=price, high/low=max/min(price, prev). volume unknown→0.
    Returns {date, open, high, low, close, volume, _source} or None."""
    label, fetcher = REALTIME_SOURCES.get(source, REALTIME_SOURCES["anue"])
    quote = fetcher([ticker]).get(ticker)
    if quote is None:
        return None
    price, prev = quote
    return {
        "date": date.today().strftime("%Y%m%d"),
        "open": float(prev), "high": float(max(price, prev)),
        "low":  float(min(price, prev)), "close": float(price),
        "volume": 0, "_source": label,
    }


def fetch_kline_with_intraday(ticker: str, realtime_source: str = "anue") -> tuple[list[list], list[tuple], dict | None]:
    """Fetch CMoney historical + splice today's bar so the chart always ends at today.
    Today's bar priority: TWSE MIS (real OHLC) → realtime quote (synth from price+prev_close).
    Returns (full_rows_for_save, ohlc_for_drawing, today_bar_or_None).
    - full_rows: CMoney historical only (completed bars; safe to persist)
    - ohlc_for_drawing: includes today's bar appended if CMoney lacks today
    - today_bar: spliced bar dict (with _source label), or None"""
    full_rows = _fetch_daily_full(ticker)
    ohlc = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in full_rows]
    today_str = date.today().strftime("%Y%m%d")
    if ohlc and ohlc[-1][0] == today_str:
        return full_rows, ohlc, None
    today_bar = get_twse_intraday(ticker)
    if today_bar and today_bar["date"] == today_str:
        today_bar["_source"] = "TWSE MIS"
    else:
        today_bar = get_realtime_bar(ticker, realtime_source)
    if today_bar and today_bar["date"] == today_str:
        ohlc.append((today_bar["date"], today_bar["open"], today_bar["high"],
                     today_bar["low"], today_bar["close"]))
        return full_rows, ohlc, today_bar
    return full_rows, ohlc, None


# TWSE STOCK_DAY: per-month OHLCV history (TSE listed only, no foreign flow).
# Used to backfill older bars beyond CMoney's ~250-day window.
TWSE_HISTORY_API = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_HISTORY_API = "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"


def _roc_to_iso(roc: str) -> str:
    """民國日期 '114/05/13' → '20250513'. Returns '' on parse failure."""
    parts = roc.strip().split("/")
    if len(parts) != 3: return ""
    try:
        y, m, d = int(parts[0]) + 1911, int(parts[1]), int(parts[2])
        return f"{y:04d}{m:02d}{d:02d}"
    except ValueError:
        return ""


def _fetch_twse_month(ticker: str, y: int, m: int) -> list[list]:
    """One month OHLCV from TWSE STOCK_DAY (TSE). Returns [[date, O, H, L, C, vol, 0, 0], ...]."""
    try:
        r = SESSION.get(TWSE_HISTORY_API, params={
            "date": f"{y:04d}{m:02d}01", "stockNo": ticker, "response": "json"
        }, timeout=15)
        d = r.json()
        if d.get("stat") != "OK": return []
        rows = []
        for row in d.get("data", []):
            iso = _roc_to_iso(row[0])
            if not iso: continue
            try:
                vol = int(row[1].replace(",", ""))
                op  = float(row[3].replace(",", ""))
                hi  = float(row[4].replace(",", ""))
                lo  = float(row[5].replace(",", ""))
                cl  = float(row[6].replace(",", ""))
                rows.append([iso, op, hi, lo, cl, vol, 0, 0])
            except (ValueError, IndexError):
                continue
        return rows
    except Exception:
        return []


def _fetch_tpex_month(ticker: str, y: int, m: int) -> list[list]:
    """One month OHLCV from TPEx (上櫃). Returns same shape as _fetch_twse_month."""
    try:
        r = SESSION.get(TPEX_HISTORY_API, params={
            "l": "zh-tw", "d": f"{y - 1911}/{m:02d}", "stkno": ticker,
            "_": str(int(time.time() * 1000))
        }, timeout=15)
        d = r.json()
        rows = []
        for row in d.get("aaData", []):
            iso = _roc_to_iso(row[0])
            if not iso: continue
            try:
                vol = int(row[1].replace(",", ""))
                op  = float(row[3].replace(",", ""))
                hi  = float(row[4].replace(",", ""))
                lo  = float(row[5].replace(",", ""))
                cl  = float(row[6].replace(",", ""))
                rows.append([iso, op, hi, lo, cl, vol, 0, 0])
            except (ValueError, IndexError):
                continue
        return rows
    except Exception:
        return []


def fetch_history_long(ticker: str, days: int) -> list[list]:
    """Backfill multi-year OHLCV by iterating TWSE STOCK_DAY (or TPEx) monthly.
    Returns rows sorted ascending; no foreign flow (zeros). Rate-limited ~0.3s/req.
    First month decides venue (TSE vs OTC); subsequent months reuse that fetcher."""
    today = date.today()
    months_needed = max(2, days // 20 + 2)  # ~20 trading days/month + buffer
    fetcher = _fetch_twse_month
    rows: list[list] = []
    used_tpex = False
    for i in range(months_needed):
        y = today.year
        m = today.month - i
        while m <= 0:
            m += 12; y -= 1
        month_rows = fetcher(ticker, y, m)
        if i == 0 and not month_rows:
            month_rows = _fetch_tpex_month(ticker, y, m)
            if month_rows:
                fetcher = _fetch_tpex_month
                used_tpex = True
        rows.extend(month_rows)
        time.sleep(0.3)
    rows.sort(key=lambda r: r[0])
    seen = set()
    out = []
    for r in rows:
        if r[0] in seen: continue
        seen.add(r[0])
        out.append(r)
    if not out:
        print(f"  ⚠ TWSE/TPEx 都查不到 {ticker} 的歷史資料")
    elif used_tpex:
        print(f"  ⓘ {ticker} 用 TPEx (上櫃) 資料源")
    return out[-days:]


def save_kline_history(ticker: str, full_rows: list[list], days: int) -> tuple[int, int]:
    """Save last `days` of OHLCV + foreign flow as structured JSON to ./data/<ticker>.json.
    For days ≤ ~250: CMoney only (with foreign flow).
    For days > 250: TWSE/TPEx backfill (no foreign flow on older bars) merged with
    CMoney on the recent overlap (CMoney wins on overlap so foreign flow is preserved)."""
    DATA_DIR.mkdir(exist_ok=True)
    if days <= len(full_rows):
        rows_slice = full_rows[-days:]
        source = "cmoney_5389"
    else:
        print(f"  ⓘ 需要 {days} 日，CMoney 僅 {len(full_rows)} 日 — 從 TWSE 回填較舊資料 "
              f"({(days - len(full_rows)) // 20 + 2} 個月 API 請求，~{((days - len(full_rows)) // 20 + 2) * 0.3:.0f}s)")
        long_rows = fetch_history_long(ticker, days)
        by_date = {r[0]: r for r in long_rows}
        for r in full_rows:
            by_date[r[0]] = r  # CMoney overrides TWSE for overlap (preserves foreign flow)
        rows_slice = sorted(by_date.values(), key=lambda r: r[0])[-days:]
        source = "twse_stock_day+cmoney_5389"
    structured = []
    for r in rows_slice:
        try:
            structured.append({
                "date": r[0],
                "open": float(r[1]), "high": float(r[2]),
                "low":  float(r[3]), "close": float(r[4]),
                "volume":       int(float(r[5] or 0)),
                "foreign_buy":  int(float(r[6] or 0)),
                "foreign_sell": int(float(r[7] or 0)),
            })
        except (ValueError, TypeError, IndexError):
            continue
    payload = {
        "ticker": ticker,
        "source": source,
        "fetched_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
        "days_requested": days,
        "days_returned": len(structured),
        "rows": structured,
    }
    out = DATA_DIR / f"{ticker}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return days, len(structured)


# Cache market-wide PE/PB (one fetch per run)
_pe_pb_cache: dict[str, tuple[float, float]] | None = None

def _fetch_all_pe_pb() -> dict[str, tuple[float, float]]:
    """Cache and return {ticker: (PE, PB)} for all listed individual stocks (no ETFs)."""
    global _pe_pb_cache
    if _pe_pb_cache is not None:
        return _pe_pb_cache
    out: dict[str, tuple[float, float]] = {}
    try:
        r = SESSION.get(CMONEY_API, params={
            "action": "getdtnodata", "DtNo": "71872",
            "ParamStr": "AssignID=2412;MTPeriod=0",
            "AssignSPID": "2412", "FilterNo": "0",
        }, timeout=15)
        for row in r.json().get("Data", []):
            try:
                tid = row[0]
                pe = _valid_float(row[8])
                pb = _valid_float(row[9])
                if pe is None or pb is None:
                    continue
                out[tid] = (pe, pb)
            except Exception:
                continue
    except Exception:
        pass
    _pe_pb_cache = out
    return out


def compute_valuation(ticker: str) -> dict:
    """Compute PE, PB, ROE (= PB/PE × 100), and A/B/C/D/F grade.
    For ETFs (not in 71872) returns grade='N/A'."""
    pe_pb = _fetch_all_pe_pb()
    pair = pe_pb.get(ticker)
    if pair is None:
        return {"pe": None, "pb": None, "roe": None, "grade": "N/A"}
    pe, pb = pair
    if pe <= 0:
        return {"pe": pe, "pb": pb, "roe": None, "grade": "F"}  # 虧損
    roe = pb / pe * 100  # algebraic identity: ROE = EPS/BPS = (P/PE)/(P/PB) = PB/PE

    # PE score (lower is better)
    if   pe <= 12: pe_s = 4
    elif pe <= 18: pe_s = 3
    elif pe <= 25: pe_s = 2
    elif pe <= 40: pe_s = 1
    else:          pe_s = 0
    # ROE score (higher is better)
    if   roe > 25: roe_s = 4
    elif roe > 15: roe_s = 3
    elif roe > 10: roe_s = 2
    elif roe >  5: roe_s = 1
    else:          roe_s = 0
    total = pe_s + roe_s
    grade = "A" if total >= 7 else "B" if total >= 5 else "C" if total >= 3 else "D"
    return {"pe": pe, "pb": pb, "roe": roe, "grade": grade}


def compute_signal(ticker: str) -> dict:
    """V2 + grading + downtrend warning.
    Status: STRONG_BUY (V2 + MA200 不跌 + 收盤≥MA150) / BUY (V2) / WATCH / HOT / None."""
    rows = _fetch_daily_full(ticker)
    if not rows or len(rows) < 65:
        return {"ticker": ticker, "status": None, "note": "資料不足 (<65 日)"}
    try:
        ohlc = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]
    except Exception:
        return {"ticker": ticker, "status": None, "note": "資料解析失敗"}
    closes = [c[4] for c in ohlc]
    ma10  = _sma(closes, 10)[-1]
    ma30  = _sma(closes, 30)[-1]
    ma60  = _sma(closes, 60)[-1]
    ma150 = _sma(closes, 150)[-1] if len(closes) >= 150 else None
    ma200 = _sma(closes, 200)[-1] if len(closes) >= 200 else None
    ma60_5d_ago  = _sma(closes[:-5], 60)[-1]  if len(closes) >= 65  else None
    ma200_5d_ago = _sma(closes[:-5], 200)[-1] if len(closes) >= 205 else None
    ma60_slope  = (ma60  / ma60_5d_ago  - 1) * 100 if ma60  and ma60_5d_ago  else 0.0
    ma200_slope = (ma200 / ma200_5d_ago - 1) * 100 if ma200 and ma200_5d_ago else None
    _, _, j_list = _kdj(ohlc, n=9)
    j = j_list[-1] if j_list[-1] is not None else 50
    j_prev = j_list[-2] if len(j_list) >= 2 and j_list[-2] is not None else j
    j_min_5 = min((x for x in j_list[-5:] if x is not None), default=50)
    close = closes[-1]
    def fnet(n):
        return sum((float(r[6] or 0) - float(r[7] or 0)) / 100000 for r in rows[-n:])
    fx_5d, fx_30d = fnet(5), fnet(30)

    # Volume contraction (5 日均量 / 30 日均量)
    try:
        v5  = sum(float(r[5] or 0) for r in rows[-5:])  / 5
        v30 = sum(float(r[5] or 0) for r in rows[-30:]) / 30
        vol_ratio = v5 / v30 if v30 else 1.0
    except Exception:
        vol_ratio = 1.0

    # 30-day price position (0 = bottom, 1 = top)
    win30_h = max(c[2] for c in ohlc[-30:])
    win30_l = min(c[3] for c in ohlc[-30:])
    pos = (close - win30_l) / (win30_h - win30_l) if win30_h > win30_l else 0.5

    # 5-day price change & foreign flow decay rate (for MOMENTUM detection)
    chg_5d = (close / closes[-6] - 1) * 100 if len(closes) >= 6 else 0.0
    if abs(fx_30d / 30) > 0.01:
        fx_decay = (fx_5d / 5) / (fx_30d / 30) * 100
    else:
        fx_decay = None
    ma60_dist = (close / ma60 - 1) * 100 if ma60 else None

    # V2 BUY conditions
    cond_j_rebound = (j_min_5 < 20) and (j >= 20) and (j > j_prev)
    cond_bottom    = pos <= 0.33
    cond_flow      = fx_5d >= -2.0
    cond_volume    = vol_ratio <= 0.80
    is_buy = cond_j_rebound and cond_bottom and cond_flow and cond_volume

    # STRONG_BUY adds trend filter
    cond_ma200_ok  = ma200_slope is not None and ma200_slope >= 0
    cond_ma150_ok  = ma150 is not None and close >= ma150
    is_strong = is_buy and cond_ma200_ok and cond_ma150_ok

    # MOMENTUM (+HOT) — backtest 驗證的動能突破訊號（牛市中應跟進，非減碼）
    # 條件：急漲 + 遠離 MA60 + 外資累積但動能消失（主力推動完成）+ 量爆 + J 接近 HOT
    cond_momentum = (
        chg_5d > 8 and
        ma60_dist is not None and ma60_dist > 12 and
        fx_30d > 5 and
        fx_decay is not None and fx_decay < 30 and
        vol_ratio > 1.25 and
        j > 75
    )

    # Downtrend warning (orthogonal — can pair with BUY/WATCH/None)
    downtrend = ma200_slope is not None and ma200_slope < -0.1

    if cond_momentum:
        status, note = "MOMENTUM", f"動能突破 (5日 {chg_5d:+.1f}% / 量比 {vol_ratio:.0%})"
    elif j > 80 and ma10 is not None and close > ma10:
        notes = ["超買 + 站上 MA10"]
        if ma60_slope >= 0.5:
            notes.append("✓ 多頭續行")
        elif ma60_slope < -0.1:
            notes.append("⚠ 中期轉弱")
        if fx_5d < -2:
            notes.append("⚠ 外資 5 日轉賣")
        status, note = "HOT", " / ".join(notes)
    elif is_strong:
        status, note = "STRONG_BUY", "V2 + MA200不跌 + ≥MA150"
    elif is_buy:
        status, note = "BUY", "V2: J反彈+底部+量縮+籌碼"
        if downtrend:
            note += f" / ⚠ MA200 下降 ({ma200_slope:+.2f}%)"
    elif j < 20:
        reasons = []
        if not cond_j_rebound: reasons.append("J 尚未反彈")
        if not cond_bottom:    reasons.append(f"非底部區 ({pos:.0%})")
        if not cond_flow:      reasons.append(f"外資賣超 ({fx_5d:+.1f}億)")
        if not cond_volume:    reasons.append(f"量未縮 ({vol_ratio:.0%})")
        status, note = "WATCH", " / ".join(reasons) if reasons else "超賣中"
    else:
        status, note = None, ""

    val = compute_valuation(ticker)
    ma60_dist = (close / ma60 - 1) * 100 if ma60 else None
    return {
        "ticker": ticker, "close": close, "j": j,
        "ma10": ma10, "ma30": ma30, "ma60": ma60, "ma150": ma150, "ma200": ma200,
        "ma60_slope": ma60_slope, "ma200_slope": ma200_slope, "ma60_dist": ma60_dist,
        "fx_5d": fx_5d, "fx_30d": fx_30d, "vol_ratio": vol_ratio, "pos30d": pos,
        "downtrend": downtrend, "status": status, "note": note,
        "pe": val["pe"], "pb": val["pb"], "roe": val["roe"], "grade": val["grade"],
    }


_BOLD = "\033[1m"

def fmt_signal(status: str | None, width: int = 5) -> str:
    if status == "STRONG_BUY": return f"{_RED}{_BOLD}{'+BUY':>{width}}{_RESET}"
    if status == "BUY":        return f"{_RED}{'BUY':>{width}}{_RESET}"
    if status == "WATCH":      return f"{_CYA}{'WATCH':>{width}}{_RESET}"
    if status == "MOMENTUM":   return f"{_RED}{_BOLD}{'+HOT':>{width}}{_RESET}"
    if status == "HOT":        return f"{_GREEN}{'HOT':>{width}}{_RESET}"
    return f"{'─':>{width}}"


def fmt_valuation(grade: str, roe: float | None, width: int = 7) -> str:
    """Render '<G> <roe>%' (e.g. 'A 47%') with grade-coded color.
    grade: A/B/C/D/F/N/A; ROE shown only if numeric."""
    if grade == "N/A":
        return colored_cell("─", "", width)
    if grade == "F":
        return colored_cell("F 虧損", _GREEN, width)
    color = {"A": _RED + _BOLD, "B": _RED, "C": _CYA, "D": _GREEN}.get(grade, "")
    body = f"{grade} {roe:.0f}%" if roe is not None else grade
    return colored_cell(body, color, width)


def _draw_j_panel(J_view: list, n: int, height: int = 7) -> None:
    valid = [j for j in J_view if j is not None]
    if not valid: return
    j_max = max(max(valid) + 5, 100)
    j_min = min(min(valid) - 5, 0)
    rng = j_max - j_min
    def yp(v): return int(round((j_max - v) / rng * (height - 1)))
    grid = [[(' ', '')] * n for _ in range(height)]
    for ref_v, color in ((80, _RED), (50, ""), (20, _GREEN)):
        if j_min <= ref_v <= j_max:
            y = yp(ref_v)
            for i in range(n): grid[y][i] = ('─', _DIM)
    for i, j in enumerate(J_view):
        if j is None: continue
        y = yp(j)
        color = _RED if j > 80 else (_GREEN if j < 20 else _CYA)
        grid[y][i] = ('●', color)
    print(f"  J 指標 (KDJ)  範圍 {j_min:.0f} ~ {j_max:.0f}   ── 80超買 ── 50中線 ── 20超賣")
    label_at = {0: f"{j_max:>5.0f}", height - 1: f"{j_min:>5.0f}"}
    for ref in (80, 50, 20):
        if j_min <= ref <= j_max: label_at[yp(ref)] = f"{ref:>5}"
    for y in range(height):
        ylab = f"  {label_at.get(y, '     ')} ┤"
        line = ylab
        for ch, col in grid[y]:
            line += f"{col}{ch}{_RESET}" if col else ch
        print(line)
    print("        └" + "─" * n)


def draw_kline(ticker: str, full_ohlc: list[tuple[str, float, float, float, float]],
               days: int = 60, height: int = 22) -> None:
    if not full_ohlc:
        print(f"  ⚠ 無法取得 {ticker} 的歷史資料"); return
    closes = [c[4] for c in full_ohlc]
    ma10  = _sma(closes, 10);  ma20  = _sma(closes, 20)
    ma30  = _sma(closes, 30);  ma60  = _sma(closes, 60)
    ma120 = _sma(closes, 120)
    K_list, D_list, J_list = _kdj(full_ohlc, n=9)

    ohlc  = full_ohlc[-days:]
    ma10  = ma10[-days:];  ma20  = ma20[-days:];  ma30 = ma30[-days:]
    ma60  = ma60[-days:];  ma120 = ma120[-days:]
    J_view = J_list[-days:]
    n = len(ohlc)

    ma_vals = [v for ms in (ma10, ma20, ma30, ma60, ma120) for v in ms if v is not None]
    hi = max([c[2] for c in ohlc] + ma_vals)
    lo = min([c[3] for c in ohlc] + ma_vals)
    rng = hi - lo or hi * 0.01
    def yp(price): return int(round((hi - price) / rng * (height - 1)))

    grid = [[(' ', '')] * n for _ in range(height)]
    for i, (_, o, h, l, c) in enumerate(ohlc):
        color = _RED if c >= o else _GREEN
        y_high, y_low = yp(h), yp(l)
        y_btop, y_bbot = yp(max(o, c)), yp(min(o, c))
        for y in range(y_high, y_low + 1): grid[y][i] = ('│', color)
        if y_btop == y_bbot: grid[y_btop][i] = ('━', color)
        else:
            for y in range(y_btop, y_bbot + 1): grid[y][i] = ('█', color)

    for i in range(n):
        for ma_series, ch, color in ((ma10, '·', _YEL), (ma20, '+', _CYA),
                                     (ma30, '*', _MAG), (ma60, '○', _BLU),
                                     (ma120, '=', _WHT)):
            v = ma_series[i]
            if v is None: continue
            y = yp(v)
            cur_ch, _ = grid[y][i]
            if cur_ch == '█': continue
            grid[y][i] = (ch, color)

    label_rows = {0: hi, height - 1: lo, (height - 1) // 2: (hi + lo) / 2,
                  (height - 1) // 4: hi - rng * 0.25,
                  3 * (height - 1) // 4: lo + rng * 0.25}

    last_d, last_o, last_h, last_l, last_c = ohlc[-1]
    chg = last_c - last_o
    chg_color = _RED if chg > 0 else (_GREEN if chg < 0 else "")
    name = get_stock_name(ticker)
    label = f"{ticker}({name})" if name else ticker
    print(f"\n  {label}  {n}日 K 線  最新 {last_d}  "
          f"O:{last_o} H:{last_h} L:{last_l} C:{chg_color}{last_c}{_RESET}  "
          f"區間 {lo:.2f}~{hi:.2f}")
    def fmt_ma(v, color, label):
        return f"{color}{label}:{v:.2f}{_RESET}" if v is not None else f"{color}{label}:─{_RESET}"
    j_now = J_view[-1]
    j_color = _RED if j_now is not None and j_now > 80 else (_GREEN if j_now is not None and j_now < 20 else "")
    j_str = f"{j_color}J:{j_now:.1f}{_RESET}" if j_now is not None else "J:─"
    print(f"  {fmt_ma(ma10[-1], _YEL, 'MA10')}  {fmt_ma(ma20[-1], _CYA, 'MA20')}  "
          f"{fmt_ma(ma30[-1], _MAG, 'MA30')}  {fmt_ma(ma60[-1], _BLU, 'MA60')}  "
          f"{fmt_ma(ma120[-1], _WHT, 'MA120')}  {j_str}\n")

    for y in range(height):
        ylab = f"{label_rows[y]:>7.2f} ┤" if y in label_rows else "        │"
        line = ylab
        for ch, col in grid[y]:
            line += f"{col}{ch}{_RESET}" if col else ch
        print(line)
    print("        └" + "─" * n)
    every = max(1, n // 6)
    label_line = "         "; pos = 0
    while pos < n:
        d = ohlc[pos][0]
        date_str = f"{d[4:6]}/{d[6:8]}"
        label_line += date_str
        pos += len(date_str)
        next_label = (pos // every + 1) * every
        if next_label > pos:
            label_line += " " * (next_label - pos)
            pos = next_label
    print(label_line[:9 + n])
    print()
    _draw_j_panel(J_view, n)
    print()
    _draw_recent_prices(ohlc, days=10)
    print()
    _draw_action_hints(ohlc, ma10, ma20, ma30, ma60, ma120, J_view)
    print()


def _draw_recent_prices(ohlc: list[tuple], days: int = 10) -> None:
    rows = ohlc[-days:]
    if not rows: return
    col_w = 7
    print(f"  近 {len(rows)} 日價格")
    date_cells = [f"{r[0][4:6]}/{r[0][6:8]}" for r in rows]
    print("   日期 " + " ".join(f"{c:>{col_w}}" for c in date_cells))
    closes = [r[4] for r in rows]
    cells = []
    for i, c in enumerate(closes):
        prev = closes[i-1] if i > 0 else c
        padded = f"{c:g}".rjust(col_w)
        if   c > prev: cells.append(f"{_RED}{padded}{_RESET}")
        elif c < prev: cells.append(f"{_GREEN}{padded}{_RESET}")
        else:          cells.append(padded)
    print("   收盤 " + " ".join(cells))


def _draw_action_hints(ohlc: list[tuple], ma10, ma20, ma30, ma60, ma120, J_view) -> None:
    c = ohlc[-1][4]
    mas = [("MA10", ma10[-1] if ma10 else None, _YEL),
           ("MA20", ma20[-1] if ma20 else None, _CYA),
           ("MA30", ma30[-1] if ma30 else None, _MAG),
           ("MA60", ma60[-1] if ma60 else None, _BLU),
           ("MA120", ma120[-1] if ma120 else None, _WHT)]
    mas = [(l, v, col) for l, v, col in mas if v is not None]
    print("  動作建議")
    pos = []
    for label, v, col in mas:
        d = (c / v - 1) * 100
        sym, sc = ("↑", _RED) if c >= v else ("↓", _GREEN)
        pos.append(f"{sc}{sym}{label}{d:+.1f}%{_RESET}")
    print(f"   目前 {c:g}  " + "  ".join(pos))
    res = sorted([(l, v) for l, v, _ in mas if v > c], key=lambda x: x[1])
    sup = sorted([(l, v) for l, v, _ in mas if v <= c], key=lambda x: x[1], reverse=True)
    if res:
        print(f"   壓力 " + " > ".join(f"{l} {v:.2f}" for l, v in res))
    if sup:
        print(f"   支撐 " + " > ".join(f"{l} {v:.2f}" for l, v in sup))
    print()
    if res:
        l, v = res[0]
        print(f"   ▲ 站回 {v:.0f} ({l}) → 動能未斷，續抱")
    if sup and len(sup) >= 1:
        l, v = sup[0]
        print(f"   ◆ 守住 {v:.0f} ({l}) → 維持多頭結構，觀察")
    ma30_v = ma30[-1] if ma30 else None
    ma60_v = ma60[-1] if ma60 else None
    if ma30_v and c > ma30_v:
        print(f"   ▼ 跌破 {ma30_v:.0f} (MA30) → 動能轉弱，減碼 1/3")
    if ma60_v and c > ma60_v:
        print(f"   ✕ 跌破 {ma60_v:.0f} (MA60) → 趨勢反轉，全出")
    j = J_view[-1] if J_view else None
    if j is not None:
        if j > 80:
            print(f"   ⚠ J={j:.0f} 超買（強多頭中可鈍化 2-4 週；HOT 框架：看 MA60 斜率 + 外資 判讀）")
        elif j < 20:
            print(f"   ⚠ J={j:.0f} 超賣（等 BUY 條件：J 反彈 + 量縮 + 外資 5日 ≥ -2億）")
        else:
            print(f"   J={j:.0f} 中性")


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="台股投資組合損益查詢工具（純股票，不含基金）",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""\
範例：
  python3 check_stocks.py                     即時報價損益表（台股）
  python3 check_stocks.py -r                  同上 + 漲跌欄
  python3 check_stocks.py -r -S twse          改用 TWSE MIS 為資料源
  python3 check_stocks.py -d                  當年度台股股利
  python3 check_stocks.py -k 00981A           畫該股 60 日 K 線
  python3 check_stocks.py -k 2412 -n 90       中華電 90 日 K 線
  python3 check_stocks.py -k 2330 --download-history 250
                                              畫 K 線並把最近 250 日歷史存到 ./data/2330.json
  python3 check_stocks.py --backfill          回填 30 個交易日股票部位歷史
  python3 check_stocks.py --backfill 2026-04-15  回填指定單日
  python3 check_stocks.py -s pnl-pct          依損益% 排序
""",
    )
    p.add_argument("-s", "--sort", choices=["pnl-pct", "pnl-value", "change", "change-pct"],
                   metavar="SORTBY",
                   default=None,
                   help="排序（由高到低）。可用值：\n"
                        "\n"
                        "    pnl-pct      損益%% 由高到低\n"
                        "    pnl-value    損益金額（台幣）由高到低\n"
                        "    change       漲跌金額由高到低（需 -r 模式）\n"
                        "    change-pct   漲跌幅%% 由高到低（需 -r 模式）\n")
    p.add_argument("-d", "--dividends", action="store_true",
                   help="僅顯示當年度台股股利（已除息 ＋ 即將除息），不查股價。\n")
    p.add_argument("-r", "--realtime", action="store_true",
                   help="即時報價模式：用盤中即時價取代昨日收盤，多顯示\n"
                        "「現價/漲跌/漲跌幅」欄與技術欄（J/MA30/訊號）；\n"
                        "預設模式無此欄位。\n")
    p.add_argument("-S", "--source", choices=list(REALTIME_SOURCES.keys()),
                   metavar="SOURCE",
                   default="anue",
                   help="即時報價資料源（預設 anue；影響 -r 與 -k 今日 bar fallback）。\n"
                        "可用值：\n"
                        "\n"
                        "    anue      鉅亨 cnyes，單次批次最穩（預設）\n"
                        "    twse      TWSE MIS，官方來源，盤後可能無資料\n"
                        "    cmoney    CMoney，單檔請求需 cmkey\n")
    p.add_argument("-k", "--kline", metavar="TICKER",
                   help="畫指定股票的日 K 線圖（紅漲綠跌）。K 線末端會自動拼接\n"
                        "今日即時 bar（TWSE MIS 真 OHLC，失敗 fallback 到 --source\n"
                        "合成）。下方附「近 10 日價格表」與「動作建議」。\n")
    p.add_argument("-n", "--days", type=int, default=60, metavar="DAYS",
                   help="K 線天數（預設 60；搭配 -k；資料來源最多回傳約 250 日）。\n")
    p.add_argument("--download-history", type=int, metavar="DAYS",
                   help="搭配 -k：把該股最近 DAYS 日歷史資料（OHLCV ＋ 外資買賣超）\n"
                        "存到 ./data/<TICKER>.json，可供回測重複讀取。\n"
                        "資料來源依 DAYS 大小自動切換：\n"
                        "\n"
                        "    DAYS ≤ 250    CMoney 一次拿齊（含外資籌碼）\n"
                        "    DAYS > 250    TWSE STOCK_DAY 按月回填 ＋ CMoney 補近期外資\n"
                        "                  （可撈 5 年以上 ≈ 1250 日，無外資的舊 bar 補 0）\n"
                        "\n"
                        "今日合成 bar 不入檔，僅 completed bars。\n")
    p.add_argument("--backfill", nargs='?', const='30', metavar='ARG',
                   help="回填股票部位 grand total 到 history_stocks.json。\n"
                        "可用值：\n"
                        "\n"
                        "    (無參數)        回填過去 30 個交易日\n"
                        "    N               回填過去 N 個交易日（整數）\n"
                        "    YYYY-MM-DD      回填指定單日\n")
    p.add_argument("--signals", action="store_true",
                   help="掃描 portfolio.json 內持股 ＋ 追蹤股的進場/出場訊號。\n"
                        "訊號類型：\n"
                        "\n"
                        "    +BUY     BUY ＋ MA200 不下跌 ＋ 收盤 ≥ MA150（最強）\n"
                        "    BUY      J 從 <20 反彈 ＋ 底部 1/3 ＋ 外資不撤 ＋ 量縮\n"
                        "    +HOT     急漲 ＋ M60 距離大 ＋ 量爆 ＋ 籌碼動能（多頭續抱）\n"
                        "    HOT      J>80 ＋ 收盤>MA10（短線超買）\n"
                        "    WATCH    J<20 但 BUY 條件未湊齊\n")
    return p.parse_args()


def _sort_key(sort, pnl_idx, pct_idx, change_idx=None, changepct_idx=None):
    if sort == "pnl-pct":    return lambda x: x[pct_idx]
    if sort == "change"     and change_idx    is not None: return lambda x: x[change_idx]
    if sort == "change-pct" and changepct_idx is not None: return lambda x: x[changepct_idx]
    return lambda x: x[pnl_idx]


# ── Backfill (stocks only) ───────────────────────────────────────────────────

def backfill_stocks(portfolio: dict, days: int | None = 30, target_date: str | None = None) -> None:
    held = [s for s in portfolio["stocks"] if s.get("shares", 0) > 0]
    print("  抓取股票歷史 K 線...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        kline_futs = {s["ticker"]: ex.submit(get_daily_kline, s["ticker"]) for s in held}
    klines = {t: f.result() for t, f in kline_futs.items()}

    # Split-adjusted close series
    stock_close: dict[str, dict[str, float]] = {}
    for t, rows in klines.items():
        if not rows:
            stock_close[t] = {}; continue
        adj_closes: list[tuple[str, float]] = []
        scale = 1.0
        prev_c = rows[-1][4]
        for d, _o, _h, _l, c in reversed(rows):
            ratio = prev_c / c if c else 1.0
            if ratio > 2.0 or ratio < 0.5:
                scale *= ratio
            iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            adj_closes.append((iso, c * scale))
            prev_c = c
        stock_close[t] = dict(adj_closes)

    if not stock_close:
        print("  ⚠ 無歷史股價，無法回填"); return
    sample_dates = sorted(next(iter(stock_close.values())).keys())
    today_iso = date.today().isoformat()
    if target_date:
        if target_date not in set(sample_dates):
            print(f"  ⚠ {target_date} 非交易日或無資料（範例股無此日 K 線），跳過"); return
        target_dates = [target_date]
    else:
        target_dates = [d for d in sample_dates if d <= today_iso][-(days or 30):]

    stock_dates_sorted = {t: sorted(m.keys()) for t, m in stock_close.items()}
    def price_on_or_before(ticker: str, target: str) -> float | None:
        dates = stock_dates_sorted.get(ticker, [])
        applicable = [d for d in dates if d <= target]
        return stock_close[ticker][applicable[-1]] if applicable else None

    snapshots = []
    total_cost = sum(s["total_cost_twd"] for s in held)
    for d in target_dates:
        v = 0.0; skip = False
        for s in held:
            price = price_on_or_before(s["ticker"], d)
            if price is None: skip = True; break
            v += price * s["shares"]
        if skip: continue
        snapshots.append({"date": d, "current": round(v, 2), "cost": round(total_cost, 2)})

    merge_history(snapshots)
    if target_date:
        if snapshots:
            print(f"  ✓ 已回填 {target_date} 到 history_stocks.json")
        else:
            print(f"  ⚠ {target_date} 計算失敗（持股缺資料）")
    else:
        print(f"  ✓ 已回填 {len(snapshots)} 個交易日到 history_stocks.json")


# ── Signals scanner (shared by --signals and scan_signals.py) ────────────────

def scan_signals(tickers: list[str], names: dict | None = None) -> list[dict]:
    """Run compute_signal in parallel for every ticker. Returns list of dicts."""
    names = names or {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {t: ex.submit(compute_signal, t) for t in tickers}
    out = []
    for t, f in futs.items():
        sig = f.result() or {"ticker": t, "status": None}
        sig["name"] = names.get(t, "")
        out.append(sig)
    return out


def print_signals_table(signals: list[dict], title: str = "進場訊號掃描") -> None:
    """Render signals table with CJK-aware visual width alignment."""
    # Column widths (visual cols)
    W_T, W_N, W_C, W_J, W_M, W_D, W_S, W_F, W_F2, W_V, W_SIG = 8, 20, 8, 4, 7, 8, 8, 8, 9, 7, 5

    print(f"\n{'═'*128}")
    print(f"  {title}")
    print(f"  條件: +BUY = BUY + MA200 不下跌 + 收盤 ≥ MA150 (中長期多頭結構，最強)")
    print(f"        BUY  = J 從 <20 反彈 + 收盤位於 30 日底部 1/3 + 外資 5日 ≥ -2億 + 量縮 (5/30日比 ≤ 80%)")
    print(f"        WATCH = J<20 但 BUY 條件未湊齊 (J 未反彈 / 非底部 / 外資賣超 / 量未縮)")
    print(f"        +HOT = 急漲(>8%) + 遠離 MA60(>12%) + 外資衰退率<30% + 量爆 + J>75 (動能突破，多頭續抱)")
    print(f"        HOT  = J>80 + 收盤>MA10 (短線超買；HOT note 會附 MA60 斜率與外資狀態)")
    print(f"        ⚠ MA200 下降警示會附在 BUY 後（提示中長期結構偏弱）")
    print(f"  估值: 評分 = PE 桶(0-4) + ROE 桶(0-4)，A=7-8, B=5-6, C=3-4, D=0-2；ROE = PB / PE × 100%")
    print(f"        PE  桶: ≤12→4, ≤18→3, ≤25→2, ≤40→1, >40→0；ROE 桶: >25%→4, >15→3, >10→2, >5→1, ≤5→0")
    print(f"        F=虧損(PE≤0), ─=ETF/無資料")
    print(f"  M60距: (收盤 / MA60 - 1)，正值=站上均線，負值=跌破均線；±2% 內為「貼線」")
    print(f"{'═'*128}")
    SEP = " "  # single-space gap between columns
    # Header (all CJK-aware)
    print("  " + cjk_ljust('代號', W_T) + cjk_ljust('股名', W_N)
          + cjk_rjust('收盤', W_C) + SEP + cjk_rjust('J', W_J) + SEP
          + cjk_rjust('MA30', W_M) + SEP + cjk_rjust('MA60', W_M) + SEP
          + cjk_rjust('M60距', W_D) + SEP + cjk_rjust('M60斜率', W_S) + SEP
          + cjk_rjust('外資5d', W_F) + SEP + cjk_rjust('外資30d', W_F2) + SEP
          + cjk_rjust('估值', W_V)
          + "  " + cjk_rjust('訊號', W_SIG) + "  說明")
    sep_line = ("  " + "─"*W_T + "─"*W_N + "─"*W_C + " " + "─"*W_J + " "
                + "─"*W_M + " " + "─"*W_M + " " + "─"*W_D + " " + "─"*W_S + " "
                + "─"*W_F + " " + "─"*W_F2 + " " + "─"*W_V + "  " + "─"*W_SIG + "  " + "─"*30)
    print(sep_line)

    order = {"STRONG_BUY": 0, "BUY": 1, "MOMENTUM": 2, "WATCH": 3, "HOT": 4, None: 5}
    signals.sort(key=lambda x: (order.get(x.get("status"), 6), x.get("ticker", "")))

    for sig in signals:
        t = sig.get("ticker", "")
        n = sig.get("name", "")
        # Insufficient data row: span numeric cols with ─
        if sig.get("status") is None and not sig.get("close"):
            blank = " " * (W_C + W_J + W_M + W_M + W_D + W_S + W_F + W_F2 + W_V + 8)  # 8 = inter-column spaces
            print("  " + cjk_ljust(t, W_T) + cjk_ljust(n, W_N) + blank
                  + "  " + colored_cell("─", "", W_SIG) + "  " + sig.get('note', ''))
            continue

        close = sig.get("close", 0)
        j = sig.get("j", 50)
        ma30 = sig.get("ma30", 0) or 0
        ma60 = sig.get("ma60", 0) or 0
        ma60_dist = sig.get("ma60_dist")
        slope = sig.get("ma60_slope", 0)
        fx5 = sig.get("fx_5d", 0)
        fx30 = sig.get("fx_30d", 0)
        status = sig.get("status")
        note = sig.get("note", "")

        # Each cell: value_str → padded → optionally colored
        close_cell = cjk_rjust(f"{close:.2f}", W_C)
        j_color = _RED if j > 80 else (_GREEN if j < 25 else "")
        j_cell = colored_cell(f"{j:.0f}", j_color, W_J)
        ma30_cell = cjk_rjust(f"{ma30:.2f}", W_M)
        ma60_cell = cjk_rjust(f"{ma60:.2f}", W_M)
        # M60距 cell: red if above (bullish), green if below (bearish);
        # near-line (|dist| ≤ 2%) shown without color emphasis.
        if ma60_dist is None:
            dist_cell = cjk_rjust("─", W_D)
        else:
            dist_color = _RED if ma60_dist > 2 else (_GREEN if ma60_dist < -2 else "")
            dist_cell = colored_cell(f"{ma60_dist:+.2f}%", dist_color, W_D)
        slope_color = _RED if slope > 0 else (_GREEN if slope < -0.1 else "")
        slope_cell = colored_cell(f"{slope:+.2f}%", slope_color, W_S)
        fx5_color = _RED if fx5 > 0 else (_GREEN if fx5 < -2 else "")
        fx5_cell = colored_cell(f"{fx5:+.2f}", fx5_color, W_F)
        fx30_cell = cjk_rjust(f"{fx30:+.2f}", W_F2)
        val_cell = fmt_valuation(sig.get("grade", "N/A"), sig.get("roe"), W_V)
        sig_cell = fmt_signal(status, W_SIG)

        SEP = " "
        print("  " + cjk_ljust(t, W_T) + cjk_ljust(n, W_N)
              + close_cell + SEP + j_cell + SEP + ma30_cell + SEP + ma60_cell + SEP
              + dist_cell + SEP + slope_cell + SEP + fx5_cell + SEP + fx30_cell + SEP
              + val_cell + "  " + sig_cell + "  " + note)

    print(f"{'═'*128}")
    n_strong = sum(1 for s in signals if s.get("status") == "STRONG_BUY")
    n_buy    = sum(1 for s in signals if s.get("status") == "BUY")
    n_mom    = sum(1 for s in signals if s.get("status") == "MOMENTUM")
    n_watch  = sum(1 for s in signals if s.get("status") == "WATCH")
    n_hot    = sum(1 for s in signals if s.get("status") == "HOT")
    print(f"  訊號統計: +BUY={n_strong}, BUY={n_buy}, +HOT={n_mom}, WATCH={n_watch}, HOT={n_hot}, 共掃 {len(signals)} 檔\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    # K-line mode (no portfolio needed)
    if args.kline:
        full_rows, ohlc, _ = fetch_kline_with_intraday(args.kline, args.source)
        draw_kline(args.kline, ohlc, days=args.days, height=22)
        if args.download_history:
            req, got = save_kline_history(args.kline, full_rows, args.download_history)
            print(f"✓ Saved {got} days of {args.kline} → ./data/{args.kline}.json")
            if got < req:
                print(f"  ⚠ 實際回傳 {got} 日（< 要求 {req}）— 可能該股上市未滿，或 TWSE 也沒更早資料")
        return

    # Signals mode
    if args.signals:
        with open(PORTFOLIO_PATH, encoding='utf-8') as f:
            portfolio = json.load(f)["portfolio"]
        tickers = [s["ticker"] for s in portfolio["stocks"]]
        names = {s["ticker"]: s.get("name", "") for s in portfolio["stocks"]}
        signals = scan_signals(tickers, names)
        print_signals_table(signals, title=f"持股+追蹤股訊號掃描 — {date.today().isoformat()}")
        return

    # Backfill mode
    if args.backfill is not None:
        val = args.backfill
        target_date, days = None, None
        if re.match(r'^\d{4}-\d{2}-\d{2}$', val):
            target_date = val
        else:
            try:
                days = int(val)
                if days <= 0: raise ValueError
            except ValueError:
                print(f"  ⚠ --backfill 接受 YYYY-MM-DD 或正整數，收到: {val!r}")
                return
        with open(PORTFOLIO_PATH, encoding='utf-8') as f:
            portfolio = json.load(f)["portfolio"]
        if target_date:
            print(f"回填指定日期 {target_date} 的股票部位...")
        else:
            print(f"回填過去 {days} 個交易日的股票部位...")
        backfill_stocks(portfolio, days=days, target_date=target_date)
        return

    with open(PORTFOLIO_PATH, encoding='utf-8') as f:
        portfolio = json.load(f)["portfolio"]
    today    = date.today().isoformat()
    div_year = date.today().year

    # Dividends mode
    if args.dividends:
        with ThreadPoolExecutor() as ex:
            div_futures = {s["ticker"]: ex.submit(get_cmoney_dividends, s["ticker"], div_year)
                           for s in portfolio["stocks"]}
        stock_divs = {ticker: fut.result() for ticker, fut in div_futures.items()}

        today_date = date.today()
        HDR = f"  {cjk_ljust('股名(代號)', 25)}  {'除息日':>7}  {'每股(元)':>7}  {'股數':>6}  {'股利金額':>10}"
        SEP = f"  {'─'*25}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*13}"

        def _div_rows(upcoming: bool):
            rows, total = [], 0.0
            for s in portfolio["stocks"]:
                if not s["shares"]: continue
                label = f"{s.get('name', '')}({s['ticker']})"
                for div_date, per_share in stock_divs.get(s["ticker"], []):
                    ex = date.fromisoformat(div_date.replace("/", "-"))
                    if (ex > today_date) != upcoming: continue
                    amount = per_share * s["shares"]
                    total += amount
                    rows.append((label, div_date, per_share, s["shares"], amount))
            return rows, total

        past_rows, past_total = _div_rows(upcoming=False)
        upcoming_rows, upcoming_total = _div_rows(upcoming=True)

        print(f"\n{'═'*80}")
        print(f"  DIVIDENDS  (台股股利 — {div_year})  ─  {today}")
        print(f"{'═'*80}")
        print(f"\n  已除息")
        print(HDR); print(SEP)
        for label, div_date, per_share, shares, amount in past_rows:
            print(f"  {cjk_ljust(label, 25)}  {div_date:>10}  {per_share:>8.4f}  {shares:>8,}  {twd(amount)}")
        print(f"\n  已除息合計: {twd(past_total)}")
        print(f"\n  即將除息")
        print(HDR); print(SEP)
        if upcoming_rows:
            for label, div_date, per_share, shares, amount in upcoming_rows:
                print(f"  {cjk_ljust(label, 25)}  {div_date:>10}  {per_share:>8.4f}  {shares:>8,}  {twd(amount)}")
        else:
            print(f"  （尚無已公告的除息資料）")
        print(f"\n  即將除息合計: {twd(upcoming_total)}")
        print(f"\n  {div_year} 年度股利合計: {twd(past_total + upcoming_total)}")
        print(f"{'═'*80}\n")
        return

    # Realtime mode (with change + tech + signal columns)
    if args.realtime:
        stock_tickers = [s["ticker"] for s in portfolio["stocks"]]
        source_label, fetcher = REALTIME_SOURCES[args.source]
        with ThreadPoolExecutor(max_workers=8) as ex:
            rt_future = ex.submit(fetcher, stock_tickers)
            sig_futures = {t: ex.submit(compute_signal, t) for t in stock_tickers}
        rt_data = rt_future.result()
        sig_data = {t: f.result() for t, f in sig_futures.items()}

        W = 108
        print(f"\n{'═'*W}")
        print(f"  STOCKS  即時報價  ─  {today}  ─  來源: {source_label}")
        print(f"{'═'*W}")
        print(f"  {cjk_ljust('股名(代號)', 25)}  {cjk_rjust('股數',8)}  {cjk_rjust('現價',8)}  {cjk_rjust('漲跌',7)}  {cjk_rjust('漲跌幅',7)}  {cjk_rjust('J',3)}  {cjk_rjust('MA30',4)}  {cjk_rjust('現值',13)}  {cjk_rjust('成本',13)}  {cjk_ljust('損益',23)}  {cjk_rjust('訊號',5)}")
        print(f"  {'─'*25}  {'─'*8}  {'─'*8}  {'─'*7}  {'─'*7}  {'─'*3}  {'─'*4}  {'─'*13}  {'─'*13}  {'─'*23}  {'─'*5}")

        stock_rows, unavailable = [], []
        for s in portfolio["stocks"]:
            pair = rt_data[s["ticker"]]
            if pair is None:
                unavailable.append(s["ticker"]); continue
            price, prev = pair
            change = round(price - prev, 2)
            change_pct = change / prev * 100 if prev else 0.0
            value = price * s["shares"]
            pnl = value - s["total_cost_twd"]
            pnl_pct = pnl / s["total_cost_twd"] if s["total_cost_twd"] else 0.0
            stock_rows.append((s, price, prev, change, change_pct, value, pnl, pnl_pct))

        if args.sort:
            stock_rows.sort(key=_sort_key(args.sort, 6, 7, change_idx=3, changepct_idx=4), reverse=True)

        stock_current = 0.0
        stock_cost = sum(s["total_cost_twd"] for s in portfolio["stocks"] if s["shares"])
        for s, price, prev, change, change_pct, value, pnl, _pct in stock_rows:
            label = f"{s.get('name', '')}({s['ticker']})"
            price_str = _colorize(f"{price:>8.2f}", change)
            chg_str = _colorize(f"{change:>+7.2f}", change)
            chg_pct_str = _colorize(f"{change_pct:>+6.2f}%", change)
            sig = sig_data.get(s["ticker"]) or {}
            j_val = sig.get("j")
            ma30 = sig.get("ma30")
            ma_status = ('up' if ma30 and price >= ma30 else 'down') if ma30 else None
            tech_str = f"{fmt_j(j_val)}  {fmt_ma_status(ma_status)}"
            sig_str = fmt_signal(sig.get("status"), 5)
            if s["shares"]:
                stock_current += value
                tail = f"{twd(value)}  {twd(s['total_cost_twd'])}  {pnl_str(pnl, s['total_cost_twd'])}"
            else:
                tail = TRACKING_CELLS
            print(f"  {cjk_ljust(label, 25)}  {s['shares']:>8,}  {price_str}  {chg_str}  {chg_pct_str}  {tech_str}  {tail}  {sig_str}")

        if unavailable:
            print(f"\n  ⚠ 無法取得報價: {', '.join(unavailable)}")

        prev_snap = update_history(today, stock_current, stock_cost)
        cur_pnl = stock_current - stock_cost
        print(f"\n  台股總計  現值: {twd(stock_current)}  成本: {twd(stock_cost)}  損益: {pnl_str(cur_pnl, stock_cost)}")
        if prev_snap:
            prev_pnl = prev_snap["current"] - prev_snap["cost"]
            pnl_diff = cur_pnl - prev_pnl
            pnl_diff_pct = pnl_diff / prev_snap["cost"] * 100 if prev_snap["cost"] else 0
            s = f"NT${pnl_diff:>+11,.0f} ({pnl_diff_pct:+5.2f}%)"
            print(f"  vs {prev_snap['date']}: {_colorize(s, pnl_diff)}")
            cost_diff = stock_cost - prev_snap["cost"]
            if abs(cost_diff) >= 1:
                sign = "加碼" if cost_diff > 0 else "減碼"
                print(f"  (期間{sign} {twd(abs(cost_diff))})")
        print(f"{'═'*W}\n")
        return

    # Default mode (no realtime change columns; uses Anue close)
    held = [s for s in portfolio["stocks"] if s.get("shares", 0) > 0]
    rt_data = get_anue_realtime([s["ticker"] for s in portfolio["stocks"]])
    all_close = {t: (pair[0] if pair else None) for t, pair in rt_data.items()}

    print(f"\n{'═'*80}")
    print(f"  Stocks Portfolio  ─  {today}")
    print(f"{'═'*80}")
    print(f"\n  STOCKS  (台股)")
    print(f"  {cjk_ljust('股名(代號)', 25)}  {'股數':>6}  {'現價':>6}  {'現值':>11}  {'成本':>11}  損益")
    print(f"  {'─'*25}  {'─'*8}  {'─'*8}  {'─'*13}  {'─'*13}  {'─'*23}")

    stock_current = 0.0
    stock_cost = sum(s["total_cost_twd"] for s in held)
    unavailable = []
    stock_rows = []
    for s in held:
        price = all_close.get(s["ticker"])
        if price is None:
            unavailable.append(s["ticker"]); continue
        value = price * s["shares"]
        pnl = value - s["total_cost_twd"]
        pnl_pct = pnl / s["total_cost_twd"] if s["total_cost_twd"] else 0.0
        stock_rows.append((s, price, value, pnl, pnl_pct))
    if args.sort:
        stock_rows.sort(key=_sort_key(args.sort, 3, 4), reverse=True)

    for s, price, value, pnl, _pct in stock_rows:
        stock_current += value
        label = f"{s.get('name', '')}({s['ticker']})"
        print(f"  {cjk_ljust(label, 25)}  {s['shares']:>8,}  {price:>8.2f}  {twd(value)}  {twd(s['total_cost_twd'])}  {pnl_str(pnl, s['total_cost_twd'])}")
    if unavailable:
        print(f"\n  ⚠ 無法取得報價: {', '.join(unavailable)}")

    total_pnl = stock_current - stock_cost
    prev_snap = update_history(today, stock_current, stock_cost)
    print(f"\n{'═'*80}")
    print("  台股總計")
    print(f"  現值合計  : {twd(stock_current)}")
    print(f"  成本合計  : {twd(stock_cost)}")
    print(f"  損益合計  : {pnl_str(total_pnl, stock_cost)}")
    if prev_snap:
        prev_pnl = prev_snap["current"] - prev_snap["cost"]
        pnl_diff = total_pnl - prev_pnl
        pnl_diff_pct = pnl_diff / prev_snap["cost"] * 100 if prev_snap["cost"] else 0
        s = f"NT${pnl_diff:>+11,.0f} ({pnl_diff_pct:+5.2f}%)"
        print(f"  vs {prev_snap['date']}: {_colorize(s, pnl_diff)}")
        cost_diff = stock_cost - prev_snap["cost"]
        if abs(cost_diff) >= 1:
            sign = "加碼" if cost_diff > 0 else "減碼"
            print(f"  (期間{sign} {twd(abs(cost_diff))})")
    print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()
