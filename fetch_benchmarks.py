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

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("[错误] 需要 yfinance 和 pandas：pip install yfinance pandas", file=sys.stderr)
    sys.exit(1)

OUT = "benchmarks.json"
PERIOD = "2y"

# (对外代码, Yahoo代码, 名称, 类别, 归属市场)
# 全部经过实测可抓。恒生科技指数 Yahoo 没有(^HSTECH 返回 404)，
# 用跟踪它的南方东英 ETF 3033.HK 代理，走势基本一致。
BENCHMARKS = [
    ("HSI",    "^HSI",     "恒生指数",          "index",  "HK"),
    ("HSCE",   "^HSCE",    "恒生中国企业指数",    "index",  "HK"),
    ("HSTECH", "3033.HK",  "恒生科技ETF(指数代理)", "etf",  "HK"),
    ("SPX",    "^GSPC",    "标普500",           "index",  "US"),
    ("DJI",    "^DJI",     "道琼斯工业平均",      "index",  "US"),
    ("NDX",    "^NDX",     "纳斯达克100",        "index",  "US"),
    ("IXIC",   "^IXIC",    "纳斯达克综合",        "index",  "US"),
    ("VIX",    "^VIX",     "波动率指数",         "index",  "US"),
    ("QQQ",    "QQQ",      "纳指100 ETF",       "etf",    "US"),
    ("SPY",    "SPY",      "标普500 ETF",       "etf",    "US"),
    ("DXY",    "DX-Y.NYB", "美元指数",           "index",  "FX"),
    ("US10Y",  "^TNX",     "美国10年期国债收益率", "rate",   "US"),
    ("ES",     "ES=F",     "标普500期货",        "future", "US"),
    ("NQ",     "NQ=F",     "纳斯达克100期货",     "future", "US"),
]


def load_existing():
    """读已有 benchmarks.json，返回 {code: kline}。"""
    if not os.path.exists(OUT):
        return {}
    try:
        with open(OUT, encoding="utf-8") as f:
            return {it["code"]: it.get("kline", []) for it in json.load(f).get("items", [])}
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
    for code, ticker, name, cat, market in BENCHMARKS:
        old = existing.get(code, [])
        # 增量时只补最后一条之后的部分，留 5 天重叠以覆盖数据修正
        start = None
        if old:
            last = datetime.date.fromisoformat(old[-1][0])
            start = (last - datetime.timedelta(days=5)).isoformat()

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
