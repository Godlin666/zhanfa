#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_info.py —— 给轻量包(site/lite_*.json)里的命中股票补充公司行业信息
在 prescan.js 之后运行。只查询命中的股票（三档并集，几百只），且带缓存
(info_cache.json，随 data 分支保存)：查过的不再请求，日常每天只新增几只。
数据来自 yfinance 的 .info（免费），sector 翻译成中文。
"""
import os
import json
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(SCRIPT_DIR, "site")
CACHE_PATH = os.path.join(SCRIPT_DIR, "info_cache.json")

# 雅虎的 sector 只有十来种，翻成中文；industry 太多不翻，原文给英文
SECTOR_CN = {
    "Technology": "科技", "Financial Services": "金融", "Healthcare": "医疗健康",
    "Consumer Cyclical": "可选消费", "Consumer Defensive": "必需消费",
    "Industrials": "工业", "Energy": "能源", "Basic Materials": "原材料",
    "Real Estate": "房地产", "Utilities": "公用事业",
    "Communication Services": "通信服务",
}

import fetch_hkus_yf as F   # 复用代理配置与港股 ticker 转换
import yfinance as yf


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def fetch_info(ticker):
    """查单只股票的 sector/industry；失败返回空(也写进缓存,避免反复重试退市票)。"""
    try:
        info = yf.Ticker(ticker).get_info() or {}
        return {"sector": info.get("sector") or "", "industry": info.get("industry") or ""}
    except Exception as e:
        print("  info 获取失败 %s: %s" % (ticker, e))
        return {"sector": "", "industry": ""}


def main():
    F.apply_proxy(F.resolve_proxy(None))
    cache = load_cache()
    changed = False

    for mkt in ("HK", "US"):
        lite_path = os.path.join(SITE_DIR, "lite_%s.json" % mkt)
        if not os.path.exists(lite_path):
            continue
        with open(lite_path, encoding="utf-8") as f:
            lite = json.load(f)
        charts = lite.get("charts", {})

        # 只查缓存里没有的
        todo = [c for c in charts if c not in cache]
        print("%s: 命中 %d 只，其中 %d 只需要新查行业信息" % (mkt, len(charts), len(todo)))
        for i, code in enumerate(todo):
            ticker = code if mkt == "US" else F.hk_ticker(code)
            cache[code] = fetch_info(ticker) if ticker else {"sector": "", "industry": ""}
            changed = True
            if (i + 1) % 20 == 0:
                print("  行业信息进度 %d/%d" % (i + 1, len(todo)), flush=True)
            time.sleep(0.25)   # 限速，别惹恼雅虎

        # 注入 lite：scan 行(各档+顶层) 和 charts
        def enrich(r):
            c = cache.get(r.get("code"))
            if c and (c["sector"] or c["industry"]):
                r["sector"] = SECTOR_CN.get(c["sector"], c["sector"])
                r["industry"] = c["industry"]

        for preset in lite.get("presets", {}).values():
            for r in preset.get("scan", []):
                enrich(r)
        for r in lite.get("scan", []):
            enrich(r)
        for code, cobj in charts.items():
            c = cache.get(code)
            if c and (c["sector"] or c["industry"]):
                cobj["sector"] = SECTOR_CN.get(c["sector"], c["sector"])
                cobj["industry"] = c["industry"]

        with open(lite_path, "w", encoding="utf-8") as f:
            json.dump(lite, f, ensure_ascii=False)
        print("%s: 行业信息已注入 %s" % (mkt, lite_path))

    if changed:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        print("缓存已更新: %s (%d 只)" % (CACHE_PATH, len(cache)))


if __name__ == "__main__":
    main()
