#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_a_baostock.py —— A股 全市场日线下载（Baostock 版，最稳）
================================================================

【为什么用这个】
东方财富 K线接口在批量拉全市场时会限频/断连。Baostock 是开源、免注册、
专为批量历史行情设计的数据源，拉全市场 A股 更稳。本脚本只下 A股，
输出与 fetch_data.py 完全相同的 data_A.json 契约（供 index.html 用）。
港股/美股 仍用 fetch_data.py（东方财富）下载。

【安装依赖】（一次性）
    pip3 install baostock --break-system-packages
    （macOS 系统 Python 受保护需加 --break-system-packages；用 venv 亦可）

【常用命令】
    python3 fetch_a_baostock.py                 # 下全部 A股（含北交所），约 5000+ 只
    python3 fetch_a_baostock.py --limit 30      # 调试：只下前 30 只
    python3 fetch_a_baostock.py --days 400      # 首次全量回溯多少自然日（默认 450≈300交易日）
    python3 fetch_a_baostock.py --incremental   # 增量更新：已有的股票只补最新几天，新股全量拉取

【增量模式说明（--incremental，每天收盘后建议用这个）】
Baostock/yfinance 这类免费接口都是"一只股票一次请求"，没有免费的"一次性拿全市场"
批量接口，所以首次全量下载（几千只 x 完整历史）无法避免、比较慢。但每天收盘后
没必要把几年历史重新下一遍——增量模式会读取本地已有的 data_A.json，对已收录的
股票只请求"上次数据的下一天 到 今天"这一小段，追加合并、按日期去重排序；只有本地
没有的新股票（比如新上市）才会全量拉取。这样第二天起的日常更新负载显著降低。
为防止 json 文件随时间无限增长，单只股票最多保留最近 MAX_KLINE_KEEP 条记录。

【说明】
- 价格为前复权（adjustflag=2），与战法低位/横盘判断口径一致。
- Baostock 单连接串行拉取，全市场首次全量约需 10~25 分钟（取决于网络），适合收盘后跑一次；
  --incremental 模式下网络请求次数不变（每只股票仍是 1 次 API 调用），
  但传输/解析的数据量小很多，且不会重复拉取已有历史。
