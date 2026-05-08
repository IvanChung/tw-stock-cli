#!/usr/bin/env python3
"""Scan arbitrary TW stocks for buy/watch/hot signals.
Reuses signal logic from check_stocks.py — no portfolio needed."""

import argparse
import sys
from pathlib import Path

# Reuse signal logic from check_stocks.py (same dir)
sys.path.insert(0, str(Path(__file__).parent))
from check_stocks import scan_signals, print_signals_table, get_stock_name


def parse_args():
    p = argparse.ArgumentParser(
        description="台股訊號掃描器（J 值 + MA60 + 外資籌碼綜合判斷）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
範例：
  python3 scan_signals.py 2207 2330 2454       直接列出 ticker
  python3 scan_signals.py -f watchlist.txt     從檔案讀取（一行一個 ticker）
  python3 scan_signals.py 2207 -f extra.txt    args + file 合併
""",
    )
    p.add_argument("tickers", nargs="*", help="股票代號（可多個）")
    p.add_argument("-f", "--file", metavar="PATH",
                   help="從檔案讀取 ticker（每行一個，支援 # 註解）")
    p.add_argument("--no-name-lookup", action="store_true",
                   help="不查詢股名（加快速度）")
    return p.parse_args()


def load_tickers_from_file(path: str) -> list[str]:
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if line:
                out.append(line)
    return out


def main():
    args = parse_args()
    tickers = list(args.tickers)
    if args.file:
        tickers += load_tickers_from_file(args.file)
    if not tickers:
        # Try default watchlist.txt in same dir
        default = Path(__file__).parent / "watchlist.txt"
        if default.exists():
            tickers = load_tickers_from_file(str(default))
            print(f"  (從預設 watchlist.txt 讀取 {len(tickers)} 檔)")
        else:
            print("  ⚠ 沒有 ticker。用法: scan_signals.py <ticker>... 或 -f watchlist.txt")
            return

    # dedupe preserving order
    seen = set()
    tickers = [t for t in tickers if not (t in seen or seen.add(t))]

    # Optionally fetch stock names
    names = {}
    if not args.no_name_lookup:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {t: ex.submit(get_stock_name, t) for t in tickers}
        names = {t: f.result() for t, f in futs.items()}

    signals = scan_signals(tickers, names)
    print_signals_table(signals, title=f"訊號掃描 — {len(tickers)} 檔")


if __name__ == "__main__":
    main()
