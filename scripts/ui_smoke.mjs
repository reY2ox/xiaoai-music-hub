#!/usr/bin/env node
/* 前端 UI 冒烟测试（可选工具，不参与 pytest / CI）
 *
 * 用途：在真实后端 + jsdom 里加载 index.html / app.js，验证：
 *   1. 首屏无 JS 报错、booting 正常结束、6 个标签页可切换、搜索能出结果；
 *   2. 模拟「小爱语音暂停」中断态，验证横幅展示与「继续播放」按钮能发出 POST /resume；
 *   3. 音质切换会发出 POST /quality。
 *
 * 依赖 jsdom（仅本脚本需要，不进 requirements）：
 *   npm install --no-save --no-package-lock jsdom
 *
 * 用法（先启动一个 hub）：
 *   python3 app.py &                      # 默认 http://127.0.0.1:8010
 *   node scripts/ui_smoke.mjs             # 或 HUB_BASE=http://127.0.0.1:8012 node scripts/ui_smoke.mjs
 *
 * 退出码：0 通过；1 失败（打印 JSON 结果）。
 */
'use strict';

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const REPO = path.resolve(__dirname, '..');
const WEB = path.join(REPO, 'xiaoai_hub', 'web');
const BASE = (process.env.HUB_BASE || 'http://127.0.0.1:8010').replace(/\/$/, '');

let JSDOM;
let VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (error) {
  console.error('缺少 jsdom。请先执行：npm install --no-save --no-package-lock jsdom');
  process.exit(2);
}

const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(WEB, 'app.js'), 'utf8');
const errors = [];

function bootDom() {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => errors.push('jsdomError: ' + (error.stack || error.message)));
  virtualConsole.on('error', (...args) => errors.push('console.error: ' + args.map(String).join(' ')));
  const dom = new JSDOM(html, {
    url: BASE + '/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;
  if (!window.matchMedia) {
    window.matchMedia = () => ({
      matches: false, addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {},
    });
  }
  window.addEventListener('error', (event) => errors.push('window.error: ' + event.message));
  window.addEventListener('unhandledrejection', (event) => errors.push('unhandledrejection: ' + event.reason));
  return dom;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const jsonOf = async (response) => {
  try {
    return await response.json();
  } catch (error) {
    return null;
  }
};

/* ------------------------------------------------------------------ */
/* 1. 真实后端：首屏 / 标签页 / 搜索                                     */
/* ------------------------------------------------------------------ */
async function checkRealBackend() {
  const dom = bootDom();
  const { window } = dom;
  const calls = [];
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, BASE);
    const { signal, ...rest } = init; // jsdom 的 AbortSignal 不能被 Node fetch 接受
    const response = await globalThis.fetch(url, rest);
    calls.push({ method: (rest.method || 'GET').toUpperCase(), path: url.pathname + url.search, status: response.status });
    return response;
  };
  window.eval(appJs);
  await wait(2500);

  const document = window.document;
  const booting = document.body.classList.contains('booting');
  const tabChecks = {};
  for (const name of ['playlists', 'queue', 'local', 'downloads', 'settings', 'search']) {
    const button = document.querySelector(`#tab-${name}`);
    const panel = document.querySelector(`#panel-${name}`);
    if (!button || !panel) {
      tabChecks[name] = { missing: true };
      continue;
    }
    button.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await wait(120);
    tabChecks[name] = { active: button.classList.contains('is-active'), visible: !panel.hidden };
  }

  let searchRows = 0;
  const input = document.querySelector('#searchInput');
  if (input) {
    input.value = '晴天';
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    await wait(1200);
    searchRows = document.querySelectorAll('#searchList .song').length;
  }

  const stateOk = calls.some((call) => call.path === '/state' && call.status === 200);
  const tabsOk = Object.values(tabChecks).every((item) => item.active && item.visible);
  return {
    ok: !booting && stateOk && tabsOk && searchRows > 0 && errors.length === 0,
    booting,
    stateOk,
    tabChecks,
    searchRows,
    calls: calls.slice(0, 12),
    errors: [...new Set(errors)],
  };
}