- 输出 data_A.json，kline 顺序：[date, open, high, low, close, volume]，升序。
"""

import os
import sys
import json
import time
import argparse
import datetime
import urllib.request

try:
    import baostock as bs
except ImportError:
    print("缺少 baostock，请先安装：pip3 install baostock --break-system-packages")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def classify_board(code6):
    """A股板块：main|gem|star|bse（code6 为不带前缀的6位代码）"""
    if code6.startswith("68"):
        return "star"   # 科创板 20%
    if code6.startswith("30"):
        return "gem"    # 创业板 20%
    if code6.startswith(("8", "4", "920", "43")):
        return "bse"    # 北交所 30%
    return "main"       # 主板 10%


def is_stock(bs_code):
    """只保留个股，剔除指数/ETF/基金。bs_code 形如 sh.600000 / sz.000001 / bj.430047"""
    mkt, code = bs_code.split(".")
    if mkt == "sh":
        return code.startswith(("60", "68"))      # 沪市主板+科创板
    if mkt == "sz":
        return code.startswith(("00", "30"))      # 深市主板+创业板
    if mkt == "bj":
        return True                                # 北交所
    return False


def latest_trade_day():
    """取最近一个有效交易日（往回找最多 10 天）。"""
    today = datetime.date.today()
    for back in range(0, 10):
        d = (today - datetime.timedelta(days=back)).isoformat()
        rs = bs.query_all_stock(day=d)
        if rs.error_code == "0":
            # 看是否真有数据
            if rs.next():
                return d
    return today.isoformat()


MAX_KLINE_KEEP = 500  # 单只股票本地最多保留的交易日数（增量模式下防止文件无限增长）


def load_existing(out_path):
    """读取本地已有 data_A.json -> {code: stock_obj}；不存在或损坏则返回空字典。"""
    if not os.path.exists(out_path):
        return {}
    try:
        with open(out_path, encoding="utf-8") as f:
            payload = json.load(f)
        return {s["code"]: s for s in payload.get("stocks", [])}
    except (json.JSONDecodeError, KeyError, OSError):
        print("  本地 data_A.json 读取失败，按无历史处理（将全量拉取）")
        return {}


def merge_kline(old_kline, new_kline):
    """按日期去重合并两段 kline（new 覆盖同日期的 old），排序后裁剪到 MAX_KLINE_KEEP 条。"""
    by_date = {row[0]: row for row in old_kline}
    for row in new_kline:
        by_date[row[0]] = row
    merged = sorted(by_date.values(), key=lambda r: r[0])
    return merged[-MAX_KLINE_KEEP:]


def fetch_one(bs_code, start, end, retries=2):
    """拉取单只股票 [start,end] 区间的 kline，返回 list（可能为空）。

    区分两种"空"：baostock 返回 error_code=="0" 但无行 = 该区间确实无数据
    （新股/停牌/非交易日），属正常空，直接返回 []；error_code!=0 或对象为空
    = 限频/断连等瞬时故障，退避后重试，避免全量拉取时把可下的票误判为无数据丢弃。
    """
    for attempt in range(retries + 1):
        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume",
            start_date=start, end_date=end, frequency="d", adjustflag="2")
        if rs is None or rs.error_code != "0":
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
            continue
        kline = []
        while rs.next():
            r = rs.get_row_data()    # [date, open, high, low, close, volume]
            try:
                o, h, low, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                vol = int(float(r[5])) if r[5] not in ("", None) else 0
            except (ValueError, IndexError):
                continue
            # 强制 OHLC 自洽（防个别源异常值把 open/close 抬出 high/low 区间）
            hi, lo = max(o, h, low, c), min(o, h, low, c)
            kline.append([r[0], o, hi, lo, c, vol])
        return kline
    return []


# ----------------------------------------------------------------------------
# 北交所(BSE)补充：baostock 的 query_all_stock 不返回 bj. 代码，且直接查 BJ
# 日线会报错(error 10004011)，即 baostock 结构性不覆盖北交所。北交所是 30% 涨跌幅
# 板块、对本战法有效，这里用东方财富免费接口单独补上(前复权，契约同 A股)。失败则
# 优雅跳过(仅告警)，不影响 baostock 主路径产出的沪深数据。
# ----------------------------------------------------------------------------
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _em_get_json(url, retries=2, timeout=12):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(0.4 * (attempt + 1))
    raise last if last else RuntimeError("请求失败")


def _em_bse_list():
    """东财 clist 取北交所个股 -> [(code6, name, f13)]。失败返回 []。"""
    out, pn = [], 1
    # m:0+t:81 是"全部新三板(NEEQ,约6800只)"，其中 +s:2048 才是真正上市的北交所(约300+只)；
    # 不加 s:2048 会混入大量非北交所的新三板挂牌股。
    fs = "m:0+t:81+s:2048"
    while True:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1"
               "&fltt=2&invt=2&fid=f12&fs=%s&fields=f12,f13,f14" % (pn, fs))
        try:
            data = _em_get_json(url)
        except Exception as e:
            print("  [BSE] 北交所列表第 %d 页失败: %s" % (pn, e))
            break
        d = data.get("data")
        if not d or not d.get("diff"):
            break
        diff = d["diff"]
        diff = list(diff.values()) if isinstance(diff, dict) else diff
        for x in diff:
            code, name = str(x.get("f12", "")), str(x.get("f14", ""))
            if not code or "退" in name:   # 同样剔除退市股
                continue
            # 前缀兜底：只保留北交所代码(920/8/4 开头)，防 fs 意外混入其它标的
            if not code.startswith(("920", "8", "4", "43")):
                continue
            out.append((code, name, x.get("f13")))
        if len(diff) < 100 or len(out) >= d.get("total", 0):
            break
        pn += 1
        time.sleep(0.05)
    return out


def _em_bse_kline(secid, lmt):
    """东财 push2his 取单只北交所日线(前复权 fqt=1)，返回契约顺序 kline。失败返回 []。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&klt=101&fqt=1"
           "&beg=0&end=20500101&lmt=%d&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57" % (secid, lmt))
    try:
        data = _em_get_json(url)
    except Exception:
        return []
    d = data.get("data") or {}
    out = []
    for s in d.get("klines") or []:
        p = s.split(",")
        if len(p) < 6:
            continue
        try:
            # 东财串顺序：date,open,close,high,low,volume —— 重排成契约 OHLCV
            o, c, h, low = float(p[1]), float(p[2]), float(p[3]), float(p[4])
            vol = int(float(p[5]))
        except (ValueError, IndexError):
            continue
        hi, lo = max(o, h, low, c), min(o, h, low, c)
        out.append([p[0], o, hi, lo, c, vol])
    out.sort(key=lambda r: r[0])
    # 东财在 beg/end 区间查询下会忽略 lmt 返回全历史，这里强制裁剪到上限，防文件膨胀。
    return out[-lmt:]


def fetch_bse(limit=None):
    """用东财补充北交所(bse)股票列表 -> [stock_obj]。整体失败/无数据时返回 []。"""
    lst = _em_bse_list()
    if not lst:
        print("  [BSE] 北交所列表不可用，跳过(不影响沪深数据)")
        return []
    if limit:
        lst = lst[:limit]
    print("  [BSE] 北交所个股 %d 只，逐只拉取日线..." % len(lst))
    results, ok = [], 0
    for code6, name, f13 in lst:
        secid = "%s.%s" % (f13 if f13 not in (None, "") else "0", code6)
        kline = _em_bse_kline(secid, MAX_KLINE_KEEP)
        if kline:
            results.append({
                "code": code6, "name": name, "board": "bse",
                "secid": secid, "kline": kline,
            })
            ok += 1
        time.sleep(0.02)
    print("  [BSE] 成功 %d / %d" % (ok, len(lst)))
    return results


