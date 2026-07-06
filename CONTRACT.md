# 数据接口契约（下载脚本与 index.html 共同遵守）

由三个下载脚本（`fetch_a_baostock.py` / `fetch_hkus_yf.py` / 备用 `fetch_data.py`）产出、由 `index.html` 消费。

战法：A股 / 港股为**低位首板回踩缩量战法**；美股为独立的**平台 / 杯柄突破策略**（用同一套数据文件，但另有 `index` 字段供大盘过滤，见下）。

## 数据文件
每个市场一个文件，与 index.html 放在同一目录：
- `data_A.json`  —— A股
- `data_HK.json` —— 港股
- `data_US.json` —— 美股

## 顶层结构
```json
{
  "market": "A",                       // "A" | "HK" | "US"
  "generated_at": "2026-06-30T20:00:00", // ISO8601
  "count": 5000,
  "stocks": [ StockObj, ... ],
  "index": IndexObj                    // 仅 data_US.json 有（大盘指数 SPY，见下）；A/HK 无此字段
}
```

## StockObj
```json
{
  "code": "000001",        // A:"000001" / HK:"00700" / US:"AAPL"
  "name": "平安银行",
  "board": "main",         // A股: main|gem|star|bse ; HK: "hk" ; US: "us"
  "secid": "0.000001",     // 东方财富 secid（便于刷新，可选）
  "kline": [
    ["2025-01-02", 10.1, 10.5, 10.0, 10.3, 123456],
    ...
  ]
}
```

### board 取值（强约束，index.html 按此映射中文名并识别 A股板块阈值）
- A股：`main`(主板) | `gem`(创业板) | `star`(科创板) | `bse`(北交所)
- 港股：统一 `hk`
- 美股：统一 `us`

（当前下载脚本对港股只产出 `hk`、美股只产出 `us`、A股按代码段判定 main/gem/star/bse。）

## IndexObj（仅 data_US.json）
美股策略需要一个大盘基准做「大盘是否走弱」过滤，`fetch_hkus_yf.py` 下载美股时会额外抓取 **SPY** 日线写入顶层 `index` 字段：
```json
{
  "code": "SPY",
  "kline": [
    ["2024-07-01", 545.63, 545.88, 542.52, 545.34, 40297800],
    ...
  ]
}
```
- `kline` 顺序、精度、升序要求与 StockObj 完全一致（见下节）。
- `index.html` 用它判定「突破当日 SPY 收盘 ≥ 其 N 日均线 → 大盘没有走弱」；**缺此字段时该过滤条件不拦截**（等同放行）。A股 / 港股数据文件没有 `index` 字段。

## kline 数组元素顺序（强约束，双方必须一致）
`[date, open, high, low, close, volume]`
- date: "YYYY-MM-DD"
- open/high/low/close: float（A股为**前复权**价）
- volume: 整数（原始成交量，单位随数据源，不需换算）
- 按日期**升序**排列；建议每只股票保留最近 ~300 个交易日

## 触发信号（"涨停 / 单日大涨"）默认阈值
- A股 main → 9.8% ；gem/star → 19.6% ；bse(北交所) → 29.5%
- 名称含 "ST"/"*ST" → 4.8%
- HK → 9%（可调）
- US → 9%（可调）

判定：`prevClose = kline[i-1].close`；`changePct = (close-prevClose)/prevClose*100`；
`changePct >= 阈值` 即为触发日（D0）。

## 东方财富接口要点
- 股票列表 clist：`https://push2.eastmoney.com/api/qt/clist/get`
  - A股 `fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23`
  - 港股 `fs=m:116+t:1,m:116+t:2,m:116+t:3,m:116+t:4`
  - 美股 `fs=m:105,m:106,m:107`
  - fields=f12(code),f14(name)
- 日线 kline：`https://push2his.eastmoney.com/api/qt/stock/kline/get`
  - `klt=101`(日)、`fqt=1`(前复权)、`fields2=f51,f52,f53,f54,f55,f56,f57`
  - 返回每日串顺序为 `date,open,close,high,low,volume,amount`（注意 close 在 high/low 之前，转换时要重排成本契约的 OHLCV 顺序）
  - secid：A股 SH=`1.xxxxxx` SZ=`0.xxxxxx`；港股=`116.xxxxx`；美股=`105./106./107.xxxx`
