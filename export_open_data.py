#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 data_{HK,US}.json 里的嵌套行情拍平成通用格式，供外部直接消费。

不碰战法逻辑、不做任何指标计算，只做格式转换：
  data_HK.json / data_US.json  ->  open_data/full/    (全量，上传到私有路径)
                                   open_data/public/  (近90天+元信息，上传到随机公开路径)

设计取舍：
  · 逐市场处理并及时释放，避免两个市场的原始 JSON 同时压在内存里（US 单文件就 150MB）
  · Parquet 分批写入，不一次性构建整张表
  · pyarrow 缺失时只跳过 Parquet，CSV 照常产出（本地无依赖时也能跑）
"""
import csv
import gzip
import io
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_ARROW = True
except ImportError:
    HAS_ARROW = False

OUT = "open_data"
FULL = os.path.join(OUT, "full")
PUBLIC = os.path.join(OUT, "public")
# 只导港美：data_A.json 虽然也在仓库里，但 workflow 的定时任务只更新 HK/US，
# A 股数据是陈的，导出去等于发过期数据
MARKETS = ["HK", "US"]
RECENT_BARS = 90          # 近 N 个交易日，按每只股票自身的序列尾部取
COLUMNS = ["code", "name", "market", "date", "open", "high", "low", "close", "volume"]
PARQUET_BATCH = 200_000   # 每积累这么多行落一次盘

ARROW_SCHEMA = pa.schema([
    ("code", pa.string()), ("name", pa.string()), ("market", pa.string()),
    ("date", pa.date32()), ("open", pa.float64()), ("high", pa.float64()),
    ("low", pa.float64()), ("close", pa.float64()), ("volume", pa.int64()),
]) if HAS_ARROW else None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


class ParquetSink:
    """按批攒行再写盘；没有 pyarrow 时整体退化为空操作。"""

    def __init__(self, path):
        self.path = path
        self.writer = None
        self.buf = [[] for _ in COLUMNS]
        self.n = 0

    def add(self, row):
        if not HAS_ARROW:
            return
        for i, v in enumerate(row):
            self.buf[i].append(v)
        self.n += 1
        if self.n >= PARQUET_BATCH:
            self.flush()

    def flush(self):
        if not HAS_ARROW or self.n == 0:
            return
        arrays = []
        for i, c in enumerate(COLUMNS):
            col = pa.array(self.buf[i])
            # 日期以字符串读入，落盘前转成 date32
            arrays.append(col.cast(pa.date32()) if c == "date"
                          else col.cast(ARROW_SCHEMA.field(c).type))
        batch = pa.Table.from_arrays(arrays, schema=ARROW_SCHEMA)
        if self.writer is None:
            self.writer = pq.ParquetWriter(self.path, ARROW_SCHEMA, compression="zstd")
        self.writer.write_table(batch)
        self.buf = [[] for _ in COLUMNS]
        self.n = 0

    def close(self):
        self.flush()
        if self.writer is not None:
            self.writer.close()


def export_market(market, info, symbols_rows, meta_markets):
    src = f"data_{market}.json"
    if not os.path.exists(src):
        print(f"[跳过] {src} 不存在")
        return

    print(f"[{market}] 读取 {src} ...", flush=True)
    with open(src, encoding="utf-8") as f:
        payload = json.load(f)
    stocks = payload.get("stocks", [])

    lo = market.lower()
    full_csv = os.path.join(FULL, f"{lo}_daily.csv.gz")
    recent_name = f"{lo}_recent{RECENT_BARS}.csv"
    recent_zip = os.path.join(PUBLIC, f"{lo}_recent{RECENT_BARS}.zip")
    parquet = os.path.join(FULL, f"{lo}_daily.parquet")

    sink = ParquetSink(parquet)
    rows = recent_rows = 0
    dmin = dmax = None

    zf = zipfile.ZipFile(recent_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
    with gzip.open(full_csv, "wt", newline="", encoding="utf-8", compresslevel=6) as fz, \
         zf.open(recent_name, "w") as raw, \
         io.TextIOWrapper(raw, encoding="utf-8", newline="") as fr:
        wz, wr = csv.writer(fz), csv.writer(fr)
        wz.writerow(COLUMNS)
        wr.writerow(COLUMNS)

        for s in stocks:
            code, name = s.get("code", ""), s.get("name", "")
            kline = s.get("kline") or []
            if not kline:
                continue

            tail_from = len(kline) - RECENT_BARS
            for idx, k in enumerate(kline):
                # kline 定长 6 项：[日期, 开, 高, 低, 收, 量]
                date, o, h, lo_, c, v = k
                row = [code, name, market, date, o, h, lo_, c, int(v)]
                wz.writerow(row)
                sink.add(row)
                if idx >= tail_from:
                    wr.writerow(row)
                    recent_rows += 1
                rows += 1

            first, last = kline[0][0], kline[-1][0]
            dmin = first if dmin is None or first < dmin else dmin
            dmax = last if dmax is None or last > dmax else dmax

            meta_info = info.get(code, {})
            symbols_rows.append([
                code, name, market, s.get("secid", ""),
                meta_info.get("sector", ""), meta_info.get("industry", ""),
                first, last, len(kline),
            ])

    sink.close()
    zf.close()
    if not HAS_ARROW and os.path.exists(parquet):
        os.remove(parquet)

    meta_markets[market] = {
        "symbols": len(stocks),
        "rows": rows,
        "recent_rows": recent_rows,
        "date_range": [dmin, dmax],
        "source_generated_at": payload.get("generated_at"),
    }
    print(f"[{market}] {len(stocks)} 只 / {rows} 行 / {dmin}~{dmax}", flush=True)
    del payload, stocks


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(FULL, exist_ok=True)
    os.makedirs(PUBLIC, exist_ok=True)

    if not HAS_ARROW:
        print("[提示] 未安装 pyarrow，本次只产出 CSV，跳过 Parquet")

    info = {}
    if os.path.exists("info_cache.json"):
        with open("info_cache.json", encoding="utf-8") as f:
            info = json.load(f)

    symbols_rows, meta_markets = [], {}
    for m in MARKETS:
        export_market(m, info, symbols_rows, meta_markets)

    if not meta_markets:
        print("[错误] 没有任何市场数据可导出", file=sys.stderr)
        return 1

    # symbols 两边各放一份：全量用户查对照，公开区的人也常需要代码表
    sym_header = ["code", "name", "market", "yahoo_symbol",
                  "sector", "industry", "first_date", "last_date", "bars"]
    sym_path = os.path.join(FULL, "symbols.csv")
    with open(sym_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(sym_header)
        w.writerows(symbols_rows)
    shutil.copy(sym_path, os.path.join(PUBLIC, "symbols.csv"))

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance (via yfinance)",
        "price_adjustment": "none",
        "price_adjustment_note": "原始未复权价，除权除息当日会出现价格跳空；计算收益率前请自行复权",
        "timezone_note": "date 为各市场当地交易日，非 UTC",
        "columns": COLUMNS,
        "recent_bars": RECENT_BARS,
        "markets": meta_markets,
        "files": [],
    }
    for d in (FULL, PUBLIC):
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            meta["files"].append({
                "path": f"{os.path.basename(d)}/{fn}",
                "bytes": os.path.getsize(p),
                "sha256": sha256_of(p),
            })

    for d in (FULL, PUBLIC):
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    print("\n=== 产出 ===")
    for d in (FULL, PUBLIC):
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            print(f"  {os.path.basename(d)}/{fn:<28} {os.path.getsize(p)/1024/1024:7.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
