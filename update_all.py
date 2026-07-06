#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_all.py —— 一键更新全部市场数据（A股 + 港股 + 美股）
============================================================

【用途】
每天收盘后，一条命令把 A股 / 港股 / 美股 的日线数据全部刷新一遍。
本脚本不重写任何下载逻辑，只是用 subprocess 依次调用目录内现成的两个脚本：
  - A股      -> fetch_a_baostock.py（baostock，输出 data_A.json）
  - 港股/美股 -> fetch_hkus_yf.py（yfinance，输出 data_HK.json / data_US.json）
每个市场跑完后读取对应 data_*.json 打印汇总，最后给出一张总汇总表。
某个市场失败（网络/限频等）不会中断其它市场，会在汇总表里标注「失败」。

【依赖】（由两个子脚本各自要求，需提前装好）
    pip3 install baostock yfinance --break-system-packages

【常用命令】
    python3 update_all.py                          # 首次：全量下载 A股+港股+美股
    python3 update_all.py --incremental             # 日常：增量更新（收盘后每天跑这个）
    python3 update_all.py --markets A,HK           # 只更新 A股 和 港股
    python3 update_all.py --markets US             # 只更新 美股
    python3 update_all.py --limit 30               # 调试：每市场只下前 30 只
    python3 update_all.py --period 1y              # 港股/美股回溯时长透传（默认沿用子脚本默认）

【首次全量 vs 每天增量】
第一次跑必须全量（本地还没有数据可参照），几千只股票 x 完整历史，比较慢。
之后每天收盘后加 --incremental：已收录的股票只请求"本地最新日期+1 到今天"这一小段
并追加合并，只有新上市的股票才会全量拉取，日常刷新负载大幅降低。

【参数】
    --markets      要更新的市场，逗号分隔，默认 A,HK,US；可只选部分，如 A,HK
    --limit N      每市场只下前 N 只（调试用），同时透传给两个子脚本
    --period       回溯时长（仅对港股/美股首次全量生效，透传给 fetch_hkus_yf.py）；
                   不传则不加该参数，沿用子脚本自身默认值（当前为 2y）
    --incremental  增量更新模式（透传给两个子脚本），每天收盘后用这个
"""

import os
import sys
import json
import argparse
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

A_SCRIPT = os.path.join(SCRIPT_DIR, "fetch_a_baostock.py")
HKUS_SCRIPT = os.path.join(SCRIPT_DIR, "fetch_hkus_yf.py")

VALID_MARKETS = ["A", "HK", "US"]


def run_subprocess(cmd):
    """运行子进程，实时透传输出。返回 True/False 表示是否成功（exit 0）。"""
    print("\n>>> " + " ".join(cmd), flush=True)
    try:
        proc = subprocess.run(cmd, cwd=SCRIPT_DIR)
        return proc.returncode == 0
    except Exception as e:
        print("子进程启动异常: %s" % e)
        return False


def latest_kline_date(stocks):
    """从任一股票的 kline 末行取最新日期；取不到返回 '-'。"""
    for s in stocks:
        kline = s.get("kline")
        if kline:
            return kline[-1][0]
    return "-"


def summarize_market(market):
    """读取 data_<market>.json，返回 (count, size_str, latest_date)；读不到抛异常。"""
    path = os.path.join(SCRIPT_DIR, "data_%s.json" % market)
    size_bytes = os.path.getsize(path)
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    count = payload.get("count", len(payload.get("stocks", [])))
    latest = latest_kline_date(payload.get("stocks", []))
    return count, human_size(size_bytes), latest


def human_size(n):
    """字节数 -> 易读字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def main():
    ap = argparse.ArgumentParser(
        description="一键更新全部市场数据（A股+港股+美股）")
    ap.add_argument("--markets", default="A,HK,US",
                    help="要更新的市场，逗号分隔（默认 A,HK,US，可只选部分如 A,HK）")
    ap.add_argument("--limit", type=int, default=None,
                    help="每市场只下前 N 只（调试用，透传给两个子脚本）")
    ap.add_argument("--period", default=None,
                    help="回溯时长，仅港股/美股生效（透传给 fetch_hkus_yf.py）；"
                         "不传则沿用子脚本默认值")
    ap.add_argument("--incremental", action="store_true",
                    help="增量更新：已收录股票只补最新数据，新股全量拉取（每天收盘后用这个，比首次全量快很多）")
    ap.add_argument("--proxy", default=None,
                    help="代理地址(仅港股/美股生效，透传给 fetch_hkus_yf.py)，如 http://127.0.0.1:7890；"
                         "不填则子脚本自动读环境变量代理。国内抓 Yahoo 数据建议走代理")
    args = ap.parse_args()

    # 解析市场列表，保持 A,HK,US 的固定顺序，剔除未知项
    requested = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    unknown = [m for m in requested if m not in VALID_MARKETS]
    for m in unknown:
        print("跳过未知市场:", m)
    markets = [m for m in VALID_MARKETS if m in requested]
    if not markets:
        print("没有有效市场可更新，退出。")
        sys.exit(1)

    py = sys.executable  # 保证用同一个 python3

    # status: market -> "成功" / "失败"
    status = {}

    # ---- A股 ----
    if "A" in markets:
        cmd = [py, A_SCRIPT]
        if args.limit is not None:
            cmd += ["--limit", str(args.limit)]
        if args.incremental:
            cmd += ["--incremental"]
        try:
            ok = run_subprocess(cmd)
            status["A"] = "成功" if ok else "失败"
        except Exception as e:
            print("A股 更新异常: %s" % e)
            status["A"] = "失败"

    # ---- 港股 / 美股（同一脚本一次跑完）----
    foreign = [m for m in ("HK", "US") if m in markets]
    if foreign:
        cmd = [py, HKUS_SCRIPT, "--markets", ",".join(foreign)]
        if args.limit is not None:
            cmd += ["--limit", str(args.limit)]
        if args.period is not None:
            cmd += ["--period", args.period]
        if args.incremental:
            cmd += ["--incremental"]
        if args.proxy is not None:
            cmd += ["--proxy", args.proxy]
        try:
            ok = run_subprocess(cmd)
            for m in foreign:
                status[m] = "成功" if ok else "失败"
        except Exception as e:
            print("港股/美股 更新异常: %s" % e)
            for m in foreign:
                status[m] = "失败"

    # ---- 逐市场读取 data_*.json 汇总 ----
    print("\n" + "=" * 60)
    print("总汇总表")
    print("=" * 60)
    header = "%-6s %-6s %-9s %-12s %-12s" % (
        "市场", "状态", "股票数", "文件大小", "最新日期")
    print(header)
    print("-" * 60)

    for m in markets:
        st = status.get(m, "失败")
        if st == "成功":
            try:
                count, size_str, latest = summarize_market(m)
                print("%-6s %-6s %-9s %-12s %-12s" % (
                    m, st, count, size_str, latest))
            except Exception as e:
                # 子脚本 exit 0 但产物读不到，也算失败
                print("%-6s %-6s %-9s %-12s %-12s" % (
                    m, "失败", "-", "-", "读取失败:%s" % e))
        else:
            print("%-6s %-6s %-9s %-12s %-12s" % (m, st, "-", "-", "-"))

    print("=" * 60)

    failed = [m for m in markets if status.get(m) != "成功"]
    if failed:
        print("以下市场更新失败: %s（其它市场已正常更新）" % ", ".join(failed))
    else:
        print("全部市场更新成功。")


if __name__ == "__main__":
    main()
