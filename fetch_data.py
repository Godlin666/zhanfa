#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_data.py —— A股 / 港股 / 美股 全市场日线行情下载脚本
=========================================================

【用途】
从东方财富免费接口下载全市场股票的日线行情（A股取前复权），按照 CONTRACT.md
约定的结构，导出成本地 JSON 文件，供同目录的 index.html 做选股 + 回测使用。

【依赖】
只用 Python3 标准库，无需 pip 安装任何东西。直接：
    python3 fetch_data.py

【常用命令】
    python3 fetch_data.py                          # 下全部三个市场
    python3 fetch_data.py --markets A              # 只下 A股
    python3 fetch_data.py --markets A,HK,US        # 指定多个市场
    python3 fetch_data.py --limit 50               # 每个市场最多下 50 只（调试用）
    python3 fetch_data.py --workers 20             # 并发线程数（默认 10）
    python3 fetch_data.py --us-active-only         # 美股只下成交额最活跃的前 N 只
    python3 fetch_data.py --us-active-limit 1500   # 配合上面，控制美股活跃股数量

【输出文件】（与本脚本同目录）
    data_A.json   data_HK.json   data_US.json

【数据契约】见同目录 CONTRACT.md。kline 元素顺序严格为：
    [date, open, high, low, close, volume]
    date: "YYYY-MM-DD"；OHLC: float；volume: int；按日期升序。
"""

import os
import re
import json
import time
import argparse
import datetime
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----------------------------------------------------------------------------
# 全局配置
# ----------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# clist 有多个镜像节点，主节点限流时自动回退到备用节点
CLIST_HOSTS = ["push2.eastmoney.com", "push2delay.eastmoney.com"]
# K线接口同样有多个镜像节点。push2his 在高频请求下会限频/断连，
# 这里轮换多个节点 + 退避重试来扛节流。
KLINE_HOSTS = [
    "push2his.eastmoney.com",
    "push2his.eastmoney.com",
    "61.push2his.eastmoney.com",
    "63.push2his.eastmoney.com",
]
KLINE_PATH = "/api/qt/stock/kline/get"

# clist 的市场过滤串（fs 参数）
FS = {
    "A":  "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "HK": "m:116+t:1,m:116+t:2,m:116+t:3,m:116+t:4",
    "US": "m:105,m:106,m:107",
}

CLIST_PAGE_SIZE = 100   # clist 单页条数（接口上限约 100，需翻页）
KLINE_LIMIT = 300       # 每只股票保留最近多少个交易日
KLINE_RETRY = 3         # kline 失败重试次数（换节点退避）
HTTP_TIMEOUT = 10       # 单次请求超时（秒）
US_ACTIVE_DEFAULT = 1500  # 美股 --us-active-only 默认取前多少只


# ----------------------------------------------------------------------------
# HTTP 工具
# ----------------------------------------------------------------------------
def http_get_json(url, timeout=HTTP_TIMEOUT, retries=0):
    """发起 GET 请求并解析 JSON。retries>0 时失败自动重试。失败抛异常。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            return json.loads(text)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise last_err


# ----------------------------------------------------------------------------
# board 判定（A股按 code 前缀；HK/US 固定）
# ----------------------------------------------------------------------------
def classify_board_a(code):
    """A股板块判定：main|gem|star|bse"""
    if code.startswith("68"):
        return "star"   # 科创板
    if code.startswith("30"):
        return "gem"    # 创业板
    if code.startswith(("8", "4", "920")):
        return "bse"    # 北交所
    return "main"       # 普通主板（60/00/001/002 等）


def make_board(market, code):
    if market == "A":
        return classify_board_a(code)
    if market == "HK":
        return "hk"
    return "us"


# ----------------------------------------------------------------------------
# secid 构造
# ----------------------------------------------------------------------------
def make_secid(market, code, f13):
    """
    用 clist 返回的市场号 f13 拼 `f13.code` 最稳妥。
    若 f13 缺失，按规则兜底推断。
    """
    if f13 is not None and f13 != "":
        return "%s.%s" % (f13, code)
    # 兜底
    if market == "A":
        # 沪市：60/68/9 开头 -> 1.；其余深市 -> 0.
        if code.startswith(("60", "68", "9")):
            return "1.%s" % code
        return "0.%s" % code
    if market == "HK":
        return "116.%s" % code
    return "105.%s" % code