/* ------------------------------------------------------------------ */
/* 2. 模拟外部暂停：中断横幅 + 继续播放                                  */
/* ------------------------------------------------------------------ */
async function checkInterruptedBanner() {
  errors.length = 0;
  let interrupted = true;
  const dom = bootDom();
  const { window } = dom;
  const calls = [];
  const settings = {
    quality: 'high',
    quality_levels: { standard: '标准 192k', high: '高品质 320k', lossless: '无损 FLAC' },
    play_mode: 'sequential',
    play_modes: ['sequential', 'loop', 'single', 'shuffle'],
    quiet_hours: '23:30-08:00',
    quiet_active: false,
    device: null,
    auth_required: false,
    version: '3.0.0',
  };
  const payload = () => ({
    ok: true,
    version: '3.0.0',
    server: { lan_ip: '127.0.0.1', port: 8010, music_dir: '/music' },
    device: { ip: '127.0.0.1', name: '测试音箱', reachable: false },
    player: {
      state: 'PLAYING', title: '晴天', artist: '周杰伦',
      position: 30, duration: 269, position_text: '00:00:30', duration_text: '00:04:29',
      uri: 'http://127.0.0.1:8010/media/1', active: !interrupted, manual_stop: false,
      interrupted: interrupted
        ? { reason: 'external_pause', title: '晴天', artist: '周杰伦', position: 30, at: 1700000000 }
        : null,
      queue: { playlist_id: 2, playlist_name: '冒烟歌单', index: 0, total: 1, mode: 'sequential', history: [] },
    },
    settings,
    download: { pending: 0, running: 0, done: 0, failed: 0 },
    account: { ok: true, logged_in: true, uid: 42, nickname: '测试', avatar: '', vip: true },
    quiet: false,
  });
  const jsonResponse = (data, status = 200) => ({
    ok: status < 400,
    status,
    headers: { get: (name) => (String(name).toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => data,
    text: async () => JSON.stringify(data),
  });
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, BASE);
    const { signal, ...rest } = init;
    const method = (rest.method || 'GET').toUpperCase();
    calls.push({ method, path: url.pathname + url.search });
    if (url.pathname === '/state') return jsonResponse(payload());
    if (url.pathname === '/resume') {
      interrupted = false;
      return jsonResponse({ ok: true, resumed: 'replay' });
    }
    if (url.pathname === '/quality') return jsonResponse({ ok: true, quality: 'lossless' });
    return jsonResponse({ ok: true });
  };
  window.eval(appJs);
  await wait(2200);

  const document = window.document;
  const banner = document.querySelector('#interruptBanner');
  const before = {
    hidden: banner.hidden,
    title: document.querySelector('#interruptTitle')?.textContent.trim(),
    meta: document.querySelector('#interruptMeta')?.textContent.trim(),
  };
  document.querySelector('#interruptResumeBtn')?.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await wait(400);
  const resumeCalled = calls.some((call) => call.method === 'POST' && call.path.startsWith('/resume'));
  await wait(1800);
  const after = { hidden: banner.hidden };

  const quality = document.querySelector('#qualitySelect');
  if (quality) {
    quality.value = 'lossless';
    quality.dispatchEvent(new window.Event('change', { bubbles: true }));
    await wait(300);
  }
  const qualityCall = calls.some((call) => call.method === 'POST' && call.path.startsWith('/quality'));

  return {
    ok: !before.hidden && before.title.length > 0 && resumeCalled && after.hidden
      && qualityCall && errors.length === 0,
    before,
    after,
    resumeCalled,
    qualityCall,
    playerTitle: document.querySelector('#playerTitle')?.textContent.trim(),
    errors: [...new Set(errors)],
  };
}

(async () => {
  const real = await checkRealBackend();
  const interrupted = await checkInterruptedBanner();
  const result = { hub: BASE, realBackend: real, interruptedBanner: interrupted };
  console.log(JSON.stringify(result, null, 2));
  process.exit(real.ok && interrupted.ok ? 0 : 1);
})();
