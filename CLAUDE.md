# tw-stock-cli

台股投資組合 CLI：即時報價、損益、K 線、訊號掃描、估值評分。

## 執行方式

```bash
python3 check_stocks.py                    # 預設：前一日收盤價，完整損益表
python3 check_stocks.py -r                 # 即時報價（預設 鉅亨 cnyes）；含漲跌上色
python3 check_stocks.py -r -S twse         # 同上，改用 TWSE MIS 為資料源
python3 check_stocks.py -r -S cmoney       # 同上，改用 CMoney 為資料源
python3 check_stocks.py -d                 # 當年度股利：已除息 + 即將除息
python3 check_stocks.py -k 00981A          # K 線 + MA10/20/30/60/120 + J 指標面板
python3 check_stocks.py -k 2412 -n 90      # 中華電 90 日 K 線
python3 check_stocks.py --backfill         # 回填過去 30 交易日股票總計
python3 check_stocks.py --backfill 60      # 回填 60 交易日
python3 check_stocks.py --backfill 2026-04-15  # 回填指定單日
python3 check_stocks.py --signals          # 持股+追蹤股訊號掃描（J/MA60/外資籌碼/估值）

python3 scan_signals.py 2207 2330 2454     # 任意 ticker 訊號掃描
python3 scan_signals.py -f watchlist.txt   # 從檔案讀取（預設讀 watchlist.txt）

python3 check_stocks.py -s pnl-pct         # 各區段依損益% 由高到低排序
python3 check_stocks.py -s pnl-value       # 各區段依損益金額（台幣）由高到低排序
python3 check_stocks.py -s pnl-pct -r      # 即時報價 + 依損益% 排序
python3 check_stocks.py -s change -r       # 即時報價 + 依漲跌金額排序
python3 check_stocks.py -s change-pct -r   # 即時報價 + 依漲跌幅排序
```

## 顯示格式

- 損益欄：正值紅色（獲利）、負值綠色（虧損），台灣股市慣例
- `-r` 模式：現價、漲跌、漲跌幅同樣套用上色
- 損益格式：`NT$ 108,302 (+164.4%)`，負值：`NT$  -1,234 (  -5.2%)`
- `-r` 模式新增技術欄：
  - `J` 值（KDJ）：紅 >80 超買、綠 <20 超賣、無色中性、`─` 資料不足
  - `MA30`：紅 ↑ 站上 30 日線、綠 ↓ 跌破 30 日線、`─` 資料不足
  - `訊號`：BUY（紅）/ WATCH（青）/ HOT（綠警示）/ `─` 中性

## 訊號邏輯（`--signals` / `scan_signals.py`）

| 訊號 | 條件 |
|---|---|
| **+BUY**（STRONG_BUY） | BUY + MA200 5日斜率 ≥ 0 + 收盤 ≥ MA150（中長期多頭結構，最強）|
| **BUY** | J 從 <20 反彈（近 5 日有 J<20 且今日 J≥20 上揚）+ 收盤位於 30 日底部 1/3 + 外資 5日 ≥ -2億 + 量縮（5/30日比 ≤ 80%）|
| **WATCH** | J<20 但 BUY 條件未湊齊（J 未反彈 / 非底部 / 外資賣超 / 量未縮）|
| **HOT** | J>80 + 收盤>MA10（短線過熱）|
| `─` | 中性，無強訊號 |

⚠ MA200 下降警示會附在 BUY 後（提示中長期結構偏弱）。

## 估值評分（`--signals` 模式 `估值` 欄）

公式：`PE 桶 (0-4) + ROE 桶 (0-4)` → A=7-8, B=5-6, C=3-4, D=0-2；ROE 由 `PB / PE × 100%` 推導。

| 桶 | PE 範圍 | 分數 | ROE 範圍 | 分數 |
|---|---|---|---|---|
| 4 | ≤12（很便宜）| 4 | >25%（極優）| 4 |
| 3 | ≤18（便宜）| 3 | >15%（優秀）| 3 |
| 2 | ≤25（合理）| 2 | >10%（良好）| 2 |
| 1 | ≤40（偏貴）| 1 | >5%（普通）| 1 |
| 0 | >40（貴）| 0 | ≤5%（差）| 0 |

顯示：A 紅粗體、B 紅、C 青、D 綠、`F 虧損` 綠（PE≤0）、`─` 為 ETF/無資料。

資料源：CMoney `DtNo=71872`，單次批次取得全市場 1500+ 個股 PE/PB（不含 ETF）。

## 架構

`check_stocks.py` 同時是 **lib + standalone CLI**，所有共用邏輯（顯示格式、即時報價、K 線、訊號、估值）的單一來源：