# ----------------------------------------------------------------------------
# 步骤一：拉取股票列表（带翻页）
# ----------------------------------------------------------------------------
def fetch_stock_list(market, active_only=False, active_limit=US_ACTIVE_DEFAULT,
                     stop_after=None):
    """
    返回 [{code, name, f13}, ...]
    active_only=True 时按成交额(fid=f6)降序取前 active_limit 只（用于美股提速）。
    stop_after=N 时取够 N 只即停止翻页（配合 --limit 调试，避免翻完全部页）。
    """
    fs = FS[market]
    # active_only 用 f6(成交额) 排序，否则用 f3(涨跌幅) 默认排序（顺序不影响最终结果）
    fid = "f6" if active_only else "f3"
    results = []
    seen = set()
    pn = 1
    total = None
    # 选定一个可用的 clist 节点（第一页成功即锁定该 host 翻页）
    host = None
    while True:
        hosts_to_try = [host] if host else CLIST_HOSTS
        data = None
        for h in hosts_to_try:
            url = (
                "https://%s/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1&fltt=2&invt=2"
                "&fid=%s&fs=%s&fields=f12,f13,f14"
                % (h, pn, CLIST_PAGE_SIZE, fid, fs)
            )
            try:
                data = http_get_json(url, retries=3)
                host = h  # 锁定可用节点
                break
            except Exception as e:
                print("  [列表] %s 第 %d 页失败: %s" % (h, pn, e))
                data = None
        if data is None:
            # 所有节点都失败：用已取到的部分
            break

        d = data.get("data")
        if not d:
            break
        if total is None:
            total = d.get("total", 0)
        diff = d.get("diff")
        if not diff:
            break
        # diff 可能是 list 或 dict（按页码键），统一成 list
        if isinstance(diff, dict):
            diff = list(diff.values())

        for item in diff:
            code = item.get("f12")
            name = item.get("f14")
            f13 = item.get("f13")
            if not code or code in seen:
                continue
            seen.add(code)
            results.append({"code": str(code), "name": str(name), "f13": f13})

        # active_only：取够数量就停
        if active_only and len(results) >= active_limit:
            results = results[:active_limit]
            break
        # 调试 --limit：取够即停，避免翻完全部页
        if stop_after and len(results) >= stop_after:
            results = results[:stop_after]
            break

        # 翻页终止条件
        if total and len(results) >= total:
            break
        if len(diff) < CLIST_PAGE_SIZE:
            break
        pn += 1
        time.sleep(0.05)  # 轻微限速，避免被拒

    return results


# ----------------------------------------------------------------------------
# 步骤二：拉取单只股票日线
# ----------------------------------------------------------------------------
def parse_kline_str(s):
    """
    东财每条 kline 串：date,open,close,high,low,volume,amount
    重排成契约要求的 [date, open, high, low, close, volume]
    """
    parts = s.split(",")
    if len(parts) < 6:
        return None
    date = parts[0]
    try:
        o = float(parts[1])
        c = float(parts[2])
        h = float(parts[3])
        low = float(parts[4])
        vol = int(float(parts[5]))
    except (ValueError, IndexError):
        return None
    return [date, o, h, low, c, vol]


def fetch_kline(market, code, secid):
    """
    拉取并解析一只股票的日线，返回 kline 列表（升序），失败返回 None。
    多节点轮换 + 退避重试，专门对付 push2his 的限频/断连。
    注意：data.klines 为空（真没数据）直接返回 None，不重试；
          只有网络异常/断连才重试换节点。
    """
    fqt = 1 if market == "A" else 0  # A股前复权；HK/US 不复权
    qs = ("?secid=%s&klt=101&fqt=%d&beg=0&end=20500101&lmt=%d"
          "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
          % (secid, fqt, KLINE_LIMIT))
    last_err = None
    for attempt in range(KLINE_RETRY + 1):
        host = KLINE_HOSTS[attempt % len(KLINE_HOSTS)]
        url = "https://%s%s%s" % (host, KLINE_PATH, qs)
        try:
            data = http_get_json(url)
            d = data.get("data")
            if not d:
                return None
            klines = d.get("klines") or []
            out = []
            for s in klines:
                row = parse_kline_str(s)
                if row:
                    out.append(row)
            # 东财默认升序，保险起见再按日期排序
            out.sort(key=lambda r: r[0])
            return out if out else None
        except Exception as e:
            # 限频/断连：退避后换下一个节点重试
            last_err = e
            if attempt < KLINE_RETRY:
                time.sleep(0.5 * (attempt + 1) + 0.2)
    return None


