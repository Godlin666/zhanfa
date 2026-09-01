#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取市场基准标的（指数 / ETF / 期货 / 利率），产出 benchmarks.json。

跟股票数据完全分开的原因：
  fetch_hkus_yf.py 里 us_symbols(include_etf=False) 是有意排除 ETF 的——
  战法针对个股涨停催化剂，一篮子基金混进去会产生噪音信号。
  基准数据是给外部使用者做参照系用的，不进 data_*.json，不影响战法扫描。

逐个下载而非批量：标的只有十几个，逐个下载能拿到普通列索引，
避开 yfinance 批量模式返回 MultiIndex 的坑，且单个失败不拖累其他。
"""
import datetime
import json
import os
import sys
import time
import urllib.request

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("[错误] 需要 yfinance 和 pandas：pip install yfinance pandas", file=sys.stderr)
    sys.exit(1)

OUT = "benchmarks.json"
PERIOD = "2y"

# (对外代码, 数据源, 源代码, 名称, 类别, 归属市场)
#
# 绝大多数走 Yahoo(稳定、无限流)。只有 Yahoo 拿不到的两个走东财：
#   · 恒生科技指数：Yahoo 的 ^HSTECH 返回 404。曾用 3033.HK ETF 代理，
#     但 ETF 是 4.5 港元、指数是 4500 点,量纲完全不同,外部用起来会画错图。
#     东财 124.HSTECH 是真指数,且日线能取到 2014 年。
#   · 恒生科技期货：新浪穷举十余种写法全空,东财有主力合约 134.HTI_M。
#
# 东财 secid 可用官方搜索接口自查(见 OPENDATA.md)：
#   searchapi.eastmoney.com/api/suggest/get?input=恒生科技&type=14
BENCHMARKS = [
    ("HSI",    "yahoo", "^HSI",        "恒生指数",           "index",  "HK"),
    ("HSCE",   "yahoo", "^HSCE",       "恒生中国企业指数",     "index",  "HK"),
    # HSTECH 保持 Yahoo 的 ETF 代理不动——外部已经在引用这个 code，
    # 换源会让他们的 b[b.code=="HSTECH"] 拿到不同量纲甚至取空。
    # 真指数和期货作为新增标的提供，是加法不是替换。
    ("HSTECH",    "yahoo", "3033.HK",    "恒生科技ETF(指数代理)", "etf",    "HK"),
    # 下面两个只有东财有。东财限流很凶，抓不到时主流程跳过，
    # 由 patch_em_benchmarks.py 后续补齐，不影响其余标的。
    ("HSTECHIDX", "em",    "124.HSTECH", "恒生科技指数",         "index",  "HK"),
    ("HTI",       "em",    "134.HTI_M",  "恒生科技指数期货主力",   "future", "HK"),
    ("SPX",    "yahoo", "^GSPC",       "标普500",            "index",  "US"),
    ("DJI",    "yahoo", "^DJI",        "道琼斯工业平均",       "index",  "US"),
    ("NDX",    "yahoo", "^NDX",        "纳斯达克100",         "index",  "US"),
    ("IXIC",   "yahoo", "^IXIC",       "纳斯达克综合",         "index",  "US"),
    ("VIX",    "yahoo", "^VIX",        "波动率指数",          "index",  "US"),
    ("QQQ",    "yahoo", "QQQ",         "纳指100 ETF",        "etf",    "US"),
    ("SPY",    "yahoo", "SPY",         "标普500 ETF",        "etf",    "US"),
    ("DXY",    "yahoo", "DX-Y.NYB",    "美元指数",            "index",  "FX"),
    ("US10Y",  "yahoo", "^TNX",        "美国10年期国债收益率",  "rate",   "US"),
    ("ES",     "yahoo", "ES=F",        "标普500期货",         "future", "US"),
    ("NQ",     "yahoo", "NQ=F",        "纳斯达克100期货",      "future", "US"),
]



def load_existing():
    """读已有 benchmarks.json，返回 {code: (源代码, kline)}。

    连源代码一起返回，是为了检测标的换源。例如 HSTECH 曾用 3033.HK(ETF,约4.5港元)
    代理，后改为东财 124.HSTECH(真指数,约4500点)——两者量纲差 1000 倍，
    若按日期直接合并，同一条序列里会混进两种量纲，图表和指标全废。
    """
    if not os.path.exists(OUT):
        return {}
    try:
        with open(OUT, encoding="utf-8") as f:
            return {it["code"]: (it.get("yahoo"), it.get("kline", []))
                    for it in json.load(f).get("items", [])}
    except Exception as e:
        print(f"[警告] 读取现有 {OUT} 失败({e})，本次全量拉取")
        return {}


def to_rows(df):
    """DataFrame -> [[日期, 开, 高, 低, 收, 量], ...]，丢弃任一价格为空的行。"""
    rows = []
    for ts, r in df.iterrows():
        vals = [r.get(c) for c in ("Open", "High", "Low", "Close")]
        if any(v is None or pd.isna(v) for v in vals):
            continue
        vol = r.get("Volume")
        vol = 0 if vol is None or pd.isna(vol) else int(vol)
        rows.append([ts.date().isoformat()] + [round(float(v), 4) for v in vals] + [vol])
    return rows


EM_HOSTS = ["push2his.eastmoney.com", "61.push2his.eastmoney.com",
            "63.push2his.eastmoney.com"]
EM_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120"


def fetch_em(secid, limit=800):
    """
    东财日线 -> [[日期, 开, 高, 低, 收, 量], ...]

    两个必须注意的点：
      · 返回字段顺序是 date,open,close,high,low,volume,amount —— 开收高低,
        不是 OHLC。按 OHLC 解析会把收盘价当最高价。
      · push2his 对请求频率限制很严,触发后直接断连(不返回错误码),
        且冷却可达十几分钟。这里做节点轮换 + 指数退避,调用方还要控制总频率。
    """
    last_err = None
    for attempt, host in enumerate(EM_HOSTS * 2):
        url = (f"https://{host}/api/qt/stock/kline/get?secid={secid}"
               f"&klt=101&fqt=0&beg=0&end=20500101&lmt={limit}"
               "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": EM_UA})
            payload = json.loads(urllib.request.urlopen(req, timeout=20).read())
            klines = (payload.get("data") or {}).get("klines") or []
            rows = []
            for line in klines:
                parts = line.split(",")
                if len(parts) < 6:
                    continue
                date, o, c, h, low = parts[0], *parts[1:5]
                rows.append([date, round(float(o), 4), round(float(h), 4),
                             round(float(low), 4), round(float(c), 4),
                             int(float(parts[5]))])
            if rows:
                return rows
        except Exception as e:
            last_err = e
            time.sleep(2 ** min(attempt, 4))
    if last_err:
        print(f"    (东财 {secid} 全节点失败: {type(last_err).__name__})")
    return []


def merge(old, new):
    """按日期合并，新数据覆盖同日旧数据。"""
    m = {r[0]: r for r in old}
    m.update({r[0]: r for r in new})
    return [m[k] for k in sorted(m)]


def main():
    incremental = "--incremental" in sys.argv
    existing = load_existing() if incremental else {}
    if incremental:
        print(f"增量模式：已有 {len(existing)} 个标的")

    items, failed = [], []
    for code, source, ticker, name, cat, market in BENCHMARKS:
        old_ticker, old = existing.get(code, (None, []))
        # 换源就丢弃旧数据全量重抓，避免两种量纲混进同一条序列
        if old and old_ticker != ticker:
            print(f"  · {code} 数据源变更 {old_ticker} → {ticker}，弃用旧数据重抓")
            old = []
        # 增量时只补最后一条之后的部分，留 5 天重叠以覆盖数据修正
        start = None
        if old:
            last = datetime.date.fromisoformat(old[-1][0])
            start = (last - datetime.timedelta(days=5)).isoformat()

        if source == "em":
            # 东财不支持按起始日期增量，每次取回一段再与本地合并；
            # 只有 2 个标的走这条路，频率远低于限流阈值
            new = fetch_em(ticker)
            time.sleep(2)      # 东财限流严，两次请求之间拉开
            kline = merge(old, new) if old else new
            if not kline:
                failed.append(code)
                print(f"  ✗ {code:<7}({ticker}) 东财无数据，跳过")
                continue
            items.append({"code": code, "yahoo": ticker, "name": name,
                          "category": cat, "market": market, "kline": kline})
            print(f"  ✓ {code:<7}({ticker:<11}) 东财 {len(kline)}条  "
                  f"最新 {kline[-1][0]} 收 {kline[-1][4]}")
            continue

        try:
            if start:
                df = yf.download(ticker, start=start, interval="1d",
                                 progress=False, auto_adjust=False, threads=False)
            else:
                df = yf.download(ticker, period=PERIOD, interval="1d",
                                 progress=False, auto_adjust=False, threads=False)
            # 单标的下载偶尔仍返回 MultiIndex，压平以统一处理
            if df is not None and isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            new = to_rows(df) if df is not None and not df.empty else []
        except Exception as e:
            print(f"  ✗ {code:<7}({ticker}) {type(e).__name__}: {str(e)[:60]}")
            new = []

        kline = merge(old, new) if old else new
        if not kline:
            failed.append(code)
            print(f"  ✗ {code:<7}({ticker}) 无数据，跳过")
            continue

        items.append({"code": code, "yahoo": ticker, "name": name,
                      "category": cat, "market": market, "kline": kline})
        tag = f"+{len(new)}" if old else f"{len(kline)}条"
        print(f"  ✓ {code:<7}({ticker:<9}) {tag:<8} 最新 {kline[-1][0]} 收 {kline[-1][4]}")
        time.sleep(0.3)   # 轻微节流，避免触发 Yahoo 限流

    if not items:
        print("[错误] 全部标的都没抓到", file=sys.stderr)
        return 1

    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "items": items,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    total = sum(len(i["kline"]) for i in items)
    print(f"\n完成：{len(items)} 个标的 / {total} 行 -> {OUT}"
          + (f"（失败 {len(failed)}: {', '.join(failed)}）" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