```
       check_stocks.py              scan_signals.py
       (主程式 + lib)                (任意 ticker 訊號掃描)
              ▲                            │
              └──── from check_stocks ─────┘
                    import (...)
```

- `check_stocks.py` 用 `if __name__ == "__main__":` 守住 `main()`，被 import 時不會執行
- `scan_signals.py` 是薄殼，僅讀 ticker 清單後呼叫 `scan_signals` + `print_signals_table`
- 改訊號條件、估值公式、即時報價來源、CJK 對齊等共用邏輯只動 `check_stocks.py` 一處

## 檔案說明

| 檔案 | 說明 |
|------|------|
| `portfolio.json` | 持倉資料主檔，含成本、股數（複製自 `portfolio.json.sample`） |
| `check_stocks.py` | **共用 lib + 台股 CLI**：顯示格式/即時報價/K 線/訊號/估值的單一實作；獨立執行支援 `-r/-d/-k/--backfill/--signals` 與排序 |
| `scan_signals.py` | 任意 ticker 訊號掃描（不依賴 portfolio.json）；用 `-f watchlist.txt` 或直接傳 ticker；同樣 import 自 check_stocks |
| `portfolio.json.sample` | 持倉資料範本 |
| `watchlist.txt.sample` | scan_signals.py 的清單範本 |
| `history_stocks.json` | `check_stocks.py` 自動記錄當日股票總計（gitignore）|
| `watchlist.txt` | 個人訊號掃描清單（gitignore）|

## 資料來源

| 類別 | 來源 |
|------|------|
| 台股價格（預設）| 鉅亨 cnyes：`ws.api.cnyes.com/ws/api/v1/quote/quotes/TWS:<ticker>:STOCK,...?column=FORMAT`；欄位代碼 `200010`=ticker, `200026`=現價, `200031`=昨收, `200027`=漲跌, `200044`=漲跌幅；單次批次查詢 |
| 台股即時（`-r -S twse`）| TWSE MIS：`mis.twse.com.tw/stock/api/getStockInfo.jsp`，`ex_ch` 用 `tse_<ticker>.tw\|otc_<ticker>.tw` 串接，單次請求批次查詢 |
| 台股即時（`-r -S cmoney`）| CMoney：`www.cmoney.tw/finance/ashx/mainpage.ashx?action=GetStockListLatestSaleData&stockId=<ticker>&cmkey=<key>`，需 `Referer: /finance/<id>/f00025`；`cmkey` 跨股票共用，每次執行先抓一次頁面解析；`commSaleData.SalePr`=現價, `Cf`=漲跌；單檔一個請求，併發限制 4 |
| 日 K 線 + 外資籌碼 | CMoney `DtNo=5389`：日 OHLC + 外資買賣張數 |
| 股利（除權息）| CMoney `DtNo=59444834`：當年度除息日與每股股利 |
| PE / PB（估值）| CMoney `DtNo=71872`：全市場 1500+ 個股單次批次（不含 ETF）|

所有 API 皆為公開端點（無需登入），直接以 `requests` 呼叫。

## portfolio.json 結構

```jsonc
{
  "portfolio": {
    "stocks": [
      { "ticker": "0050", "name": "元大台灣50", "shares": 100, "avg_price_twd": 90.00, "total_cost_twd": 9000.00 },
      { "ticker": "2330", "name": "台積電",     "shares": 100, "avg_price_twd": 1500.00, "total_cost_twd": 150000.00 },
      { "ticker": "2412", "name": "中華電",     "shares": 0,   "avg_price_twd": 0,        "total_cost_twd": 0 }
    ]
  }
}
```

- `shares`：持有股數（整數）；`0` 視為「追蹤標的」
- `avg_price_twd`：均價（僅參考，總計用 `total_cost_twd`）
- `total_cost_twd`：成本總額（含手續費，券商對帳單為準）

### 追蹤標的（非持股）

在 `stocks` 內加入 `"shares": 0`（`total_cost_twd` 也設 0）即視為「追蹤標的」：

- `-r` 模式：顯示現價/漲跌/漲跌幅，「現值/成本/損益」用 `─` 與「追蹤標的」標籤佔位，不計入小計
- 預設模式（完整損益表）：完全略過
- `-d` 模式：完全略過（除息金額為 0 沒有資訊量）
- `--signals` 模式：照常掃訊號（追蹤標的就是觀察用）

## 注意事項

- 台股成本以**券商對帳單含手續費**為準，非均價 × 股數
- `--backfill` 用 CMoney 日 K 自動偵測拆股（前後比率 >2 或 <0.5），自動還原舊收盤
- `history_stocks.json` 是當日快照，每次執行覆蓋當日紀錄（同日多次執行不會重複）
- 訊號／估值邏輯為個人交易框架的紀錄，**非投資建議**；自行判斷風險
