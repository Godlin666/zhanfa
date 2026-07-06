#!/bin/bash
# ============================================================
# 战法选股工具 启动器（macOS 双击运行）
# 用本地服务器打开 index.html，让工具能自动读取最新的
# data_A.json / data_HK.json / data_US.json 真实数据。
#
# 为什么需要它：浏览器出于安全限制，直接双击 index.html
# (file:// 方式) 打开时无法自动读取本地数据文件，只能显示
# 内置演示样本。通过本地服务器打开就能自动加载真实数据。
# ============================================================

cd "$(dirname "$0")"

PORT=8765
# 端口被占用时自动往后找一个空闲端口
while lsof -ti:$PORT >/dev/null 2>&1; do
  PORT=$((PORT+1))
done

echo "正在启动本地服务器 (端口 $PORT) ..."
python3 -m http.server $PORT >/dev/null 2>&1 &
PID=$!
trap "kill $PID 2>/dev/null" EXIT

# 等服务器真正起来再打开浏览器，最多等约 5 秒，避免白屏
for i in $(seq 1 25); do
  if ! kill -0 $PID 2>/dev/null; then
    echo "❌ 本地服务器启动失败（端口 $PORT 可能被占用或 python3 异常）。"
    exit 1
  fi
  if lsof -ti:$PORT >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

open "http://localhost:$PORT/index.html"

echo ""
echo "=================================================="
echo "  ✅ 战法选股工具已在浏览器打开"
echo "     http://localhost:$PORT/index.html"
echo "=================================================="
echo ""
echo "  · 工具会自动加载最新的 data_A/HK/US.json"
echo "  · 想更新数据：先跑 python3 update_all.py --incremental"
echo "    再刷新浏览器即可"
echo ""
echo "  ⚠️  用完后关闭这个终端窗口来停止服务器"
echo "     (窗口开着期间工具才能正常读取数据)"
echo ""

wait $PID
