#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
补抓 benchmarks.json 里缺失的东财标的（恒生科技指数 / 期货）。

单独成脚本而不是塞进 fetch_benchmarks.py，是因为东财限流冷却可达十几分钟，
需要用远比主流程耐心的节奏重试；主流程不该为这两个标的卡住。
主流程里东财失败只是跳过，之后由本脚本补齐。

用法：python patch_em_benchmarks.py [重试次数]
"""
import json
import os
import sys
import time
import urllib.request

OUT = "benchmarks.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120"
HOSTS = ["push2his.eastmoney.com", "61.push2his.eastmoney.com",
         "63.push2his.eastmoney.com"]
# (code, secid, 名称, 类别, 市场)
TARGETS = [
    ("HSTECHIDX", "124.HSTECH", "恒生科技指数",        "index",  "HK"),
    ("HTI",       "134.HTI_M",  "恒生科技指数期货主力",  "future", "HK"),
]
GAP = 8          # 每轮之间等这么多秒——东财冷却很长，急不得


def fetch(secid, limit=800):
    """东财日线 -> [[日期, 开, 高, 低, 收, 量], ...]；字段源顺序是 开收高低。"""
    for host in HOSTS:
        url = (f"https://{host}/api/qt/stock/kline/get?secid={secid}"
               f"&klt=101&fqt=0&beg=0&end=20500101&lmt={limit}"
               "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            d = json.loads(urllib.request.urlopen(req, timeout=20).read())
            rows = []
            for line in (d.get("data") or {}).get("klines") or []:
                p = line.split(",")
                if len(p) < 6:
                    continue
                rows.append([p[0], round(float(p[1]), 4), round(float(p[3]), 4),
                             round(float(p[4]), 4), round(float(p[2]), 4),
                             int(float(p[5]))])
            if rows:
                return rows
        except Exception:
            pass
        time.sleep(2)
    return []


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    if not os.path.exists(OUT):
        print(f"[错误] {OUT} 不存在，请先跑 fetch_benchmarks.py", file=sys.stderr)
        return 1

    with open(OUT, encoding="utf-8") as f:
        payload = json.load(f)
    have = {it["code"] for it in payload.get("items", [])}
    todo = [t for t in TARGETS if t[0] not in have]
    if not todo:
        print("东财标的都已存在，无需补抓")
        return 0
    print(f"待补抓: {', '.join(t[0] for t in todo)}")

    for r in range(1, rounds + 1):
        still = []
        for code, secid, name, cat, market in todo:
            rows = fetch(secid)
            if rows:
                payload["items"].append({
                    "code": code, "yahoo": secid, "name": name,
                    "category": cat, "market": market, "kline": rows,
                })
                print(f"  ✓ 第{r}轮 {code:<7} {len(rows)}条  "
                      f"{rows[0][0]} ~ {rows[-1][0]}  最新收 {rows[-1][4]}")
            else:
                still.append((code, secid, name, cat, market))
            time.sleep(3)

        todo = still
        if not todo:
            break
        print(f"  · 第{r}轮仍缺 {', '.join(t[0] for t in todo)}，{GAP*r}s 后重试")
        time.sleep(GAP * r)          # 线性拉长间隔，别把冷却越拖越久

    payload["count"] = len(payload["items"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    total = sum(len(i["kline"]) for i in payload["items"])
    print(f"\n完成：{payload['count']} 个标的 / {total} 行"
          + (f"（仍缺 {', '.join(t[0] for t in todo)}）" if todo else ""))
    return 1 if todo else 0


if __name__ == "__main__":
    sys.exit(main())