def fetch_all(limit=None, days=450, incremental=False):
    print("登录 baostock ...")
    lg = bs.login()
    for attempt in range(3):
        if lg.error_code == "0":
            break
        print("登录失败(第%d次): %s，重试中..." % (attempt + 1, lg.error_msg))
        time.sleep(1.0 * (attempt + 1))
        lg = bs.login()
    if lg.error_code != "0":
        print("登录失败:", lg.error_msg)
        return None

    day = latest_trade_day()
    print("以交易日 %s 取全市场列表..." % day)
    rs = bs.query_all_stock(day=day)
    codes = []
    delisted = 0
    while rs is not None and rs.error_code == "0" and rs.next():
        row = rs.get_row_data()      # [code, tradeStatus, code_name]
        bs_code, _status, name = row[0], row[1], row[2]
        if not is_stock(bs_code):
            continue
        # 剔除退市股：退市整理期是仙股(价格塌到几毛)且即将摘牌，对"低位首板"战法是纯噪音。
        # A股 退市股会被更名为含"退"字(如 "恒久退"/"退市创兴")，据此识别。
        if "退" in (name or ""):
            delisted += 1
            continue
        codes.append((bs_code, name))
    print("个股共 %d 只（已剔除退市股 %d 只）" % (len(codes), delisted))

    if limit:
        codes = codes[:limit]
        print("--limit 生效，仅处理前 %d 只" % len(codes))

    full_start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    # 用"最新交易日"而非日历今天作为 end 边界：这样收盘后本地已是最新的票能正确命中
    # "已最新无需请求"分支(零 API 调用)，而不是每只都发一次拿回空结果。
    end = day

    out_path = os.path.join(SCRIPT_DIR, "data_A.json")
    existing = load_existing(out_path) if incremental else {}
    if incremental:
        print("  增量模式：本地已有 %d 只，只补最新数据" % len(existing))

    results = []
    total = len(codes)
    ok = skipped = fresh = appended = uptodate = 0
    for idx, (bs_code, name) in enumerate(codes, 1):
        code6 = bs_code.split(".")[1]
        secid = ("1." if bs_code.startswith("sh") else
                 "0." if bs_code.startswith("sz") else "0.") + code6

        old = existing.get(code6)
        if incremental and old and old.get("kline"):
            last_date = old["kline"][-1][0]
            if last_date >= end:
                kline = old["kline"]  # 已是最新，无需请求
                uptodate += 1
            else:
                incr_start = (datetime.date.fromisoformat(last_date)
                              + datetime.timedelta(days=1)).isoformat()
                new_rows = fetch_one(bs_code, incr_start, end)
                kline = merge_kline(old["kline"], new_rows)
                appended += 1
        else:
            kline = fetch_one(bs_code, full_start, end)
            fresh += 1

        if not kline:
            skipped += 1
        else:
            kline.sort(key=lambda x: x[0])
            results.append({
                "code": code6,
                "name": name,
                "board": classify_board(code6),
                "secid": secid,
                "kline": kline,
            })
            ok += 1
        if idx % 100 == 0 or idx == total:
            print("  %d/%d  成功 %d / 跳过 %d" % (idx, total, ok, skipped))

    bs.logout()

    # 北交所(bse)补充：baostock 不覆盖，用东财单独拉取后并入 A股 结果。
    bse_results = fetch_bse(limit=limit)
    if not bse_results and incremental:
        # 增量模式下东财补充失败时，沿用本地已有的北交所数据，避免把它们弄丢。
        bse_results = [s for s in existing.values() if s.get("board") == "bse"]
        if bse_results:
            print("  [BSE] 东财补充失败，沿用本地已有北交所 %d 只" % len(bse_results))
    results.extend(bse_results)

    results.sort(key=lambda s: s["code"])
    payload = {
        "market": "A",
        "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "count": len(results),
        "stocks": results,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print("\n完成：成功 %d / 跳过 %d / 共 %d" % (ok, skipped, total))
    if incremental:
        print("  其中：已最新无需请求 %d / 增量补数 %d / 全量首拉(新股或本地无历史) %d"
              % (uptodate, appended, fresh))
    print("写入 %s" % out_path)
    return payload


def main():
    ap = argparse.ArgumentParser(description="Baostock 版 A股 全市场日线下载（前复权）")
    ap.add_argument("--limit", type=int, default=None, help="只下前 N 只（调试用）")
    ap.add_argument("--days", type=int, default=450,
                    help="首次全量回溯自然日数（默认 450≈300 交易日）")
    ap.add_argument("--incremental", action="store_true",
                    help="增量更新：已收录股票只补最新数据，新股全量拉取（每天收盘后建议用这个）")
    args = ap.parse_args()
    fetch_all(limit=args.limit, days=args.days, incremental=args.incremental)


if __name__ == "__main__":
    main()
