#!/usr/bin/env node
/*
 * prescan.js —— 云端预计算脚本（GitHub Actions 里跑，本地也能跑）
 * ============================================================
 * 手机端不可能加载几十上百 MB 的全量 K 线，所以在云端把重活干完：
 *   1. 从 index.html 里抽出纯函数引擎（引擎只有一份，不复制代码，永不失同步）
 *   2. 读 data_<市场>.json 全量数据，用默认参数跑「选股扫描」+「全历史信号体检」
 *   3. 输出 site/lite_<市场>.json —— 只含：
 *        扫描命中列表 + 命中股票的K线窗口(供画图) + 体检统计摘要 + 最近交易明细
 *      单市场几百 KB，手机秒开。
 *
 * 用法： node prescan.js [HK,US]     （默认 HK,US）
 * 输出： site/lite_HK.json site/lite_US.json，并把 index.html 等静态文件拷进 site/
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DIR = __dirname;
const OUT_DIR = path.join(DIR, 'site');
// 轻量包里每只命中股票带的K线窗口：D0 往前 70 根 + 之后全部（画图窗口是 D0-60~+15，留余量）
const CHART_LOOKBACK = 110;   // 多带些历史，手机端K线手势平移才有余地
const RECENT_TRADES = 150;   // 体检明细只带最近这么多笔（统计数字是全量算的）

/* ---- 从 index.html 抽引擎（<script> 起始到「UI 层」分隔线为止的纯函数区） ---- */
function loadEngine() {
  const html = fs.readFileSync(path.join(DIR, 'index.html'), 'utf-8');
  const m = html.match(/<script>([\s\S]*)<\/script>/);
  if (!m) throw new Error('index.html 里找不到 <script> 段');
  let cut = m[1].indexOf('UI 层');
  if (cut < 0) throw new Error('index.html 里找不到「UI 层」分隔注释——引擎抽取锚点丢了');
  cut = m[1].lastIndexOf('/*', cut);   // 回退到该注释块的 /* 开头，别把半个注释留在引擎源码里
  // const/function 声明不会自动挂到 vm 的 globalThis 上，末尾补一个显式导出
  const src = m[1].slice(0, cut)
    + '\n;globalThis.__ENGINE__ = { DEFAULT_PARAMS, PARAM_PRESETS, PRESET_ORDER, scanMarket, backtest, computeStats, computeTierStats, computeCalStats };';
  const ctx = vm.createContext({ console });
  vm.runInContext(src, ctx, { filename: 'engine-from-index.html' });
  return ctx.__ENGINE__;
}

function main() {
  const markets = (process.argv[2] || 'HK,US').split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
  const E = loadEngine();
  fs.mkdirSync(OUT_DIR, { recursive: true });

  for (const mkt of markets) {
    const dataPath = path.join(DIR, 'data_' + mkt + '.json');
    if (!fs.existsSync(dataPath)) { console.log(mkt + ': 没有 ' + dataPath + '，跳过'); continue; }
    const data = JSON.parse(fs.readFileSync(dataPath, 'utf-8'));
    const stocks = data.stocks || [];
    stocks.forEach(s => { s.market = mkt; });
    const indexKline = (data.index && data.index.kline) ? data.index.kline : null;

    // 三档参数(严格/标准/宽松)各跑一遍扫描 + 全历史体检
    const byCode = new Map(stocks.map(s => [s.code, s]));
    const charts = {};   // 三档命中股票K线窗口的并集
    const presets = {};
    const presetDefs = E.PARAM_PRESETS[mkt] || { '标准': {} };
    for (const pname of E.PRESET_ORDER) {
      if (!(pname in presetDefs)) continue;
      const params = { ...E.DEFAULT_PARAMS, ...presetDefs[pname] };
      const scan = E.scanMarket(stocks, params, indexKline);
      scan.sort((a, b) => (a.state === b.state ? 0 : a.state === '可买入' ? -1 : 1)
        || (a.d0Date < b.d0Date ? 1 : a.d0Date > b.d0Date ? -1 : 0));
      const bt = E.backtest(stocks, params, null, indexKline);
      presets[pname] = {
        scan,
        stats: bt.stats,
        tierStats: mkt === 'US' ? E.computeTierStats(bt.trades) : null,
        calStats: mkt === 'US' ? E.computeCalStats(bt.trades) : null,
        trades_recent: bt.trades.slice(-RECENT_TRADES),
        trades_total: bt.trades.length,
      };
      for (const r of scan) {
        const s = byCode.get(r.code);
        if (!s || !s.kline || charts[r.code]) continue;
        const from = Math.max(0, (r.d0Idx != null ? r.d0Idx : s.kline.length - 1) - CHART_LOOKBACK);
        charts[r.code] = { name: s.name, board: s.board, kline: s.kline.slice(from) };
      }
      console.log(mkt + ' [' + pname + ']: 命中 ' + scan.length
        + ' (可买入 ' + scan.filter(r => r.state === '可买入').length + ') · 体检 '
        + bt.stats.count + ' 笔 胜率 ' + bt.stats.winRate.toFixed(1) + '%');
    }

    let minD = '9999', maxD = '0';
    for (const s of stocks) {
      const k = s.kline;
      if (k && k.length) { if (k[0][0] < minD) minD = k[0][0]; if (k[k.length - 1][0] > maxD) maxD = k[k.length - 1][0]; }
    }

    const std = presets['标准'];
    const lite = {
      market: mkt, mode: 'lite',
      generated_at: new Date().toISOString().replace('T', ' ').slice(0, 19) + ' UTC',
      stock_count: stocks.length,
      data_range: [minD, maxD],
      presets,
      charts,
      // 顶层保留标准档字段：兼容手机上还缓存着旧版页面的情况
      scan: std.scan, stats: std.stats, tierStats: std.tierStats, calStats: std.calStats,
      trades_recent: std.trades_recent, trades_total: std.trades_total,
    };
    const outPath = path.join(OUT_DIR, 'lite_' + mkt + '.json');
    fs.writeFileSync(outPath, JSON.stringify(lite));
    console.log(mkt + ': K线窗口 ' + Object.keys(charts).length + ' 只 · '
      + (fs.statSync(outPath).size / 1024).toFixed(0) + ' KB -> ' + outPath);
  }

  // 静态文件一并拷进 site/（Pages 发布目录）
  for (const f of ['index.html', 'manifest.webmanifest', 'icon.svg', 'sw.js']) {
    const src = path.join(DIR, f);
    if (fs.existsSync(src)) fs.copyFileSync(src, path.join(OUT_DIR, f));
  }
  console.log('site/ 就绪');
}

main();
