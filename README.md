# check_stocks

A terminal-based Taiwan stock portfolio tracker. Reads holdings from `portfolio.json`,
fetches live quotes from public APIs, and prints colored P&L tables, dividend
schedules, candlestick charts with technical indicators, and daily snapshot history —
all without leaving your shell.

```
═════════════════════════════════════════════════════════════════════════
  Stocks Portfolio  ─  2026-01-15
═════════════════════════════════════════════════════════════════════════

  STOCKS  (台股)
  股名(代號)                      股數      現價            現值            成本  損益
  ──────────────────────────  ────────  ────────  ──────────────  ──────────────
  元大台灣50(0050)                 100     95.75  NT$      9,575  NT$      9,000  NT$    +575 (  +6.4%)
  元大高股息(0056)               5,000     43.86  NT$    219,300  NT$    175,000  NT$ +44,300 ( +25.3%)
  國泰永續高股息(00878)          5,000     27.33  NT$    136,650  NT$    110,000  NT$ +26,650 ( +24.2%)
  台積電(2330)                     100   2250.00  NT$    225,000  NT$    150,000  NT$ +75,000 ( +50.0%)
  中華電(2412)                   1,000    136.00  NT$    136,000  NT$    110,000  NT$ +26,000 ( +23.6%)
  兆豐金(2886)                   5,000     39.55  NT$    197,750  NT$    160,000  NT$ +37,750 ( +23.6%)
  第一金(2892)                   5,000     28.80  NT$    144,000  NT$    110,000  NT$ +34,000 ( +30.9%)

  台股總計
  現值合計  : NT$  1,068,275
  成本合計  : NT$    824,000
  損益合計  : NT$   +244,275 ( +29.6%)
  vs 2026-01-14: NT$    +1,896 ( +0.23%)
```

(Example based on `portfolio.json.sample` — your actual numbers will look similar with your own holdings.)

## Features

- **Live quote loss/gain table** — current value, cost basis, P&L per holding
- **Real-time mode** (`-r`) — adds intraday change columns with red-up / green-down coloring (Taiwan convention), plus J-value and MA30 status
- **Three quote sources** (`-S`) — Anue (cnyes, default), TWSE MIS, CMoney; switch on the fly
- **Dividend schedule** (`-d`) — current-year ex-dividend dates split into "已除息" and "即將除息"
- **Candlestick chart** (`-k`) — ASCII K-line with MA10/20/30/60/120 overlay and KDJ J-value panel
- **Signal scanner** (`--signals`) — combines J-value rebound, 30-day position, foreign-investor flow, and volume contraction into +BUY / BUY / WATCH / HOT classifications, with an A/B/C/D valuation grade per stock
- **Standalone watchlist scanner** (`scan_signals.py`) — same signal logic for any tickers without touching your portfolio
- **Stock-split detection** — backfill auto-rescales pre-split closes to keep the price series continuous
- **Daily snapshot history** — every run records the day's stock total to `history_stocks.json` and shows the diff vs the previous trading day
- **Backfill** (`--backfill`) — reconstruct the past 30 (or N) days, or fill a specific missing date
- **Tracking-only stocks** (`shares: 0`) — watchlist entries that show price/change but don't affect totals
- **Sort** (`-s`) — order rows by P&L %, P&L value, intraday change, or change %

## Requirements

- Python 3.10+
- `requests`

```bash
pip install requests
```

## Quick Start

```bash
# 1. Create portfolio.json (see schema below)
# 2. Run any mode:

python3 check_stocks.py                # Loss/gain table for all holdings
python3 check_stocks.py -r             # With intraday change columns
python3 check_stocks.py -d             # Current-year dividend schedule
python3 check_stocks.py -k 2412        # 60-day K-line for 中華電
python3 check_stocks.py --signals      # Buy/watch/hot signal scan + valuation
python3 check_stocks.py --backfill     # Backfill 30 days of snapshots

python3 scan_signals.py 2330 2412      # Signal scan for arbitrary tickers
python3 scan_signals.py -f watchlist.txt   # Read tickers from a file
```

## Usage

### Default mode — daily P&L table