# ----------------------------------------------------------------------------
# 单个市场的下载流程
# ----------------------------------------------------------------------------
def fetch_market(market, limit=None, workers=10,
                 us_active_only=False, us_active_limit=US_ACTIVE_DEFAULT):
    label = {"A": "A股", "HK": "港股", "US": "美股"}[market]
    active_only = (market == "US" and us_active_only)
    print("\n==== 开始下载 %s ====" % label)
    print("  获取股票列表中...")
    stocks = fetch_stock_list(market, active_only=active_only,
                              active_limit=us_active_limit,
                              stop_after=limit)
    print("  列表共 %d 只" % len(stocks))

    if limit:
        stocks = stocks[:limit]
        print("  --limit 生效，仅处理前 %d 只" % len(stocks))

    total = len(stocks)
    results = []
    done = 0
    ok = 0
    skipped = 0

    def worker(st):
        secid = make_secid(market, st["code"], st["f13"])
        kline = fetch_kline(market, st["code"], secid)
        return st, secid, kline

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(worker, st): st for st in stocks}
        for fut in as_completed(futures):
            done += 1
            try:
                st, secid, kline = fut.result()
            except Exception as e:
                st = futures[fut]
                print("  [%s] %d/%d %s ✗ (%s)" % (label, done, total, st["name"], e))
                skipped += 1
                continue

            if not kline:
                skipped += 1
                print("  [%s] %d/%d %s ✗ (无K线)" % (label, done, total, st["name"]))
                continue

            results.append({
                "code": st["code"],
                "name": st["name"],
                "board": make_board(market, st["code"]),
                "secid": secid,
                "kline": kline,
            })
            ok += 1
            if done % 50 == 0 or done == total:
                print("  [%s] %d/%d %s ✓ (累计成功 %d)"
                      % (label, done, total, st["name"], ok))

    # 按 code 排序，输出稳定
    results.sort(key=lambda s: s["code"])

    payload = {
        "market": market,
        "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "count": len(results),
        "stocks": results,
    }
    out_path = os.path.join(SCRIPT_DIR, "data_%s.json" % market)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    print("  ---- %s 完成：成功 %d / 跳过 %d / 共 %d ----"
          % (label, ok, skipped, total))
    print("  写入 %s" % out_path)
    return payload


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="从东方财富下载 A股/港股/美股 全市场日线行情，导出本地 JSON。")
    ap.add_argument("--markets", default="A,HK,US",
                    help="要下载的市场，逗号分隔，如 A,HK,US（默认全下）")
    ap.add_argument("--limit", type=int, default=None,
                    help="每个市场最多下 N 只（调试用）")
    ap.add_argument("--workers", type=int, default=6,
                    help="并发线程数（默认 6，过高易被接口限频断连）")
    ap.add_argument("--us-active-only", action="store_true",
                    help="美股只下成交额最活跃的前 N 只，避免上万只下太久")
    ap.add_argument("--us-active-limit", type=int, default=US_ACTIVE_DEFAULT,
                    help="配合 --us-active-only，美股活跃股数量（默认 %d）"
                         % US_ACTIVE_DEFAULT)
    args = ap.parse_args()

    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    for m in markets:
        if m not in FS:
            print("未知市场: %s（应为 A/HK/US 之一）" % m)
            return

    t0 = time.time()
    summary = []
    for m in markets:
        payload = fetch_market(
            m, limit=args.limit, workers=args.workers,
            us_active_only=args.us_active_only,
            us_active_limit=args.us_active_limit,
        )
        summary.append((m, payload["count"]))

    print("\n===== 全部完成（耗时 %.1fs）=====" % (time.time() - t0))
    for m, c in summary:
        print("  %s: %d 只" % (m, c))


if __name__ == "__main__":
    main()
