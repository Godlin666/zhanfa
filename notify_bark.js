#!/usr/bin/env node
/*
 * notify_bark.js —— 收盘后把当日信号推送到 iPhone（Bark App）
 * 在 GitHub Actions 里于 prescan 之后运行：
 *     BARK_KEY=xxx node notify_bark.js HK   （参数=本次更新的市场,逗号分隔）
 * 规则：
 *   - BARK_KEY 未配置(仓库 secret) → 静默跳过，不报错
 *   - 只在【标准档有"可买入"信号】时推送，避免每天空消息打扰
 *   - 点通知直接打开选股页面
 */
const fs = require('fs');
const path = require('path');
const https = require('https');

const KEY = (process.env.BARK_KEY || '').trim();
if (!KEY) { console.log('未配置 BARK_KEY secret，跳过手机推送'); process.exit(0); }

const PAGE_URL = 'https://godlin666.github.io/zhanfa/';
const markets = (process.argv[2] || 'HK,US').split(',').map(s => s.trim().toUpperCase()).filter(Boolean);

const parts = [];
for (const m of markets) {
  const p = path.join(__dirname, 'site', 'lite_' + m + '.json');
  if (!fs.existsSync(p)) continue;
  const lite = JSON.parse(fs.readFileSync(p, 'utf-8'));
  const std = (lite.presets && lite.presets['标准']) || lite;
  const buys = (std.scan || []).filter(r => r.state === '可买入');
  if (!buys.length) continue;
  const label = m === 'US' ? '美股' : '港股';
  const top = buys.slice(0, 5).map(r => r.code + (r.tier ? '(' + r.tier + ')' : '')).join(' ');
  parts.push(label + ' 可买入 ' + buys.length + ' 只：' + top + (buys.length > 5 ? ' …' : ''));
}

if (!parts.length) { console.log('标准档没有可买入信号，今天不推送'); process.exit(0); }

const title = '战法选股 · 今日信号';
const body = parts.join('\n');
const url = 'https://api.day.app/' + KEY + '/' + encodeURIComponent(title) + '/' + encodeURIComponent(body)
  + '?group=zhanfa&url=' + encodeURIComponent(PAGE_URL);

https.get(url, res => {
  console.log('Bark 推送 HTTP', res.statusCode);
  res.resume();
  process.exitCode = (res.statusCode === 200) ? 0 : 0;   // 推送失败也不让整条流水线红
}).on('error', e => { console.log('Bark 推送失败(不影响流水线):', e.message); });