```bash
python3 check_stocks.py
```

Prints each holding with `現價 / 現值 / 成本 / 損益`, plus a portfolio total
and the diff vs the previous trading day's snapshot. Tracking-only entries
(`shares: 0`) are skipped here.

### Real-time mode `-r`

```bash
python3 check_stocks.py -r
```

Same as default plus intraday change columns (`漲跌 / 漲跌幅`, colored), and
three technical-indicator columns:

| Column | Meaning |
|---|---|
| `J` | KDJ J-value. Red if > 80 (overbought), green if < 20 (oversold), `─` if too few data points |
| `MA30` | Red `↑` if last close ≥ 30-day moving average; green `↓` if below; `─` if < 30 days history |
| `訊號` | `+BUY` / `BUY` (red) / `WATCH` (cyan) / `HOT` (green warning) / `─` neutral — see [Signal scan](#signal-scan---signals-and-scan_signalspy) below for the full criteria |

Each row's price gets red/green coloring based on direction. Tracking-only
entries appear with their quote and technical indicators, but their
value/cost/P&L cells are placeholders.

K-line data is fetched in parallel with quotes, adding ~1-2 seconds.

Example (sample portfolio):

```
═════════════════════════════════════════════════════════════════════════════════════════════════════
  STOCKS  即時報價  ─  2026-01-15  ─  來源: Anue 鉅亨
═════════════════════════════════════════════════════════════════════════════════════════════════════
  股名(代號)                      股數      現價     漲跌   漲跌幅    J  MA30            現值            成本  損益
  ──────────────────────────  ────────  ────────  ───────  ───────  ───  ────  ──────────────  ──────────────  ─────────────────────
  元大台灣50(0050)                 100     95.75    +1.95   +2.08%   97    ↑   NT$      9,575  NT$      9,000  NT$       +575 (  +6.4%)
  元大高股息(0056)               5,000     43.86    +0.86   +2.00%   88    ↑   NT$    219,300  NT$    175,000  NT$    +44,300 ( +25.3%)
  國泰永續高股息(00878)          5,000     27.33    +0.55   +2.05%   92    ↑   NT$    136,650  NT$    110,000  NT$    +26,650 ( +24.2%)
  台積電(2330)                     100   2250.00   +25.00   +1.12%   85    ↑   NT$    225,000  NT$    150,000  NT$    +75,000 ( +50.0%)
  中華電(2412)                   1,000    136.00    -0.50   -0.37%   22    ↑   NT$    136,000  NT$    110,000  NT$    +26,000 ( +23.6%)
  兆豐金(2886)                   5,000     39.55    +0.10   +0.25%   42    ↑   NT$    197,750  NT$    160,000  NT$    +37,750 ( +23.6%)
  第一金(2892)                   5,000     28.80    +0.40   +1.41%   86    ↑   NT$    144,000  NT$    110,000  NT$    +34,000 ( +30.9%)
  聯發科(2454)                       0   1485.00    +5.00   +0.34%   78    ↑               ─               ─  ─       追蹤標的
  群聯(8299)                         0   2325.00   +20.00   +0.87%   65    ↑               ─               ─  ─       追蹤標的

  台股總計  現值: NT$  1,068,275  成本: NT$    824,000  損益: NT$   +244,275 ( +29.6%)
  vs 2026-01-14: NT$    +1,896 ( +0.23%)
```

In the actual terminal output, columns are colored: red for gains/overbought,
green for losses/oversold (Taiwan stock convention).

### Quote sources `-S`

| Source | Speed | Notes |
|---|---|---|
| `anue` (default) | Fast | Single batch request, very stable, intraday updates |
| `twse` | Fast | Official TWSE MIS, single batch |
| `cmoney` | Slower | One request per ticker; needs a session cookie probe; concurrency capped at 4 |

```bash
python3 check_stocks.py -r -S twse
python3 check_stocks.py -r -S cmoney
```

### Dividend schedule `-d`

```bash
python3 check_stocks.py -d
```

Lists current-year cash dividends from CMoney for every held stock, split
into "已除息" (past) and "即將除息" (announced & upcoming). Tracking-only
entries are skipped.

### K-line chart `-k`

```bash
python3 check_stocks.py -k 2412          # 60 days (default)
python3 check_stocks.py -k 2412 -n 90    # 90 days
python3 check_stocks.py -k 6531 -n 30    # 30 days
```

ASCII candlestick (red up / green down per Taiwan convention), with five
moving averages overlaid on the price chart and a separate KDJ J-value panel
below.

```
  2412(中華電)  90日 K 線  最新 20260506  O:135.5 H:136.0 L:135.0 C:136.0  區間 129.50~138.00
  MA10:136.30  MA20:135.88  MA30:135.27  MA60:135.07  MA120:133.32  J:16.5
```

| Marker | Color | Meaning |
|---|---|---|
| `█` / `│` / `━` | red/green | Body / wick / doji |
| `·` | yellow | MA10 |
| `+` | cyan | MA20 |
| `*` | magenta | MA30 |
| `○` | blue | MA60 |
| `=` | white | MA120 |
| `●` | red/green/cyan | KDJ J value (overbought / oversold / neutral) |

K-line works for any TWSE/TPEx ticker — the stock doesn't need to be in
your portfolio.

### Backfill `--backfill`

```bash
python3 check_stocks.py --backfill              # past 30 trading days
python3 check_stocks.py --backfill 60           # past 60 trading days
python3 check_stocks.py --backfill 2026-04-15   # one specific date
```

Reconstructs historical stock-total values and writes them to
`history_stocks.json` (deduped, last 30 entries kept).

Notes:
- Stock prices come from CMoney's daily K-line (full history available)
- Cost basis is held constant at the current value (no lot-level history)
- Stock splits are auto-detected (consecutive close ratio > 2× or < 0.5×) and earlier prices rescaled
- Trading suspensions (e.g. during a split) use carry-forward (last known close)

### Signal scan `--signals` and `scan_signals.py`

```bash
python3 check_stocks.py --signals          # scan all holdings + tracking entries
python3 scan_signals.py 2330 2412 2885     # scan arbitrary tickers
python3 scan_signals.py -f watchlist.txt   # read tickers from a file
python3 scan_signals.py                    # default: read ./watchlist.txt
```

Each row shows close, J, MA30, MA60, MA60 5-day slope, foreign-investor net flow
(5d/30d), valuation grade, and a signal column.

**Signal classifications**:

| Signal | Condition |
|---|---|
| **+BUY** (STRONG_BUY) | BUY plus MA200 5-day slope ≥ 0 plus close ≥ MA150 (long-term uptrend confirmed) |
| **BUY** | J rebounded from < 20 in last 5 days (today J ≥ 20 and rising) + close in bottom 1/3 of 30-day range + foreign 5d net ≥ −2 億 + volume contraction (5d/30d ≤ 80%) |
| **WATCH** | J < 20 but BUY conditions not all met (lists which ones failed) |
| **HOT** | J > 80 + close > MA10 (short-term overbought — consider trimming) |
| `─` | No strong signal |

⚠ A "MA200 下降" warning is appended to BUY when MA200 5-day slope < −0.1% (long-term structure weak).

**Valuation grade** (A/B/C/D), shown next to the signal:

Formula: `PE bucket (0–4) + ROE bucket (0–4)` → A=7–8, B=5–6, C=3–4, D=0–2.
ROE is derived as `PB / PE × 100%` (algebraically equivalent to EPS/BPS).

| Bucket | PE | Score | ROE | Score |
|---|---|---|---|---|
| 4 | ≤ 12 (very cheap) | 4 | > 25% (excellent) | 4 |
| 3 | ≤ 18 (cheap) | 3 | > 15% (good) | 3 |
| 2 | ≤ 25 (fair) | 2 | > 10% (decent) | 2 |
| 1 | ≤ 40 (expensive) | 1 | > 5% (weak) | 1 |
| 0 | > 40 (very expensive) | 0 | ≤ 5% (poor) | 0 |

Display: A red bold, B red, C cyan, D green, `F 虧損` green (PE ≤ 0, loss-making),
`─` for ETFs and stocks without PE/PB data.

PE/PB data comes from CMoney `DtNo=71872` (single batch call covering ~1,500
listed/OTC individual stocks; ETFs not included).

### Sort `-s`

| Value | Effect |
|---|---|
| `pnl-pct` | By P&L % (high to low) |
| `pnl-value` | By P&L value in TWD |
| `change` | By intraday absolute change (only with `-r`) |
| `change-pct` | By intraday change % (only with `-r`) |

```bash
python3 check_stocks.py -s pnl-pct
python3 check_stocks.py -r -s change-pct
```

## Configuration

`portfolio.json` schema (only the `stocks` array is read):

```jsonc
{
  "portfolio": {
    "stocks": [
      { "ticker": "0050",  "name": "元大台灣50", "shares": 160,   "avg_price_twd": 80.36, "total_cost_twd": 12857.60 },
      { "ticker": "2412",  "name": "中華電",     "shares": 1200,  "avg_price_twd": 108.83, "total_cost_twd": 130740.00 },
      { "ticker": "00891", "name": "中信關鍵半導體", "shares": 11000, "avg_price_twd": 16.22, "total_cost_twd": 178538.00 }
    ]
  }
}
```

Fields:

| Field | Type | Description |
|---|---|---|
| `ticker` | string | TWSE/TPEx code (4-digit, ETFs may be 4-6 chars with letters) |
| `name` | string | Display name |
| `shares` | int | Number of shares held; `0` marks a tracking-only entry |
| `avg_price_twd` | number | Average cost per share (informational only — totals use `total_cost_twd`) |
| `total_cost_twd` | number | Total cost basis in TWD (used for P&L) |

### Tracking-only entries

Set `shares: 0` (and `total_cost_twd: 0`) to add a watchlist ticker:

```jsonc
{ "ticker": "2330", "name": "台積電", "shares": 0, "avg_price_twd": 0, "total_cost_twd": 0 }
```

Behavior:

| Mode | Tracking entry shown? |
|---|---|
| Default | No |
| `-r` | Yes (price/change displayed; value/cost/P&L are placeholders, not in totals) |
| `-d` | No (zero-share dividends are not informative) |
| `-k` | N/A (any ticker is queryable) |

## Data Sources

| Source | Used for | Endpoint |
|---|---|---|
| Anue (cnyes) | Real-time quotes (default), stock names | `ws.api.cnyes.com/ws/api/v1/quote/quotes/TWS:<ticker>:STOCK` |
| TWSE MIS | Real-time quotes (`-S twse`) | `mis.twse.com.tw/stock/api/getStockInfo.jsp` |
| CMoney | Real-time quotes (`-S cmoney`), daily K-line, dividend history | `cmoney.tw/finance/ashx/mainpage.ashx`, `MobileService/ashx/GetDtnoData.ashx` |

All endpoints are public (no auth) and used directly via `requests`.

## Output Conventions

- **Colors**: red = up / profit, green = down / loss (Taiwan stock convention, opposite of US)
- **P&L format**: `NT$  108,302 ( +164.4%)`; negative: `NT$   -1,234 (  -5.2%)`
- **Currency**: TWD only — this tool covers TW equities and ETFs

## File Layout

```
.
├── check_stocks.py        # main script + shared library
├── scan_signals.py        # signal scanner for arbitrary tickers
├── portfolio.json         # holdings (used by check_stocks.py)
├── watchlist.txt.sample   # template for scan_signals.py
├── history_stocks.json    # auto-generated daily snapshots (gitignored)
└── README.md              # this file
```

## Architecture

`check_stocks.py` doubles as both a standalone CLI and a shared library —
its `if __name__ == "__main__":` guard lets other scripts `import` its
helpers without triggering `main()`. `scan_signals.py` is a thin wrapper
that reuses `scan_signals` and `print_signals_table` from `check_stocks.py`.

This means every shared concern (CJK-aware formatting, the three quote
sources, K-line + KDJ + MA computation, signal logic, valuation grading)
lives in one file. Adding a new signal rule, swapping a quote source, or
tweaking the table layout is a single-file change.
