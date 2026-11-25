/* ==========================================================================
 * 小爱音乐中枢 v3 · 前端逻辑
 * 零构建、零依赖、原生 ES2020。
 *
 * 结构：
 *   1. 常量与工具        2. 全局状态          3. 本地存储
 *   4. Toast / 模态框    5. API 封装          6. 渲染（按区域拆分）
 *   7. 数据加载          8. 用户动作          9. 轮询
 *  10. 事件绑定         11. 启动
 *
 * 安全约定：所有来自接口的文本一律走 textContent / createElement，
 * 本文件不使用 HTML 字符串拼接（静态图标由 SVG sprite + createElementNS 构建）。
 * ========================================================================== */
'use strict';

(() => {
  /* ========================================================================
   * 1. 常量与工具
   * ====================================================================== */
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const POLL_STATE_MS = 1500;          // 播放状态轮询
  const POLL_DOWNLOAD_MS = 2000;       // 下载任务轮询
  const SEARCH_DEBOUNCE_MS = 350;      // 搜索输入防抖
  const LOCAL_DEBOUNCE_MS = 350;       // 本地曲库搜索防抖
  const VOLUME_DEBOUNCE_MS = 180;      // 音量提交防抖
  const REQUEST_TIMEOUT = 15000;       // 默认请求超时
  const BOOT_TIMEOUT_MS = 7000;        // 首屏骨架兜底

  const STORAGE = {
    tab: 'xmh.tab',
    target: 'xmh.target',
    volume: 'xmh.volume',
    playlist: 'xmh.playlist',
  };

  const TAB_IDS = ['search', 'playlists', 'queue', 'local', 'downloads', 'settings'];

  const DEFAULT_MODES = ['sequential', 'loop', 'single', 'shuffle'];

  const MODE_META = {
    sequential: { label: '顺序播放', short: '顺序', icon: 'order' },
    loop: { label: '列表循环', short: '循环', icon: 'repeat' },
    single: { label: '单曲循环', short: '单曲', icon: 'repeat-one' },
    shuffle: { label: '随机播放', short: '随机', icon: 'shuffle' },
  };

  const PLAYER_STATE_META = {
    PLAYING: { text: '播放中', tone: 'ok' },
    PAUSED_PLAYBACK: { text: '已暂停', tone: 'warn' },
    TRANSITIONING: { text: '缓冲中', tone: 'info' },
    STOPPED: { text: '已停止', tone: 'muted' },
    NO_MEDIA_PRESENT: { text: '空闲', tone: 'muted' },
  };

  const QUALITY_FALLBACK = {
    standard: '标准 192k',
    high: '高品质 320k',
    lossless: '无损 FLAC',
  };

  const DOWNLOAD_META = {
    pending: { text: '排队中', tone: 'info' },
    running: { text: '下载中', tone: 'info' },
    done: { text: '已完成', tone: 'ok' },
    failed: { text: '失败', tone: 'err' },
    canceled: { text: '已取消', tone: 'muted' },
  };

  const QR_STATUS_TEXT = {
    waiting: '等待扫码…',
    scanned: '已扫码，请在手机上确认',
    success: '登录成功',
    expired: '二维码已过期',
    error: '二维码获取失败',
  };

  /** 下载任务排序：进行中 → 排队 → 失败 → 完成 → 取消 */
  const DOWNLOAD_ORDER = { running: 0, pending: 1, failed: 2, done: 3, canceled: 4 };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function str(value) {
    if (typeof value === 'string') return value;
    if (value === null || value === undefined) return '';
    return String(value);
  }

  function clamp(value, min, max) {
    const number = Number(value);
    if (!Number.isFinite(number)) return min;
    return Math.min(max, Math.max(min, number));
  }

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  function fmtTime(value) {
    const total = Math.max(0, Math.round(Number(value) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    if (hours > 0) return hours + ':' + pad2(minutes) + ':' + pad2(seconds);
    return pad2(minutes) + ':' + pad2(seconds);
  }

  function fmtSize(bytes) {
    const value = Number(bytes) || 0;
    if (value <= 0) return '0 B';
    if (value < 1024) return value + ' B';
    if (value < 1024 * 1024) return Math.round(value / 1024) + ' KB';
    if (value < 1024 * 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
    return (value / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  function firstChar(text) {
    const value = str(text).trim();
    if (!value) return '♪';
    return Array.from(value)[0];
  }

  /** 根据文本生成稳定封面渐变（色相 200–269，蓝 → 紫，保持品牌一致性） */
  function setCoverVars(node, text) {
    const value = str(text).trim();
    if (!value) {
      node.style.setProperty('--cover-a', '#33415c');
      node.style.setProperty('--cover-b', '#171d2a');
      return;
    }
    let hash = 0;
    for (const char of Array.from(value)) hash = (hash * 31 + char.codePointAt(0)) % 100000;
    const hue = 200 + (hash % 70);
    const lightA = 48 + ((hash >> 3) % 12); // 48%–59%
    const lightB = lightA - 21;
    node.style.setProperty('--cover-a', 'hsl(' + hue + ' 64% ' + lightA + '%)');
    node.style.setProperty('--cover-b', 'hsl(' + ((hue + 22) % 360) + ' 58% ' + lightB + '%)');
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function debounce(fn, wait) {
    let timer = 0;
    const wrapped = (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        timer = 0;
        fn(...args);
      }, wait);
    };
    wrapped.cancel = () => {
      clearTimeout(timer);
      timer = 0;
    };
    return wrapped;
  }

  function idValue(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : value;
  }

  function readStore(key, fallback) {
    try {
      const value = window.localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (error) {
      return fallback;
    }
  }

  function writeStore(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
    } catch (error) {
      /* localStorage 不可用时静默降级 */
    }
  }

  /* ---------------------------- DOM 构建工具 ---------------------------- */
  function appendChildren(node, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }

  /**
   * 创建元素。props 支持：class / text / type / value / title / dataset /
   * attrs / style（对象）/ on（事件表）；其余键作为 attribute。
   */
  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = String(value);
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key === 'on') {
          for (const [eventName, handler] of Object.entries(value)) {
            node.addEventListener(eventName, handler);
          }
        } else if (key === 'attrs') {
          for (const [attrName, attrValue] of Object.entries(value)) {
            if (attrValue !== null && attrValue !== undefined) node.setAttribute(attrName, String(attrValue));
          }
        } else if (key === 'style' && typeof value === 'object') {
          Object.assign(node.style, value);
        } else {
          node.setAttribute(key, String(value));
        }
      }
    }
    appendChildren(node, children);
    return node;
  }

  function mount(node, ...children) {
    if (!node) return node;
    while (node.firstChild) node.removeChild(node.firstChild);
    appendChildren(node, children);
    return node;
  }

  function setText(node, text) {
    if (!node) return;
    const value = str(text);
    if (node.textContent !== value) node.textContent = value;
  }

  /** 内联 SVG 图标（sprite <use>），替代 emoji/图标字体 */
  function icon(name, className = 'icon') {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function setButtonIcon(button, name) {
    if (!button) return;
    if (button.dataset.icon === name) return;
    button.dataset.icon = name;
    mount(button, icon(name));
  }

  function coverNode(title, coverUrl, modifier) {
    const node = el('div', { class: 'cover ' + (modifier || 'cover--sm'), 'aria-hidden': 'true' });
    setCoverVars(node, title);
    node.appendChild(el('span', { class: 'cover__char', text: firstChar(title) }));
    if (coverUrl && /^https?:\/\//i.test(coverUrl)) {
      const img = el('img', {
        src: coverUrl,
        alt: '',
        loading: 'lazy',
        referrerpolicy: 'no-referrer',
      });
      img.addEventListener('error', () => img.remove(), { once: true });
      node.appendChild(img);
    }
    return node;
  }

  function avatarNode(account, className) {
    const node = el('span', { class: className, 'aria-hidden': 'true' });
    if (account && account.avatar) {
      const img = el('img', {
        src: account.avatar,
        alt: '',
        loading: 'lazy',
        referrerpolicy: 'no-referrer',
      });
      img.addEventListener(
        'error',
        () => mount(node, el('span', { text: firstChar((account && account.nickname) || '云') })),
        { once: true },
      );
      node.appendChild(img);
    } else if (account && account.loggedIn) {
      node.appendChild(el('span', { text: firstChar(account.nickname || '云') }));
    } else {
      node.appendChild(icon('user'));
    }
    return node;
  }

  function emptyState(options) {
    const { art = 'empty-music', title = '', desc = '', actionLabel = '', actionIcon = '', onAction = null } = options || {};
    const wrap = el(
      'div',
      { class: 'empty' },
      el('span', { class: 'empty__art', 'aria-hidden': 'true' }, icon(art)),
      el('p', { class: 'empty__title', text: title }),
      desc ? el('p', { class: 'empty__desc', text: desc }) : null,
    );
    if (actionLabel && typeof onAction === 'function') {
      wrap.appendChild(
        el(
          'button',
          { class: 'btn btn--primary btn--sm empty__action', type: 'button', on: { click: onAction } },
          actionIcon ? icon(actionIcon) : null,
          el('span', { text: actionLabel }),
        ),
      );
    }
    return wrap;
  }

  function loadingRows(count) {
    const rows = [];
    for (let index = 0; index < count; index += 1) {
      rows.push(
        el(
          'div',
          { class: 'loading-row' },
          el('div', { class: 'sk sk--avatar' }),
          el(
            'div',
            { class: 'sk-stack' },
            el('div', { class: 'sk sk--line sk--w60' }),
            el('div', { class: 'sk sk--line sk--w42' }),
          ),
        ),
      );
    }
    return el('div', { class: 'loading-rows', 'aria-hidden': 'true' }, rows);
  }

  /* ========================================================================
   * 2. 全局状态
   * ====================================================================== */
  const state = {
    booting: true,
    booted: false,
    online: null, // null=连接中，true/false
    hadOnline: false,
    authRequired: false,
    authWarned: false,
    polling: false,
    scanning: false,

    tab: 'search',
    snapshot: null,
    server: {},
    device: { ip: '', name: '音箱', reachable: true, model: '' },
    activeDevice: '',
    devices: [],
    settings: {
      quality: 'high',
      qualityLevels: Object.assign({}, QUALITY_FALLBACK),
      playMode: 'sequential',
      playModes: DEFAULT_MODES.slice(),
      quietHours: '',
      quietActive: false,
      authRequired: false,
      version: '',
    },
    settingsDirtyUntil: 0,

    player: {
      state: 'STOPPED',
      title: '',
      artist: '',
      position: 0,
      duration: 0,
      positionText: '',
      durationText: '',
      interrupted: null,
      queue: null,
      active: false,
      manualStop: false,
      volume: null,
    },

    account: { loggedIn: false, nickname: '', uid: null, avatar: '', vip: false, reason: '' },

    playlists: [],
    currentPlaylistId: '',
    targetPlaylistId: '',
    tracks: [],
    tracksLoading: false,

    search: { q: '', songs: [], loading: false, timer: 0, seq: 0 },

    queue: null,

    local: { q: '', tracks: [], loading: false, loaded: false, timer: 0, seq: 0 },

    downloads: { jobs: [], stats: null, loading: false, errorAt: 0 },
    downloadStats: { pending: 0, running: 0, done: 0, failed: 0, canceled: 0, runningWorkers: 0 },

    netease: { open: false, loading: false, playlists: [], syncing: false, timer: 0 },

    seek: { dragging: false, value: 0 },

    volume: { value: 30, mutedFrom: 30, dirtyUntil: 0, timer: 0 },

    qr: { timer: 0, token: 0 },

    dialog: { resolve: null, validate: null, confirmBtn: null },

    drag: { from: -1 },
    interruptDismissedKey: '',
    localScanning: false,

    timers: { state: 0, download: 0, queueCounter: 0 },
  };

  const openModals = new Set();
  let lastFocusedElement = null;

  /* ========================================================================
   * 3. API 封装
   * ====================================================================== */
  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.name = 'ApiError';
      this.status = status || 0;
      this.data = data || null;
      this.retryable = false;
    }
  }

  function buildUrl(path, params) {
    if (!params) return path;
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value === null || value === undefined || value === '') continue;
      search.append(key, String(value));
    }
    const query = search.toString();
    if (!query) return path;
    return path + (path.includes('?') ? '&' : '?') + query;
  }

  function extractErrorMessage(data) {
    if (!data) return '';
    if (typeof data === 'string') return data.slice(0, 200);
    if (typeof data.error === 'string') return data.error;
    if (typeof data.message === 'string') return data.message;
    return '';
  }

  /**
   * 统一请求封装：
   *   - JSON 解析与 {ok:false} 错误归一
   *   - 401 提示需要密码
   *   - 网络错误 / 超时 / 502/503/504 自动重试一次
   *   - 统一 toast（silent 可关闭）
   */
  async function api(path, options = {}) {
    const {
      method = 'GET',
      params = null,
      body,
      timeout = REQUEST_TIMEOUT,
      silent = false,
      retry = true,
    } = options;

    const url = buildUrl(path, params);
    const maxAttempts = retry === false ? 1 : 2;
    let lastError = null;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      try {
        const headers = {};
        if (body !== undefined) headers['Content-Type'] = 'application/json';
        const response = await fetch(url, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          credentials: 'same-origin',
          cache: method === 'GET' ? 'no-store' : 'default',
          signal: controller.signal,
        });
        clearTimeout(timer);

        let data = null;
        const contentType = response.headers.get('content-type') || '';
        try {
          data = contentType.indexOf('json') >= 0 ? await response.json() : await response.text();
        } catch (parseError) {
          data = null;
        }

        if (response.status === 401) {
          handleUnauthorized();
          throw new ApiError('需要访问密码', 401, data);
        }

        if (!response.ok) {
          const message = extractErrorMessage(data) || '请求失败（HTTP ' + response.status + '）';
          const error = new ApiError(message, response.status, data);
          error.retryable = response.status === 502 || response.status === 503 || response.status === 504;
          if (error.retryable && attempt < maxAttempts) {
            lastError = error;
            await sleep(350);
            continue;
          }
          if (!silent) toast(message, 'error');
          throw error;
        }

        if (data && typeof data === 'object' && data.ok === false) {
          const message = extractErrorMessage(data) || '操作失败';
          const error = new ApiError(message, response.status, data);
          if (!silent) toast(message, 'error');
          throw error;
        }

        return data;
      } catch (error) {
        clearTimeout(timer);
        if (error instanceof ApiError) throw error;
        // 网络错误 / 超时：自动重试一次
        lastError = new ApiError(
          error && error.name === 'AbortError' ? '请求超时，请稍后重试' : '网络异常，请检查连接',
          0,
          null,
        );
        if (attempt < maxAttempts) {
          await sleep(400);
          continue;
        }
        if (!silent) toast(lastError.message, 'error');
        throw lastError;
      }
    }

    throw lastError || new ApiError('请求失败', 0, null);
  }

  function toastApiError(error, fallback) {
    if (!error) {
      toast(fallback || '操作失败', 'error');
      return;
    }
    if (error.status === 401) {
      handleUnauthorized();
      return;
    }
    const message = str(error.message) || fallback || '操作失败';
    toast(message, 'error');
  }

  function handleUnauthorized() {
    state.authRequired = true;
    const banner = $('#authBanner');
    if (banner) banner.hidden = false;
    if (state.authWarned) return;
    state.authWarned = true;
    toast('需要访问密码：请在浏览器弹窗中登录，或刷新页面重试', 'error', { duration: 6000 });
  }

  /* ========================================================================
   * 4. Toast / 模态框 / 对话框
   * ====================================================================== */
  function toast(message, type = 'info', options = {}) {
    const holder = $('#toasts');
    if (!holder) return;
    const iconName = type === 'success' ? 'check' : type === 'error' ? 'alert' : type === 'warn' ? 'alert' : 'info';
    const item = el(
      'div',
      { class: 'toast toast--' + type, role: 'status' },
      icon(iconName),
      el('span', { class: 'toast__text', text: message }),
    );
    holder.appendChild(item);
    requestAnimationFrame(() => item.classList.add('is-in'));
    while (holder.children.length > 4) holder.firstElementChild.remove();
    const duration = options.duration || 3200;
    setTimeout(() => {
      item.classList.add('is-out');
      setTimeout(() => item.remove(), 260);
    }, duration);
  }

  function isModalOpen() {
    return openModals.size > 0;
  }

  function openModal(selector) {
    const node = $(selector);
    if (!node) return;
    lastFocusedElement = document.activeElement;
    node.hidden = false;
    openModals.add(selector);
    requestAnimationFrame(() => node.classList.add('is-open'));
    const focusTarget =
      node.querySelector('input, select, textarea') ||
      node.querySelector('.modal__foot .btn--primary') ||
      node.querySelector('button, [href]');
    if (focusTarget) setTimeout(() => focusTarget.focus(), 120);
  }

  function closeModal(selector) {
    const node = $(selector);
    if (!node) return;
    node.classList.remove('is-open');
    openModals.delete(selector);
    if (selector === '#accountModal') stopQrPoll();
    setTimeout(() => {
      if (!node.classList.contains('is-open')) node.hidden = true;
    }, 220);
    if (lastFocusedElement && typeof lastFocusedElement.focus === 'function') {
      try {
        lastFocusedElement.focus();
      } catch (error) {
        /* 元素已移除时忽略 */
      }
    }
    lastFocusedElement = null;
  }

  function closeTopModal() {
    if (openModals.has('#dialog')) {
      closeDialog(null);
      return;
    }
    if (openModals.has('#accountModal')) closeModal('#accountModal');
  }

  function openDialog(options) {
    const {
      title = '提示',
      message = '',
      body = null,
      confirmText = '确定',
      cancelText = '取消',
      danger = false,
      validate = null,
    } = options || {};

    return new Promise((resolve) => {
      state.dialog.resolve = resolve;
      state.dialog.validate = validate;
      setText($('#dialogTitle'), title);

      const parts = [];
      if (message) parts.push(el('p', { class: 'dialog-message', text: message }));
      if (body) parts.push(body);
      mount($('#dialogBody'), parts);

      const cancelBtn = el(
        'button',
        { class: 'btn btn--ghost', type: 'button', on: { click: () => closeDialog(null) } },
        document.createTextNode(cancelText),
      );
      const confirmBtn = el(
        'button',
        {
          class: 'btn ' + (danger ? 'btn--danger' : 'btn--primary'),
          type: 'button',
          on: {
            click: () => {
              const value = state.dialog.validate ? state.dialog.validate() : true;
              if (value === null || value === undefined) return;
              closeDialog(value);
            },
          },
        },
        document.createTextNode(confirmText),
      );
      state.dialog.confirmBtn = confirmBtn;
      mount($('#dialogActions'), cancelBtn, confirmBtn);
      openModal('#dialog');
    });
  }

  function closeDialog(value) {
    const resolve = state.dialog.resolve;
    state.dialog.resolve = null;
    state.dialog.validate = null;
    state.dialog.confirmBtn = null;
    closeModal('#dialog');
    if (resolve) resolve(value);
  }

  function confirmDialog(options) {
    return openDialog(Object.assign({ confirmText: '确定' }, options));
  }

  function promptDialog(options) {
    const { title = '请输入', label = '', value = '', placeholder = '', confirmText = '确定', maxLength = 60 } = options || {};
    const input = el('input', {
      class: 'input',
      type: 'text',
      value,
      placeholder,
      maxlength: maxLength,
      autocomplete: 'off',
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (state.dialog.confirmBtn) state.dialog.confirmBtn.click();
      }
    });
    const body = el(
      'div',
      { class: 'dialog-field' },
      label ? el('label', { class: 'dialog-field__label', text: label }) : null,
      input,
    );
    return openDialog({
      title,
      body,
      confirmText,
      validate: () => {
        const text = input.value.trim();
        if (!text) {
          toast('内容不能为空', 'error');
          input.focus();
          return null;
        }
        return text;
      },
    });
  }

  async function withLoading(button, task) {
    if (!button) return task();
    if (button.dataset.loading === '1') return undefined;
    button.dataset.loading = '1';
    button.classList.add('is-loading');
    button.disabled = true;
    try {
      return await task();
    } finally {
      button.dataset.loading = '0';
      button.classList.remove('is-loading');
      button.disabled = false;
    }
  }

  /* ========================================================================
   * 5. 数据归一化
   * ====================================================================== */
  function normalizeQualityLevels(raw) {
    const source = raw && typeof raw === 'object' && Object.keys(raw).length ? raw : QUALITY_FALLBACK;
    const result = {};
    for (const [key, value] of Object.entries(source)) {
      result[key] = typeof value === 'string' ? value : str(value && value.label) || key;
    }
    return result;
  }

  function normalizeSettings(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const modes = Array.isArray(source.play_modes) && source.play_modes.length ? source.play_modes : DEFAULT_MODES;
    return {
      quality: str(source.quality) || 'high',
      qualityLevels: normalizeQualityLevels(source.quality_levels),
      playMode: str(source.play_mode) || 'sequential',
      playModes: modes.slice(),
      quietHours: typeof source.quiet_hours === 'string' ? source.quiet_hours : '',
      quietActive: !!source.quiet_active,
      authRequired: !!source.auth_required,
      version: str(source.version),
    };
  }

  function normalizePlayer(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const position = Number(
      source.position !== undefined ? source.position : source.position_seconds,
    );
    const duration = Number(source.duration !== undefined ? source.duration : source.duration_seconds);
    return {
      state: str(source.state) || 'STOPPED',
      title: str(source.title),
      artist: str(source.artist),
      position: Number.isFinite(position) ? Math.max(0, position) : 0,
      duration: Number.isFinite(duration) ? Math.max(0, duration) : 0,
      positionText: str(source.position_text),
      durationText: str(source.duration_text),
      interrupted: source.interrupted && typeof source.interrupted === 'object' ? source.interrupted : null,
      queue: source.queue && typeof source.queue === 'object' ? source.queue : null,
      active: !!source.active,
      manualStop: !!source.manual_stop,
      volume: Number.isFinite(Number(source.volume)) ? Number(source.volume) : null,
    };
  }

  function normalizeTrack(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      id: source.id,
      songId: source.song_id !== undefined ? source.song_id : source.id,
      name: str(source.name) || str(source.title) || '未知歌曲',
      artist: str(source.artist),
      album: str(source.album),
      pos: Number(source.pos) || 0,
      localId: source.local_id !== undefined ? source.local_id : null,
      duration: Number(source.duration) || 0,
    };
  }

  function normalizeSong(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const artist = Array.isArray(source.artists) ? source.artists.join(' / ') : str(source.artist);
    return {
      songId: source.song_id !== undefined ? source.song_id : source.id,
      name: str(source.name) || str(source.title) || '未知歌曲',
      artist,
      album: str(source.album),
      fee: source.fee,
      duration: Number(source.duration) || 0,
      cover: str(source.cover) || str(source.pic),
    };
  }

  function normalizeSongs(data) {
    const raw = data && typeof data === 'object' ? data : {};
    const list = Array.isArray(raw.songs)
      ? raw.songs
      : Array.isArray(raw.results)
        ? raw.results
        : Array.isArray(raw.tracks)
          ? raw.tracks
          : Array.isArray(raw)
            ? raw
            : [];
    return list.map(normalizeSong);
  }

  function normalizeLocalTrack(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      id: source.id,
      title: str(source.title),
      artist: str(source.artist),
      album: str(source.album),
      size: Number(source.size) || 0,
      duration: Number(source.duration) || 0,
      path: str(source.path),
    };
  }

  function normalizeDownloadJob(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      id: source.id,
      songId: source.song_id,
      name: str(source.name) || '未知曲目',
      artist: str(source.artist),
      status: str(source.status) || 'pending',
      progress: clamp(Number(source.progress) || 0, 0, 100),
      error: str(source.error),
      bytesDone: Number(source.bytes_done) || 0,
      bytesTotal: Number(source.bytes_total) || 0,
      retries: Number(source.retries) || 0,
      attempts: Number(source.attempts) || 0,
    };
  }

  function normalizeDownloadStats(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      pending: Number(source.pending) || 0,
      running: Number(source.running) || 0,
      done: Number(source.done) || 0,
      failed: Number(source.failed) || 0,
      canceled: Number(source.canceled) || 0,
      runningWorkers: Number(source.running_workers) || 0,
    };
  }

  function normalizeQueue(data) {
    if (!data || typeof data !== 'object') return null;
    const tracks = Array.isArray(data.tracks) ? data.tracks.map(normalizeTrack) : [];
    const playlistId = data.playlist_id !== undefined ? data.playlist_id : null;
    if (!tracks.length && playlistId === null) return null;
    return {
      playlistId,
      playlistName: str(data.playlist_name),
      index: Number.isInteger(data.index) ? data.index : -1,
      mode: str(data.mode),
      tracks,
    };
  }

  function normalizeNcmPlaylist(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      id: source.id,
      name: str(source.name) || '未命名歌单',
      count: Number(source.count) || 0,
      cover: str(source.cover),
    };
  }

  function isVipSong(song) {
    const fee = Number(song && song.fee);
    return fee === 1 || fee === 4;
  }

  function modeLabel(mode) {
    return (MODE_META[mode] && MODE_META[mode].label) || str(mode) || '顺序播放';
  }

  function normalizeClock(text) {
    const parts = str(text).split(':');
    if (parts.length !== 2) return '';
    const hours = Number(parts[0]);
    const minutes = Number(parts[1]);
    if (!Number.isFinite(hours) || !Number.isFinite(minutes) || hours > 23 || minutes > 59) return '';
    return pad2(hours) + ':' + pad2(minutes);
  }

  function parseQuietRange(raw) {
    const text = str(raw).trim();
    const match = /^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$/.exec(text);
    if (!match) return null;
    const start = normalizeClock(match[1]);
    const end = normalizeClock(match[2]);
    if (!start || !end) return null;
    return { start, end };
  }

  /* ========================================================================
   * 6. 渲染：外壳 / 连接 / 设备 / 账号
   * ====================================================================== */
  function finishBoot() {
    if (!state.booting) return;
    state.booting = false;
    state.booted = true;
    document.body.classList.remove('booting');
  }

  function setOnline(online) {
    const value = online !== false;
    const changed = state.online !== value;
    state.online = value;
    renderConnection();
    if (value) state.hadOnline = true;
    if (!changed || !state.booted) return;
    if (!value) {
      toast('与中枢的连接已断开，正在自动重试…', 'warn');
    } else if (state.hadOnline) {
      toast('已重新连接到中枢', 'success');
    }
  }

  function renderConnection() {
    const node = $('#conn');
    const text = $('#connText');
    if (!node) return;
    node.classList.remove('conn--online', 'conn--offline', 'conn--pending');
    if (state.online === null) {
      node.classList.add('conn--pending');
      setText(text, '连接中');
      node.title = '正在连接中枢服务';
    } else if (state.online) {
      node.classList.add('conn--online');
      setText(text, '已连接');
      node.title = '中枢服务在线';
    } else {
      node.classList.add('conn--offline');
      setText(text, '已断开');
      node.title = '无法连接中枢服务，请检查后端是否运行';
    }
  }

  function renderDeviceOptions() {
    const select = $('#deviceSelect');
    if (!select) return;
    if (!state.devices.length) {
      mount(select, el('option', { value: '', text: state.online === false ? '设备不可用' : '未发现设备' }));
      select.disabled = true;
      return;
    }
    select.disabled = false;
    const activeIp = state.activeDevice || state.device.ip;
    const signature = state.devices.map((device) => device.ip + ':' + device.name).join('|');
    if (select.dataset.signature !== signature) {
      select.dataset.signature = signature;
      mount(
        select,
        state.devices.map((device) =>
          el('option', { value: device.ip, text: (device.name || '音箱') + ' · ' + device.ip }),
        ),
      );
    }
    if (activeIp) select.value = activeIp;
  }

  function deviceName(ip) {
    const device = state.devices.find((item) => item.ip === ip);
    return device ? device.name || device.ip : ip;
  }

  function renderAccountChip() {
    const avatar = $('#accountAvatar');
    const name = $('#accountName');
    const vip = $('#accountVip');
    if (!avatar || !name) return;
    mount(avatar);
    const account = state.account;
    if (account.loggedIn) {
      const node = avatarNode(account, '');
      while (node.firstChild) avatar.appendChild(node.firstChild);
      setText(name, account.nickname || '网易云用户');
      if (vip) vip.hidden = !account.vip;
      const button = $('#accountBtn');
      if (button) button.title = '网易云账号：' + (account.nickname || '已登录');
    } else {
      avatar.appendChild(icon('user'));
      setText(name, '未登录');
      if (vip) vip.hidden = true;
      const button = $('#accountBtn');
      if (button) button.title = '登录网易云账号';
    }
  }

  /* ========================================================================
   * 7. 渲染：播放器
   * ====================================================================== */
  function renderPlayer() {
    const player = state.player;
    const hasTrack = !!player.title;

    const title = hasTrack ? player.title : '未在播放';
    const titleNode = $('#playerTitle');
    setText(titleNode, title);
    if (titleNode) titleNode.title = title;

    const artist = hasTrack ? player.artist || '未知歌手' : '搜索一首歌，推送到音箱';
    const artistNode = $('#playerArtist');
    setText(artistNode, artist);
    if (artistNode) artistNode.title = hasTrack ? player.artist || '' : '';

    setText($('#playerQueue'), queueSummary(player));
    renderBadge();
    renderCover();
    renderPlayToggle();
    renderProgress();
    renderInterrupt();

    document.title = hasTrack
      ? (player.artist ? player.title + ' · ' + player.artist : player.title) + ' — 小爱音乐中枢'
      : '小爱音乐中枢';
  }

  function queueSummary(player) {
    const queue = player.queue;
    if (queue) {
      const parts = [];
      if (queue.playlist_name) parts.push(queue.playlist_name);
      const total = Number(queue.total) || 0;
      const index = Number.isInteger(queue.index) ? Number(queue.index) + 1 : 0;
      if (total > 0 && index > 0) parts.push(index + '/' + total);
      if (queue.mode) parts.push(modeLabel(queue.mode));
      if (parts.length) return parts.join(' · ');
    }
    if (player.title) return player.active ? '队列自动续播中' : '单曲播放';
    return '';
  }

  function renderBadge() {
    const badge = $('#playerBadge');
    const text = $('#playerBadgeText');
    if (!badge) return;
    let meta;
    if (state.online === false) meta = { text: '离线', tone: 'err' };
    else if (state.device && state.device.reachable === false) meta = { text: '设备离线', tone: 'err' };
    else if (state.player.interrupted) meta = { text: '外部暂停', tone: 'warn' };
    else meta = PLAYER_STATE_META[state.player.state] || { text: state.player.state || '空闲', tone: 'muted' };
    badge.className = 'badge badge--' + meta.tone;
    setText(text, meta.text);
  }

  function renderCover() {
    const cover = $('#cover');
    const charNode = $('#coverChar');
    if (!cover) return;
    const title = state.player.title;
    setCoverVars(cover, title);
    setText(charNode, firstChar(title));
  }

  function renderPlayToggle() {
    const button = $('#playToggleBtn');
    if (!button) return;
    const playing = state.player.state === 'PLAYING' || state.player.state === 'TRANSITIONING';
    setButtonIcon(button, playing ? 'pause' : 'play');
    let label = '播放';
    if (playing) label = '暂停';
    else if (state.player.interrupted) label = '继续播放';
    button.title = label;
    button.setAttribute('aria-label', label);
  }

  function renderProgress() {
    const player = state.player;
    const progress = $('#progress');
    if (!progress) return;

    const duration = Math.max(0, Number(player.duration) || 0);
    const dragging = state.seek.dragging;
    const position = dragging ? state.seek.value : Math.max(0, Number(player.position) || 0);
    const ratio = duration > 0 ? clamp(position / duration, 0, 1) : 0;
    const percent = (ratio * 100).toFixed(3);

    const fill = $('#progressFill');
    const thumb = $('#progressThumb');
    if (fill) fill.style.width = percent + '%';
    if (thumb) thumb.style.left = percent + '%';

    const seekable = duration > 0;
    progress.classList.toggle('is-disabled', !seekable);
    progress.setAttribute('aria-valuemax', String(Math.round(duration)));
    progress.setAttribute('aria-valuenow', String(Math.round(position)));

    const positionText =
      dragging
        ? fmtTime(position)
        : player.positionText && /^\d{1,2}:\d{2}(:\d{2})?$/.test(player.positionText)
          ? player.positionText
          : fmtTime(position);
    const durationText =
      player.durationText && /^\d{1,2}:\d{2}(:\d{2})?$/.test(player.durationText)
        ? player.durationText
        : fmtTime(duration);

    setText($('#timeNow'), positionText);
    setText($('#timeTotal'), durationText);
    progress.setAttribute('aria-valuetext', positionText + ' / ' + durationText);
    setText($('#progressBubble'), fmtTime(dragging ? state.seek.value : position));
  }

  function renderInterrupt() {
    const banner = $('#interruptBanner');
    if (!banner) return;
    const info = state.player.interrupted;
    const key = info
      ? str(info.at) + '|' + str(info.title) + '|' + str(info.position) + '|' + str(info.reason)
      : '';
    const visible = !!info && key !== state.interruptDismissedKey;
    banner.hidden = !visible;
    if (!visible) return;

    const isStop = info.reason === 'external_stop';
    setText(
      $('#interruptTitle'),
      isStop ? '音箱已被语音/其他控制器停止' : '音箱已被语音/其他控制器暂停',
    );
    const parts = [];
    const title = str(info.title) || state.player.title;
    if (title) parts.push(title);
    if (info.artist) parts.push(str(info.artist));
    const position = Number(info.position);
    if (Number.isFinite(position) && position > 0) parts.push('停在 ' + fmtTime(position));
    setText($('#interruptMeta'), parts.join(' · ') || '点击「继续播放」从原位置恢复');
  }

  /* ========================================================================
   * 8. 渲染：音量 / 音质 / 模式
   * ====================================================================== */
  function renderVolumeUI() {
    const value = clamp(state.volume.value, 0, 100);
    const slider = $('#volumeSlider');
    if (slider) {
      if (document.activeElement !== slider) slider.value = String(value);
      slider.style.setProperty('--pct', value + '%');
    }
    setText($('#volumeValue'), String(Math.round(value)));
    const mute = $('#muteBtn');
    setButtonIcon(mute, value === 0 ? 'volume-off' : 'volume');
    if (mute) {
      const label = value === 0 ? '取消静音' : '静音';
      mute.title = label;
      mute.setAttribute('aria-label', label);
    }
  }

  function renderQualitySelects() {
    const levels = state.settings.qualityLevels;
    const value = state.settings.quality;
    const entries = Object.entries(levels);
    const signature = entries.map(([key, label]) => key + ':' + label).join('|');
    ['#qualitySelect', '#settingsQuality'].forEach((selector) => {
      const node = $(selector);
      if (!node) return;
      if (node.dataset.signature !== signature) {
        node.dataset.signature = signature;
        mount(
          node,
          entries.map(([key, label]) => el('option', { value: key, text: label })),
        );
      }
      if (document.activeElement !== node) node.value = value;
    });
  }

  function renderModeGroups() {
    const modes = state.settings.playModes.length ? state.settings.playModes : DEFAULT_MODES;
    const active = state.settings.playMode;
    [
      { selector: '#modeGroup', compact: true },
      { selector: '#settingsModes', compact: false },
    ].forEach(({ selector, compact }) => {
      const node = $(selector);
      if (!node) return;
      const signature = modes.join('|') + (compact ? ':c' : ':w');
      if (node.dataset.signature !== signature) {
        node.dataset.signature = signature;
        mount(
          node,
          modes.map((mode) => {
            const meta = MODE_META[mode] || { label: mode, short: mode, icon: 'order' };
            return el(
              'button',
              {
                class: 'seg-btn',
                type: 'button',
                dataset: { mode },
                role: 'radio',
                title: meta.label,
                'aria-label': meta.label,
              },
              icon(meta.icon),
              el('span', { text: compact ? meta.short : meta.label }),
            );
          }),
        );
      }
      Array.from(node.children).forEach((button) => {
        const isActive = button.dataset.mode === active;
        button.classList.toggle('is-active', isActive);
        button.setAttribute('aria-checked', isActive ? 'true' : 'false');
      });
    });
  }

  function syncVolumeFromState() {
    const volume = state.player.volume;
    if (volume === null || !Number.isFinite(volume)) return;
    if (Date.now() < state.volume.dirtyUntil) return;
    const next = clamp(volume, 0, 100);
    if (next === state.volume.value) return;
    state.volume.value = next;
    renderVolumeUI();
  }

  /* ========================================================================
   * 9. 渲染：标签页
   * ====================================================================== */
  function normalizeTab(tab) {
    return TAB_IDS.indexOf(tab) >= 0 ? tab : 'search';
  }

  function switchTab(tab, options = {}) {
    const next = normalizeTab(tab);
    state.tab = next;
    writeStore(STORAGE.tab, next);

    for (const id of TAB_IDS) {
      const button = $('#tab-' + id);
      const panel = $('#panel-' + id);
      const active = id === next;
      if (button) {
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
        button.tabIndex = active ? 0 : -1;
      }
      if (panel) {
        panel.hidden = !active;
        panel.classList.remove('is-enter');
        if (active && !options.silent) {
          void panel.offsetWidth; // 强制重排，重放淡入动画
          panel.classList.add('is-enter');
        }
      }
    }

    requestAnimationFrame(() => {
      moveTabIndicator();
      scrollActiveTabIntoView();
    });
    if (!options.silent) ensureTabData(next);
  }

  /** 移动端标签栏可横向滚动，切换时把激活项滚入可视区 */
  function scrollActiveTabIntoView() {
    const tabs = $('#tabs');
    const active = $('.tab.is-active');
    if (!tabs || !active || tabs.scrollWidth <= tabs.clientWidth) return;
    const padding = 16;
    const left = active.offsetLeft - padding;
    const right = active.offsetLeft + active.offsetWidth + padding;
    if (tabs.scrollLeft > left) {
      tabs.scrollTo({ left: Math.max(0, left), behavior: 'smooth' });
    } else if (tabs.scrollLeft + tabs.clientWidth < right) {
      tabs.scrollTo({ left: right - tabs.clientWidth, behavior: 'smooth' });
    }
  }

  function moveTabIndicator() {
    const indicator = $('#tabIndicator');
    if (!indicator) return;
    if (window.getComputedStyle(indicator).display === 'none') return;
    const active = $('.tab.is-active');
    if (!active) {
      indicator.style.width = '0px';
      return;
    }
    indicator.style.width = active.offsetWidth + 'px';
    indicator.style.transform = 'translateX(' + active.offsetLeft + 'px)';
  }

  function ensureTabData(tab) {
    if (tab === 'playlists') {
      if (!state.playlists.length) loadPlaylists();
      else {
        renderPlaylistsPanel();
        if (state.currentPlaylistId && !state.tracks.length && !state.tracksLoading) loadTracks({ silent: true });
      }
    } else if (tab === 'queue') {
      loadQueue({ silent: true });
    } else if (tab === 'local') {
      if (!state.local.loaded) loadLocal({ silent: true });
    } else if (tab === 'downloads') {
      loadDownloads();
    } else if (tab === 'settings') {
      if (!state.devices.length) loadDevices({ silent: true });
      renderSettings();
    }
  }

  /* ========================================================================
   * 10. 渲染：搜索
   * ====================================================================== */
  function renderSearchIdle() {
    setText($('#searchHint'), '输入关键词，自动搜索网易云曲库');
    mount(
      $('#searchList'),
      emptyState({
        art: 'empty-search',
        title: '搜索并播放',
        desc: '输入歌名、歌手或专辑，点击「播放」即可推送到当前音箱。',
      }),
    );
  }

  function renderSearchSkeleton() {
    mount($('#searchList'), loadingRows(4));
  }

  function renderSearchError(message) {
    mount(
      $('#searchList'),
      emptyState({
        art: 'empty-wifi',
        title: '搜索失败',
        desc: message || '网络异常，请稍后重试。',
        actionLabel: '重试',
        actionIcon: 'refresh',
        onAction: () => runSearch(),
      }),
    );
  }

  function renderSearchResults(songs) {
    const list = $('#searchList');
    if (!list) return;
    if (!songs.length) {
      mount(
        list,
        emptyState({
          art: 'empty-search',
          title: '没有找到相关歌曲',
          desc: '换个关键词试试，或确认网易云账号状态是否正常。',
        }),
      );
      return;
    }
    const fragment = document.createDocumentFragment();
    songs.forEach((song, index) => {
      const subtitle = [song.artist, song.album].filter(Boolean).join(' · ') || '未知歌手';
      fragment.appendChild(
        el(
          'div',
          { class: 'song', dataset: { index: String(index) } },
          coverNode(song.name, song.cover, 'cover--md'),
          el(
            'div',
            { class: 'song__main' },
            el(
              'div',
              { class: 'song__title' },
              el('span', { class: 'song__name', text: song.name, title: song.name }),
              isVipSong(song) ? el('span', { class: 'tag tag--vip', text: 'VIP' }) : null,
            ),
            el('div', { class: 'song__sub', text: subtitle, title: subtitle }),
          ),
          song.duration > 0 ? el('span', { class: 'song__dur num', text: fmtTime(song.duration) }) : null,
          el(
            'div',
            { class: 'song__actions' },
            el(
              'button',
              {
                class: 'play-btn',
                type: 'button',
                dataset: { action: 'play', index: String(index) },
                title: '播放并推送到音箱',
                'aria-label': '播放并推送到音箱',
              },
              icon('play'),
            ),
            el(
              'button',
              {
                class: 'icon-btn icon-btn--sm',
                type: 'button',
                dataset: { action: 'add', index: String(index) },
                title: '加入歌单',
                'aria-label': '加入歌单',
              },
              icon('plus'),
            ),
            el(
              'button',
              {
                class: 'icon-btn icon-btn--sm',
                type: 'button',
                dataset: { action: 'download', index: String(index) },
                title: '下载到本地曲库',
                'aria-label': '下载',
              },
              icon('download'),
            ),
          ),
        ),
      );
    });
    mount(list, fragment);
  }

  /* ========================================================================
   * 11. 渲染：歌单
   * ====================================================================== */
  function currentPlaylist() {
    return state.playlists.find((item) => String(item.id) === String(state.currentPlaylistId)) || null;
  }

  function playlistNameById(id) {
    const item = state.playlists.find((playlist) => String(playlist.id) === String(id));
    return item ? item.name : '歌单';
  }

  function renderPlaylistSelect() {
    const select = $('#playlistSelect');
    if (!select) return;
    if (!state.playlists.length) {
      mount(select, el('option', { value: '', text: '暂无歌单' }));
      select.disabled = true;
      return;
    }
    select.disabled = false;
    const signature = state.playlists.map((item) => item.id + ':' + item.name + ':' + item.count).join('|');
    if (select.dataset.signature !== signature) {
      select.dataset.signature = signature;
      mount(
        select,
        state.playlists.map((item) =>
          el('option', {
            value: String(item.id),
            text: item.name + '（' + item.count + '）',
          }),
        ),
      );
    }
    if (document.activeElement !== select) select.value = String(state.currentPlaylistId || '');
  }

  function renderTargetSelects() {
    const signature = state.playlists.map((item) => item.id + ':' + item.name).join('|') || 'empty';
    ['#searchTarget', '#localTarget'].forEach((selector) => {
      const node = $(selector);
      if (!node) return;
      if (!state.playlists.length) {
        node.dataset.signature = 'empty';
        mount(node, el('option', { value: '', text: '无歌单' }));
        node.disabled = true;
        return;
      }
      node.disabled = false;
      if (node.dataset.signature !== signature) {
        node.dataset.signature = signature;
        mount(
          node,
          state.playlists.map((item) => el('option', { value: String(item.id), text: item.name })),
        );
      }
      if (document.activeElement !== node) node.value = String(state.targetPlaylistId || state.playlists[0].id);
    });
  }

  function renderPlaylistsPanel() {
    const playlist = currentPlaylist();
    const meta = $('#playlistMeta');
    const playAllBtn = $('#playAllBtn');
    const downloadBtn = $('#downloadWholeBtn');
    const renameBtn = $('#renamePlaylistBtn');
    const deleteBtn = $('#deletePlaylistBtn');
    const downloadLabel = downloadBtn ? downloadBtn.querySelector('span') : null;

    const hasPlaylist = !!playlist;
    if (playAllBtn) playAllBtn.disabled = !hasPlaylist || state.tracks.length === 0;
    if (renameBtn) renameBtn.disabled = !hasPlaylist;
    if (deleteBtn) deleteBtn.disabled = !hasPlaylist;
    if (!playlist) {
      setText(meta, '');
      if (downloadBtn) downloadBtn.disabled = true;
      if (downloadLabel) setText(downloadLabel, '下载整张');
      return;
    }

    const total = state.tracks.length;
    const downloaded = state.tracks.filter((track) => track.localId !== null && track.localId !== undefined).length;
    const remaining = state.tracks.filter(
      (track) => (track.localId === null || track.localId === undefined) && track.songId !== null && track.songId !== undefined,
    ).length;
    setText(meta, total ? '共 ' + total + ' 首 · 已下载 ' + downloaded + ' 首' : '');
    if (downloadBtn) downloadBtn.disabled = remaining === 0;
    if (downloadLabel) setText(downloadLabel, remaining > 0 ? '下载整张（剩 ' + remaining + '）' : '已全部下载');
  }

  function trackIsCurrent(track, index) {
    const queue = state.player.queue;
    if (!queue) return false;
    if (String(queue.playlist_id) !== String(state.currentPlaylistId)) return false;
    return Number(queue.index) === index;
  }

  function buildTrackRow(track, index) {
    const downloaded = track.localId !== null && track.localId !== undefined;
    const row = el('div', {
      class: 'track' + (trackIsCurrent(track, index) ? ' is-current' : ''),
      dataset: { index: String(index), id: String(track.id) },
      draggable: 'true',
    });

    row.appendChild(
      el(
        'button',
        {
          class: 'track__grip',
          type: 'button',
          title: '拖拽排序',
          'aria-label': '拖拽排序',
          tabindex: '-1',
        },
        icon('grip'),
      ),
    );
    row.appendChild(el('span', { class: 'track__idx num', text: pad2(index + 1) }));
    row.appendChild(
      el(
        'div',
        { class: 'track__main' },
        el(
          'div',
          { class: 'track__title' },
          el('span', { class: 'track__name', text: track.name, title: track.name }),
          downloaded ? el('span', { class: 'tag tag--done', text: '已下载' }) : null,
        ),
        el('div', {
          class: 'track__sub',
          text: [track.artist, track.album].filter(Boolean).join(' · ') || '未知歌手',
        }),
      ),
    );
    row.appendChild(
      el('span', {
        class: 'track__dur num',
        text: track.duration > 0 ? fmtTime(track.duration) : '—',
      }),
    );
    row.appendChild(
      el(
        'div',
        { class: 'track__actions' },
        el(
          'button',
          {
            class: 'icon-btn icon-btn--sm',
            type: 'button',
            dataset: { action: 'play' },
            title: '播放这首',
            'aria-label': '播放这首',
          },
          icon('play'),
        ),
        downloaded || track.songId === null || track.songId === undefined
          ? null
          : el(
              'button',
              {
                class: 'icon-btn icon-btn--sm',
                type: 'button',
                dataset: { action: 'download' },
                title: '下载这首',
                'aria-label': '下载这首',
              },
              icon('download'),
            ),
        el(
          'button',
          {
            class: 'icon-btn icon-btn--sm icon-btn--danger',
            type: 'button',
            dataset: { action: 'remove' },
            title: '从歌单移除',
            'aria-label': '从歌单移除',
          },
          icon('close'),
        ),
      ),
    );
    row.appendChild(
      el(
        'div',
        { class: 'track__move' },
        el(
          'button',
          {
            class: 'track__move-btn',
            type: 'button',
            dataset: { action: 'moveup' },
            title: '上移',
            'aria-label': '上移',
            disabled: index === 0,
          },
          icon('arrow-up'),
        ),
        el(
          'button',
          {
            class: 'track__move-btn',
            type: 'button',
            dataset: { action: 'movedown' },
            title: '下移',
            'aria-label': '下移',
            disabled: index === state.tracks.length - 1,
          },
          icon('arrow-down'),
        ),
      ),
    );
    return row;
  }

  function renderTracks() {
    const holder = $('#tracksList');
    if (!holder) return;

    if (!state.playlists.length) {
      mount(
        holder,
        emptyState({
          art: 'empty-music',
          title: '还没有歌单',
          desc: '新建一个歌单，把喜欢的歌收进来；登录网易云后也可以一键导入云端歌单。',
          actionLabel: '新建歌单',
          actionIcon: 'plus',
          onAction: () => createPlaylist(),
        }),
      );
      renderPlaylistsPanel();
      return;
    }

    if (state.tracksLoading && !state.tracks.length) {
      mount(holder, loadingRows(5));
      renderPlaylistsPanel();
      return;
    }

    if (!state.tracks.length) {
      mount(
        holder,
        emptyState({
          art: 'empty-music',
          title: '这个歌单还是空的',
          desc: '去「搜索」页找到喜欢的歌，点击行尾的加号加入当前歌单。',
        }),
      );
      renderPlaylistsPanel();
      return;
    }

    const fragment = document.createDocumentFragment();
    state.tracks.forEach((track, index) => fragment.appendChild(buildTrackRow(track, index)));
    mount(holder, fragment);
    renderPlaylistsPanel();
  }

  function renderNeteaseLoading() {
    mount($('#neteaseList'), loadingRows(3));
  }

  function renderNeteaseError(error) {
    const holder = $('#neteaseList');
    if (!holder) return;
    const status = error && error.status;
    if (status === 401) {
      mount(
        holder,
        emptyState({
          art: 'empty-qr',
          title: '需要登录网易云',
          desc: '登录后才能读取云端歌单并导入到本地。',
          actionLabel: '扫码登录',
          actionIcon: 'qr',
          onAction: () => openAccountModal(),
        }),
      );
      return;
    }
    mount(
      holder,
      emptyState({
        art: 'empty-wifi',
        title: '网易云歌单读取失败',
        desc: (error && error.message) || '可能是网络或上游接口异常，请稍后重试。',
        actionLabel: '重试',
        actionIcon: 'refresh',
        onAction: () => loadNeteasePlaylists(),
      }),
    );
  }

  function renderNeteaseList() {
    const holder = $('#neteaseList');
    if (!holder) return;
    const playlists = state.netease.playlists;
    if (!playlists.length) {
      mount(
        holder,
        emptyState({
          art: 'empty-cloud',
          title: '没有可导入的歌单',
          desc: '登录网易云账号后，这里会列出云端歌单。',
        }),
      );
      return;
    }
    const fragment = document.createDocumentFragment();
    playlists.forEach((playlist) => {
      const local = state.playlists.find((item) => String(item.remoteId) === String(playlist.id));
      const button = el(
        'button',
        {
          class: 'btn btn--sm ' + (local ? 'btn--ghost' : 'btn--primary'),
          type: 'button',
          dataset: {
            action: local ? 'sync' : 'import',
            remote: String(playlist.id),
            local: local ? String(local.id) : '',
            name: playlist.name,
          },
        },
        icon(local ? 'refresh' : 'download'),
        el('span', { text: local ? '同步' : '导入' }),
      );
      fragment.appendChild(
        el(
          'div',
          { class: 'ncm-row' },
          coverNode(playlist.name, playlist.cover, 'cover--sm'),
          el(
            'div',
            { class: 'ncm-row__main' },
            el('span', { class: 'ncm-row__name', text: playlist.name, title: playlist.name }),
            el('span', {
              class: 'ncm-row__sub',
              text: playlist.count + ' 首' + (local ? ' · 已导入' : ''),
            }),
          ),
          button,
        ),
      );
    });
    mount(holder, fragment);
  }

  function showSyncProgress(visible) {
    const box = $('#syncProgress');
    if (box) box.hidden = !visible;
  }

  function renderSyncProgress(data) {
    const fill = $('#syncProgressFill');
    const text = $('#syncProgressText');
    if (!data || typeof data !== 'object') return;
    const done = Number(data.done) || 0;
    const total = Number(data.total) || 0;
    const added = Number(data.added) || 0;
    const errors = Array.isArray(data.errors) ? data.errors.length : 0;
    const percent = total > 0 ? clamp((done / total) * 100, 0, 100) : 0;
    if (fill) fill.style.width = percent + '%';
    if (text) {
      const parts = [
        data.running ? '同步中' : '同步完成',
        done + '/' + total,
        '新增 ' + added + ' 首',
      ];
      if (data.current) parts.push(data.current);
      if (errors > 0) parts.push(errors + ' 个错误');
      setText(text, parts.join(' · '));
    }
  }

  /* ========================================================================
   * 12. 渲染：队列 / 本地 / 下载
   * ====================================================================== */
  function renderQueue() {
    const holder = $('#queueList');
    const meta = $('#queueMeta');
    if (!holder) return;
    const queue = state.queue;
    if (!queue || !queue.tracks.length) {
      setText(meta, '');
      mount(
        holder,
        emptyState({
          art: 'empty-queue',
          title: '当前没有播放队列',
          desc: '在歌单页点击「播放整张」，这里就会显示队列与当前播放位置。',
        }),
      );
      return;
    }
    const parts = [];
    if (queue.playlistName) parts.push('「' + queue.playlistName + '」');
    if (queue.index >= 0) parts.push('第 ' + (queue.index + 1) + ' / ' + queue.tracks.length + ' 首');
    if (queue.mode) parts.push(modeLabel(queue.mode));
    setText(meta, parts.join(' · '));

    const fragment = document.createDocumentFragment();
    queue.tracks.forEach((track, index) => {
      const current = index === queue.index;
      fragment.appendChild(
        el(
          'div',
          {
            class: 'track track--clickable' + (current ? ' is-current' : ''),
            dataset: { index: String(index) },
            role: 'button',
            tabindex: '0',
            title: '点击播放这首',
          },
          el('span', { class: 'track__idx num', text: pad2(index + 1) }),
          el(
            'div',
            { class: 'track__main' },
            el(
              'div',
              { class: 'track__title' },
              el('span', { class: 'track__name', text: track.name, title: track.name }),
              current ? el('span', { class: 'tag tag--done', text: '正在播放' }) : null,
              track.localId !== null && track.localId !== undefined && !current
                ? el('span', { class: 'tag tag--done', text: '本地' })
                : null,
            ),
            el('div', {
              class: 'track__sub',
              text: [track.artist, track.album].filter(Boolean).join(' · ') || '未知歌手',
            }),
          ),
          el('span', {
            class: 'track__dur num',
            text: track.duration > 0 ? fmtTime(track.duration) : '—',
          }),
          el('div', { class: 'track__actions' }, icon('chevron-right')),
        ),
      );
    });
    mount(holder, fragment);
  }

  function renderLocal() {
    const holder = $('#localList');
    const meta = $('#localMeta');
    if (!holder) return;
    const tracks = state.local.tracks;
    const query = str(state.local.q).trim();
    setText(
      meta,
      tracks.length
        ? '共 ' + tracks.length + ' 首' + (query ? '（搜索：' + query + '）' : '')
        : query
          ? '没有匹配「' + query + '」的本地曲目'
          : '',
    );

    if (!tracks.length) {
      mount(
        holder,
        emptyState({
          art: 'empty-folder',
          title: query ? '没有匹配的曲目' : '本地曲库还是空的',
          desc: query
            ? '换个关键词试试，或清空搜索查看全部曲目。'
            : '把音乐文件放进曲库目录，然后点击「扫描目录」建立索引。已下载的歌曲会自动入库。',
          actionLabel: query ? '' : '扫描目录',
          actionIcon: 'scan',
          onAction: () => scanLocal(),
        }),
      );
      return;
    }

    const fragment = document.createDocumentFragment();
    tracks.forEach((track, index) => {
      const subtitle = [
        track.artist || '未知歌手',
        fmtSize(track.size),
        track.album,
      ].filter(Boolean).join(' · ');
      fragment.appendChild(
        el(
          'div',
          { class: 'song', dataset: { index: String(index) } },
          coverNode(track.title, '', 'cover--sm'),
          el(
            'div',
            { class: 'song__main' },
            el(
              'div',
              { class: 'song__title' },
              el('span', { class: 'song__name', text: track.title || '未知曲目', title: track.title }),
            ),
            el('div', { class: 'song__sub', text: subtitle, title: subtitle }),
          ),
          el(
            'div',
            { class: 'song__actions' },
            el(
              'button',
              {
                class: 'play-btn',
                type: 'button',
                dataset: { action: 'play', index: String(index) },
                title: '播放这首本地曲目',
                'aria-label': '播放这首本地曲目',
              },
              icon('play'),
            ),
            el(
              'button',
              {
                class: 'icon-btn icon-btn--sm',
                type: 'button',
                dataset: { action: 'add', index: String(index) },
                title: '加入歌单',
                'aria-label': '加入歌单',
              },
              icon('plus'),
            ),
          ),
        ),
      );
    });
    mount(holder, fragment);
  }

  function renderDownloadStats() {
    const stats = state.downloadStats || {};
    setText($('#statPending'), String(Number(stats.pending) || 0));
    setText($('#statRunning'), String(Number(stats.running) || 0));
    setText($('#statDone'), String(Number(stats.done) || 0));
    setText($('#statFailed'), String(Number(stats.failed) || 0));

    const active = (Number(stats.pending) || 0) + (Number(stats.running) || 0);
    const meta = $('#downloadMeta');
    if (meta) {
      const total = state.downloads.jobs.length;
      setText(meta, total ? '共 ' + total + ' 个任务' + (active ? ' · 进行中 ' + active : '') : '');
    }
    const retryAll = $('#downloadRetryAllBtn');
    if (retryAll) retryAll.disabled = (Number(stats.failed) || 0) === 0;
  }

  function renderDownloads() {
    const holder = $('#downloadList');
    if (!holder) return;
    renderDownloadStats();
    const jobs = state.downloads.jobs;
    if (!jobs.length) {
      mount(
        holder,
        emptyState({
          art: 'empty-download',
          title: '下载队列是空的',
          desc: '在搜索结果或歌单里点击下载按钮，任务会出现在这里并自动开始。',
        }),
      );
      return;
    }

    const fragment = document.createDocumentFragment();
    jobs.forEach((job) => {
      const meta = DOWNLOAD_META[job.status] || { text: job.status, tone: 'muted' };
      const isActive = job.status === 'pending' || job.status === 'running';
      const percent = job.status === 'done' ? 100 : job.progress;
      const subtitleParts = [job.artist || '未知歌手'];
      if (job.status === 'running' || job.status === 'pending') subtitleParts.push(Math.round(percent) + '%');
      if (job.retries > 0) subtitleParts.push('重试 ' + job.retries + ' 次');

      const actions = [];
      if (job.status === 'failed') {
        actions.push(
          el(
            'button',
            {
              class: 'icon-btn icon-btn--sm',
              type: 'button',
              dataset: { action: 'retry' },
              title: '重试下载',
              'aria-label': '重试下载',
            },
            icon('refresh'),
          ),
        );
      }
      if (isActive) {
        actions.push(
          el(
            'button',
            {
              class: 'icon-btn icon-btn--sm icon-btn--danger',
              type: 'button',
              dataset: { action: 'cancel' },
              title: '取消下载',
              'aria-label': '取消下载',
            },
            icon('close'),
          ),
        );
      }

      let footLeftText = '';
      if (job.status === 'running' || job.status === 'pending') footLeftText = Math.round(percent) + '%';
      else if (job.status === 'done') footLeftText = '已完成';
      else if (job.status === 'canceled') footLeftText = '已取消';
      else if (job.status === 'failed') footLeftText = job.retries > 0 ? '已重试 ' + job.retries + ' 次' : '等待重试';
      const footLeft = el('span', { text: footLeftText });
      const footRight = job.error
        ? el('span', { class: 'download__error', text: job.error, title: job.error })
        : el('span', { text: job.bytesTotal > 0 ? fmtSize(job.bytesDone) + ' / ' + fmtSize(job.bytesTotal) : '' });

      fragment.appendChild(
        el(
          'div',
          { class: 'download is-' + job.status, dataset: { id: String(job.id) } },
          el(
            'div',
            { class: 'download__top' },
            el(
              'div',
              { class: 'download__main' },
              el('span', { class: 'download__title', text: job.name, title: job.name }),
              el('span', { class: 'download__sub', text: subtitleParts.join(' · ') }),
            ),
            el('span', { class: 'chip chip--' + meta.tone, text: meta.text }),
            actions.length ? el('div', { class: 'download__actions' }, actions) : null,
          ),
          el(
            'div',
            { class: 'download__bar' },
            el('div', { class: 'download__fill', style: { width: clamp(percent, 0, 100) + '%' } }),
          ),
          el('div', { class: 'download__foot' }, footLeft, footRight),
        ),
      );
    });
    mount(holder, fragment);
  }

  /* ========================================================================
   * 13. 渲染：设置
   * ====================================================================== */
  function renderQuiet() {
    const settings = state.settings;
    const enabled = !!settings.quietHours;
    const toggle = $('#quietToggle');
    if (toggle) {
      toggle.classList.toggle('is-on', enabled);
      toggle.setAttribute('aria-checked', enabled ? 'true' : 'false');
    }
    const times = $('#quietTimes');
    if (times) times.classList.toggle('is-disabled', !enabled);
    ['#quietStart', '#quietEnd'].forEach((selector) => {
      const input = $(selector);
      if (input) input.disabled = !enabled;
    });
    const range = parseQuietRange(settings.quietHours);
    if (range) {
      const start = $('#quietStart');
      const end = $('#quietEnd');
      if (start && document.activeElement !== start) start.value = range.start;
      if (end && document.activeElement !== end) end.value = range.end;
    }
    setText(
      $('#quietHint'),
      enabled
        ? settings.quietActive
          ? '免打扰生效中（' + settings.quietHours + '）'
          : '已设定时段 ' + settings.quietHours + '，当前未生效'
        : '当前未启用',
    );
  }

  function renderSettingsDevices() {
    const holder = $('#settingsDeviceList');
    if (!holder) return;
    if (!state.devices.length) {
      mount(holder, el('p', { class: 'hint', text: '尚未发现设备，请点击下方「扫描」或检查音箱网络。' }));
      return;
    }
    const signature = state.devices.map((device) => device.ip + ':' + device.name).join('|') + '#' + state.activeDevice;
    if (holder.dataset.signature === signature) return;
    holder.dataset.signature = signature;
    mount(
      holder,
      state.devices.map((device) =>
        el(
          'button',
          {
            class: 'device-item' + (device.ip === state.activeDevice ? ' is-active' : ''),
            type: 'button',
            dataset: { ip: device.ip },
            title: device.model ? device.name + ' · ' + device.model : device.name,
          },
          el('span', { class: 'device-item__icon' }, icon('device')),
          el(
            'span',
            { class: 'device-item__main' },
            el('span', { class: 'device-item__name', text: device.name }),
            el('span', {
              class: 'device-item__ip',
              text: device.ip + (device.model ? ' · ' + device.model : ''),
            }),
          ),
          icon('check', 'icon device-item__check'),
        ),
      ),
    );
  }

  function renderSettingsAccount() {
    const holder = $('#settingsAccount');
    if (!holder) return;
    const account = state.account;
    if (!account.loggedIn) {
      mount(
        holder,
        el(
          'div',
          { class: 'account-panel' },
          el('span', { class: 'account-panel__avatar', 'aria-hidden': 'true' }, icon('user')),
          el(
            'div',
            { class: 'account-panel__main' },
            el('span', { class: 'account-panel__name', text: '未登录' }),
            el('span', { class: 'account-panel__sub', text: '登录后可导入网易云歌单、播放 VIP 曲目' }),
          ),
          el(
            'div',
            { class: 'account-panel__actions' },
            el(
              'button',
              { class: 'btn btn--primary btn--sm', type: 'button', dataset: { action: 'account-login' } },
              icon('qr'),
              el('span', { text: '扫码登录' }),
            ),
          ),
        ),
      );
      return;
    }
    mount(
      holder,
      el(
        'div',
        { class: 'account-panel' },
        avatarNode(account, 'account-panel__avatar'),
        el(
          'div',
          { class: 'account-panel__main' },
          el(
            'span',
            { class: 'account-panel__name' },
            el('span', { text: account.nickname || '网易云用户' }),
            account.vip ? el('span', { class: 'tag tag--vip', text: 'VIP' }) : null,
          ),
          el('span', {
            class: 'account-panel__sub',
            text: account.uid ? 'UID ' + account.uid : '已登录网易云',
          }),
        ),
        el(
          'div',
          { class: 'account-panel__actions' },
          el(
            'button',
            { class: 'btn btn--ghost btn--sm', type: 'button', dataset: { action: 'account-refresh' } },
            icon('refresh'),
            el('span', { text: '刷新' }),
          ),
          el(
            'button',
            { class: 'btn btn--sm', type: 'button', dataset: { action: 'account-logout' } },
            icon('logout'),
            el('span', { text: '退出登录' }),
          ),
        ),
      ),
    );
  }

  function renderServerInfo() {
    setText($('#serverVersion'), state.server.version || state.settings.version || '—');
    const lanIp = str(state.server.lan_ip);
    const port = state.server.port;
    const address = lanIp ? lanIp + (port ? ':' + port : '') : '';
    setText($('#serverAddress'), address || (window.location.host || '—'));
    const musicDir = str(state.server.music_dir);
    const musicNode = $('#serverMusicDir');
    setText(musicNode, musicDir || '—');
    if (musicNode && musicDir) musicNode.title = musicDir;
    const device = state.device;
    const deviceNode = $('#serverDevice');
    setText(deviceNode, device.ip ? (device.name || '音箱') + ' · ' + device.ip : '未选择');
    if (deviceNode && device.ip) deviceNode.title = device.ip;

    const version = state.server.version || state.settings.version;
    setText($('#footVersion'), version ? 'v' + version : '');
  }

  function renderSettings() {
    renderQualitySelects();
    renderModeGroups();
    renderQuiet();
    renderSettingsDevices();
    renderSettingsAccount();
    renderServerInfo();
  }

  /* ========================================================================
   * 14. 加载：/state 主循环与各区域数据
   * ====================================================================== */
  function applyState(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    state.snapshot = snapshot;
    state.server = snapshot.server && typeof snapshot.server === 'object' ? snapshot.server : {};
    state.device = {
      ip: str(state.server.ip || (snapshot.device && snapshot.device.ip)) || state.device.ip,
      name: str(snapshot.device && snapshot.device.name) || '音箱',
      reachable: !(snapshot.device && snapshot.device.reachable === false),
      model: str(snapshot.device && snapshot.device.model),
    };
    state.activeDevice = str(snapshot.device && snapshot.device.ip) || state.activeDevice;

    const normalized = normalizeSettings(snapshot.settings);
    if (Date.now() > state.settingsDirtyUntil) {
      state.settings = normalized;
    } else {
      state.settings.quietActive = normalized.quietActive;
      state.settings.authRequired = normalized.authRequired;
      state.settings.version = normalized.version;
    }

    state.player = normalizePlayer(snapshot.player);
    state.downloadStats = Object.assign(
      normalizeDownloadStats(state.downloadStats),
      normalizeDownloadStats(snapshot.download),
    );

    const account = snapshot.account && typeof snapshot.account === 'object' ? snapshot.account : null;
    if (account) {
      const loggedIn = !!account.logged_in;
      if (loggedIn !== state.account.loggedIn) {
        if (loggedIn) {
          state.account.loggedIn = true;
          state.account.nickname = str(account.nickname) || state.account.nickname;
          state.account.uid = account.uid !== undefined ? account.uid : state.account.uid;
          state.account.vip = !!account.vip;
          loadAccount();
          loadPlaylists({ silent: true });
        } else {
          state.account = { loggedIn: false, nickname: '', uid: null, avatar: '', vip: false, reason: '' };
        }
        renderAccountChip();
        renderSettingsAccount();
      } else if (loggedIn) {
        if (str(account.nickname) && str(account.nickname) !== state.account.nickname) {
          state.account.nickname = str(account.nickname);
          renderAccountChip();
        }
        state.account.vip = !!account.vip;
      }
    }

    renderPlayer();
    renderConnection();
    syncVolumeFromState();
    if (Date.now() > state.settingsDirtyUntil) {
      renderQualitySelects();
      renderModeGroups();
    }
    renderServerInfo();
    renderDeviceOptions();
    renderSettingsDevices();
    updateDownloadBadge();
    syncDownloadPoll();

    if (state.tab === 'settings') renderQuiet();
    if (state.tab === 'queue') {
      state.timers.queueCounter += 1;
      if (state.timers.queueCounter % 4 === 0) loadQueue({ silent: true });
    }
  }

  function scheduleStatePoll() {
    clearTimeout(state.timers.state);
    state.timers.state = setTimeout(() => {
      if (document.hidden) return;
      pollState();
    }, POLL_STATE_MS);
  }

  async function pollState() {
    if (state.polling) return;
    state.polling = true;
    clearTimeout(state.timers.state);
    try {
      const data = await api('/state', { silent: true, timeout: 8000, retry: true });
      applyState(data);
      setOnline(true);
      finishBoot();
    } catch (error) {
      if (!(error && error.status === 401)) setOnline(false);
      finishBoot();
    } finally {
      state.polling = false;
      scheduleStatePoll();
    }
  }

  async function loadDevices(options = {}) {
    try {
      const data = await api('/devices', { silent: true, timeout: 12000 });
      const raw = Array.isArray(data.devices) ? data.devices : [];
      state.devices = raw.map((device) => ({
        ip: str(device.ip),
        name: str(device.name) || '小爱音箱',
        model: str(device.model),
        udn: str(device.udn),
        lastSeen: Number(device.last_seen) || 0,
        active: !!device.active,
      }));
      state.activeDevice =
        str(data.active) ||
        (state.devices.find((device) => device.active) || {}).ip ||
        state.activeDevice;
      renderDeviceOptions();
      renderSettingsDevices();
    } catch (error) {
      renderDeviceOptions();
      if (!options.silent) toastApiError(error, '设备列表加载失败');
    }
  }

  async function loadAccount() {
    try {
      const data = await api('/account', { silent: true, timeout: 12000 });
      const loggedIn = !!(data && data.logged_in);
      state.account = {
        loggedIn,
        nickname: str(data && data.nickname),
        uid: data && data.uid !== undefined ? data.uid : null,
        avatar: str(data && data.avatar),
        vip: !!(data && data.vip),
        reason: str(data && data.reason),
      };
    } catch (error) {
      const data = error && error.data;
      if (data && data.logged_in === false) {
        state.account = {
          loggedIn: false,
          nickname: '',
          uid: null,
          avatar: '',
          vip: false,
          reason: str(data.reason),
        };
      } else if (error && error.status !== 401) {
        return; // 网络异常时保留上一次状态
      }
    }
    renderAccountChip();
    renderSettingsAccount();
  }

  async function loadPlaylists(options = {}) {
    try {
      const data = await api('/playlists', { silent: true, timeout: 12000 });
      const raw = Array.isArray(data.playlists) ? data.playlists : [];
      state.playlists = raw.map((playlist) => ({
        id: playlist.id,
        name: str(playlist.name) || '未命名歌单',
        source: str(playlist.source),
        remoteId: playlist.remote_id !== undefined ? playlist.remote_id : null,
        ownerUid: playlist.owner_uid !== undefined ? playlist.owner_uid : null,
        count: Number(playlist.count) || 0,
        position: Number(playlist.position) || 0,
      }));

      const available = state.playlists.map((playlist) => String(playlist.id));
      const stored = readStore(STORAGE.playlist, '');
      if (available.indexOf(String(state.currentPlaylistId)) < 0) {
        state.currentPlaylistId = available.indexOf(stored) >= 0 ? stored : available[0] || '';
      }
      if (available.length) writeStore(STORAGE.playlist, state.currentPlaylistId);
      const storedTarget = String(state.targetPlaylistId || readStore(STORAGE.target, ''));
      state.targetPlaylistId = available.indexOf(storedTarget) >= 0 ? storedTarget : state.currentPlaylistId;

      renderPlaylistSelect();
      renderTargetSelects();
      if (state.tab === 'playlists') renderPlaylistsPanel();

      if (state.currentPlaylistId && !options.skipTracks) {
        await loadTracks({ silent: options.silent });
      } else if (!state.currentPlaylistId) {
        state.tracks = [];
        renderTracks();
      }
    } catch (error) {
      if (!options.silent) toastApiError(error, '歌单加载失败');
      renderTracks();
    }
  }

  async function loadTracks(options = {}) {
    const playlistId = state.currentPlaylistId;
    if (!playlistId) {
      state.tracks = [];
      state.tracksLoading = false;
      renderTracks();
      return;
    }
    state.tracksLoading = true;
    if (!options.silent) renderTracks();
    try {
      const data = await api('/playlist', { params: { id: playlistId }, silent: true, timeout: 15000 });
      state.tracks = (Array.isArray(data.tracks) ? data.tracks : []).map(normalizeTrack);
    } catch (error) {
      state.tracks = [];
      if (!options.silent) toastApiError(error, '曲目加载失败');
    } finally {
      state.tracksLoading = false;
      renderTracks();
      syncPlaylistCount();
    }
  }

  function syncPlaylistCount() {
    const playlist = currentPlaylist();
    if (!playlist) return;
    if (playlist.count !== state.tracks.length && state.tracks.length) {
      playlist.count = state.tracks.length;
      renderPlaylistSelect();
    }
  }

  async function loadQueue(options = {}) {
    try {
      const data = await api('/queue', { silent: true, timeout: 12000 });
      state.queue = normalizeQueue(data);
      renderQueue();
    } catch (error) {
      state.queue = null;
      renderQueue();
      if (!options.silent && error && error.status !== 409) toastApiError(error, '队列加载失败');
    }
  }

  async function loadLocal(options = {}) {
    const query = str(state.local.q).trim();
    const seq = ++state.local.seq;
    state.local.loading = true;
    if (!options.silent) renderLocalLoading();
    try {
      const data = await api('/local/tracks', {
        params: { q: query, limit: 200 },
        silent: true,
        timeout: 20000,
      });
      if (seq !== state.local.seq) return;
      state.local.tracks = (Array.isArray(data.tracks) ? data.tracks : []).map(normalizeLocalTrack);
      state.local.loaded = true;
    } catch (error) {
      if (seq !== state.local.seq) return;
      state.local.loaded = true;
      state.local.tracks = [];
      if (!options.silent) toastApiError(error, '本地曲库加载失败');
    } finally {
      if (seq === state.local.seq) {
        state.local.loading = false;
        renderLocal();
      }
    }
  }

  function renderLocalLoading() {
    mount($('#localList'), loadingRows(4));
  }

  async function loadDownloads(options = {}) {
    state.downloads.loading = true;
    try {
      const data = await api('/download/jobs', {
        params: { limit: 100 },
        silent: true,
        timeout: 12000,
      });
      const raw = Array.isArray(data.jobs) ? data.jobs : [];
      state.downloads.jobs = raw
        .map(normalizeDownloadJob)
        .sort((left, right) => {
          const leftOrder = DOWNLOAD_ORDER[left.status] !== undefined ? DOWNLOAD_ORDER[left.status] : 9;
          const rightOrder = DOWNLOAD_ORDER[right.status] !== undefined ? DOWNLOAD_ORDER[right.status] : 9;
          if (leftOrder !== rightOrder) return leftOrder - rightOrder;
          return Number(right.id) - Number(left.id);
        });
      state.downloadStats = Object.assign(
        normalizeDownloadStats(state.downloadStats),
        normalizeDownloadStats(data.stats),
      );
      state.downloads.errorAt = 0;
      updateDownloadBadge();
      if (state.tab === 'downloads') renderDownloads();
      syncDownloadPoll();
    } catch (error) {
      const now = Date.now();
      if (state.tab === 'downloads' && now - state.downloads.errorAt > 10000) {
        state.downloads.errorAt = now;
        toastApiError(error, '下载任务加载失败');
      }
    } finally {
      state.downloads.loading = false;
    }
  }

  async function loadNeteasePlaylists() {
    state.netease.loading = true;
    renderNeteaseLoading();
    try {
      const data = await api('/netease/playlists', { silent: true, timeout: 20000 });
      const raw = Array.isArray(data.playlists) ? data.playlists : [];
      state.netease.playlists = raw.map(normalizeNcmPlaylist);
      renderNeteaseList();
    } catch (error) {
      state.netease.playlists = [];
      renderNeteaseError(error);
    } finally {
      state.netease.loading = false;
    }
  }

  /* ========================================================================
   * 15. 轮询：下载 / 同步
   * ====================================================================== */
  function updateDownloadBadge() {
    const badge = $('#downloadBadge');
    if (!badge) return;
    const stats = state.downloadStats || {};
    const active = (Number(stats.pending) || 0) + (Number(stats.running) || 0);
    if (active > 0) {
      badge.hidden = false;
      setText(badge, active > 99 ? '99+' : String(active));
    } else {
      badge.hidden = true;
    }
  }

  function hasActiveDownloads() {
    const stats = state.downloadStats || {};
    return (Number(stats.pending) || 0) + (Number(stats.running) || 0) > 0;
  }

  function syncDownloadPoll() {
    if (hasActiveDownloads()) {
      if (!state.timers.download) {
        state.timers.download = setInterval(() => {
          if (document.hidden) return;
          loadDownloads();
        }, POLL_DOWNLOAD_MS);
      }
    } else if (state.timers.download) {
      clearInterval(state.timers.download);
      state.timers.download = 0;
    }
  }

  function startSyncPoll() {
    stopSyncPoll();
    state.netease.syncing = true;
    state.netease.timer = setInterval(async () => {
      if (document.hidden) return;
      try {
        const data = await api('/netease/sync_all/status', { silent: true, timeout: 10000 });
        renderSyncProgress(data);
        if (!data || !data.running) {
          stopSyncPoll();
          state.netease.syncing = false;
          toast('网易云歌单同步完成', 'success');
          setTimeout(() => {
            showSyncProgress(false);
            loadPlaylists({ silent: true });
            if (state.netease.open) loadNeteasePlaylists();
          }, 1200);
        }
      } catch (error) {
        /* 轮询失败忽略，下一轮继续 */
      }
    }, 1500);
  }

  function stopSyncPoll() {
    if (state.netease.timer) {
      clearInterval(state.netease.timer);
      state.netease.timer = 0;
    }
  }

  /* ========================================================================
   * 16. 动作：播放控制
   * ====================================================================== */
  async function playerAction(action, options = {}) {
    const { message = '', silent = false } = options;
    try {
      const data = await api('/' + action, { method: 'POST', silent: true, timeout: 20000 });
      if (message) toast(message, 'success');
      return data;
    } catch (error) {
      if (!silent) toastApiError(error, '操作失败');
      return null;
    } finally {
      setTimeout(pollState, 500);
      if (state.tab === 'queue') setTimeout(() => loadQueue({ silent: true }), 900);
    }
  }

  function togglePlay() {
    const player = state.player;
    if (player.interrupted) {
      resumePlayback();
      return;
    }
    if (player.state === 'PLAYING' || player.state === 'TRANSITIONING') {
      playerAction('pause');
    } else if (player.title || player.state === 'PAUSED_PLAYBACK') {
      playerAction('resume');
    } else {
      toast('还没有可播放的曲目，先去搜索一首吧', 'info');
    }
  }

  async function resumePlayback() {
    try {
      const data = await api('/resume', { method: 'POST', silent: true, timeout: 20000 });
      state.interruptDismissedKey = '';
      toast(data && data.resumed === 'replay' ? '已重新推流并恢复到原位置' : '已继续播放', 'success');
      setTimeout(pollState, 700);
    } catch (error) {
      toastApiError(error, '继续播放失败');
    }
  }

  async function seekTo(seconds) {
    const player = state.player;
    const duration = Number(player.duration) || 0;
    const target = Math.max(0, Math.min(Number(seconds) || 0, duration > 0 ? duration : Number(seconds) || 0));
    player.position = target;
    state.seek.value = target;
    renderProgress();
    try {
      await api('/seek', { method: 'POST', params: { pos: target.toFixed(2) }, silent: true, timeout: 12000 });
    } catch (error) {
      toastApiError(error, '跳转失败');
    }
    setTimeout(pollState, 400);
  }

  function seekBy(delta) {
    if (!(state.player.duration > 0)) return;
    const base = state.seek.dragging ? state.seek.value : state.player.position;
    seekTo(base + delta);
  }

  function currentProgressSeconds() {
    return state.seek.dragging ? state.seek.value : state.player.position;
  }

  /* ========================================================================
   * 17. 动作：音量 / 音质 / 模式
   * ====================================================================== */
  function setVolume(value) {
    const next = clamp(Math.round(value), 0, 100);
    state.volume.value = next;
    state.volume.dirtyUntil = Date.now() + 1500;
    renderVolumeUI();
    clearTimeout(state.volume.timer);
    state.volume.timer = setTimeout(() => {
      state.volume.timer = 0;
      commitVolume(state.volume.value);
    }, VOLUME_DEBOUNCE_MS);
  }

  async function commitVolume(level) {
    try {
      const data = await api('/volume', { method: 'POST', params: { level: level }, silent: true, timeout: 12000 });
      if (data && Number.isFinite(Number(data.volume))) {
        state.volume.value = clamp(Number(data.volume), 0, 100);
        renderVolumeUI();
      }
      writeStore(STORAGE.volume, state.volume.value);
    } catch (error) {
      if (error && error.status === 401) {
        handleUnauthorized();
      } else {
        toast('音量设置失败：' + (error && error.message ? error.message : '未知错误'), 'error');
      }
    }
  }

  function toggleMute() {
    if (state.volume.value > 0) {
      state.volume.mutedFrom = state.volume.value;
      setVolume(0);
    } else {
      setVolume(state.volume.mutedFrom || 30);
    }
  }

  function nudgeVolume(delta) {
    setVolume(state.volume.value + delta);
  }

  async function setQuality(quality) {
    const value = str(quality);
    if (!value || value === state.settings.quality) {
      renderQualitySelects();
      return;
    }
    const previous = state.settings.quality;
    state.settings.quality = value;
    state.settingsDirtyUntil = Date.now() + 2500;
    renderQualitySelects();
    try {
      await api('/quality', { method: 'POST', params: { set: value }, silent: true, timeout: 12000 });
      toast('音质已切换为「' + (state.settings.qualityLevels[value] || value) + '」', 'success');
    } catch (error) {
      state.settings.quality = previous;
      renderQualitySelects();
      toastApiError(error, '音质切换失败');
    }
  }

  async function setMode(mode) {
    const value = str(mode);
    if (!value || value === state.settings.playMode) return;
    const previous = state.settings.playMode;
    state.settings.playMode = value;
    state.settingsDirtyUntil = Date.now() + 2500;
    renderModeGroups();
    try {
      await api('/mode', { method: 'POST', params: { set: value }, silent: true, timeout: 12000 });
      toast('播放模式：' + modeLabel(value), 'success');
    } catch (error) {
      state.settings.playMode = previous;
      renderModeGroups();
      toastApiError(error, '模式切换失败');
    }
  }

  /* ========================================================================
   * 18. 动作：设备 / 账号
   * ====================================================================== */
  async function chooseDevice(ip) {
    if (!ip) return;
    try {
      await api('/devices/select', { method: 'POST', params: { ip }, silent: true, timeout: 15000 });
      state.activeDevice = ip;
      toast('已切换到 ' + deviceName(ip), 'success');
      await loadDevices({ silent: true });
      renderDeviceOptions();
      pollState();
    } catch (error) {
      toastApiError(error, '切换设备失败');
      renderDeviceOptions();
    }
  }

  function updateScanButtons() {
    [
      { selector: '#scanBtn', isBtn: false },
      { selector: '#settingsScanBtn', isBtn: true },
    ].forEach(({ selector }) => {
      const button = $(selector);
      if (!button) return;
      button.classList.toggle('is-loading', state.scanning);
      button.disabled = state.scanning;
    });
  }

  async function scanDevices() {
    if (state.scanning) return;
    state.scanning = true;
    updateScanButtons();
    try {
      const data = await api('/devices/scan', { method: 'POST', silent: true, timeout: 30000 });
      const found = Array.isArray(data.found) ? data.found : Array.isArray(data.devices) ? data.devices : [];
      if (found.length) toast('发现 ' + found.length + ' 个设备', 'success');
      else toast('没有发现新设备，请确认音箱已通电并连接同一网络', 'info');
      await loadDevices({ silent: true });
      setTimeout(pollState, 500);
    } catch (error) {
      toastApiError(error, '设备扫描失败');
    } finally {
      state.scanning = false;
      updateScanButtons();
    }
  }

  function openAccountModal() {
    openModal('#accountModal');
    renderAccountModal();
  }

  function renderAccountModal() {
    const body = $('#accountModalBody');
    if (!body) return;
    const account = state.account;
    if (account.loggedIn) {
      mount(
        body,
        el(
          'div',
          { class: 'acct' },
          el(
            'div',
            { class: 'acct__profile' },
            avatarNode(account, 'acct__avatar'),
            el(
              'div',
              { class: 'acct__meta' },
              el(
                'div',
                { class: 'acct__name' },
                el('span', { text: account.nickname || '网易云用户' }),
                account.vip ? el('span', { class: 'tag tag--vip', text: 'VIP' }) : null,
              ),
              el('p', {
                class: 'acct__sub',
                text: account.uid ? 'UID ' + account.uid : '已登录网易云音乐',
              }),
            ),
          ),
          el(
            'div',
            { class: 'acct__actions' },
            el(
              'button',
              { class: 'btn btn--ghost', type: 'button', on: { click: () => startQrLogin() } },
              icon('refresh'),
              el('span', { text: '重新扫码登录' }),
            ),
            el(
              'button',
              { class: 'btn btn--sm', type: 'button', on: { click: () => logoutAccount() } },
              icon('logout'),
              el('span', { text: '退出登录' }),
            ),
          ),
          el('p', { class: 'acct__hint', text: '切换账号后会自动刷新网易云歌单。' }),
        ),
      );
      return;
    }
    startQrLogin();
  }

  async function startQrLogin() {
    const body = $('#accountModalBody');
    if (!body) return;
    const token = ++state.qr.token;
    stopQrPoll();
    mount(
      body,
      el(
        'div',
        { class: 'acct' },
        el(
          'div',
          { class: 'acct__qr' },
          el(
            'div',
            { class: 'acct__qr-img' },
            el('div', { class: 'acct__qr-placeholder' }, el('span', { class: 'spinner' })),
          ),
          el('p', { class: 'acct__qr-tip', text: '请使用网易云音乐 App 扫码登录' }),
          el(
            'span',
            { class: 'acct__status' },
            el('span', { class: 'spinner' }),
            el('span', { text: '正在获取二维码…' }),
          ),
        ),
        el(
          'div',
          { class: 'acct__actions' },
          el(
            'button',
            {
              class: 'btn btn--ghost',
              type: 'button',
              class: 'btn btn--ghost js-qr-refresh',
              hidden: 'hidden',
              on: { click: () => startQrLogin() },
            },
            icon('refresh'),
            el('span', { text: '刷新二维码' }),
          ),
        ),
        el('p', { class: 'acct__hint', text: '扫码并确认后，登录信息会保存在服务端，歌单会自动刷新。' }),
      ),
    );

    try {
      await api('/account/qr', { method: 'POST', silent: true, timeout: 15000 });
      if (token !== state.qr.token) return;
      renderQrImage();
      startQrPoll();
    } catch (error) {
      if (token !== state.qr.token) return;
      showQrRefresh();
      setQrStatus('二维码获取失败：' + (error && error.message ? error.message : '请稍后重试'), false);
    }
  }

  function renderQrImage() {
    const holder = $('#accountModalBody .acct__qr-img');
    if (!holder) return;
    const image = el('img', {
      src: '/qr.png?ts=' + Date.now(),
      alt: '网易云登录二维码',
      referrerpolicy: 'no-referrer',
    });
    image.addEventListener(
      'error',
      () => {
        mount(holder, el('div', { class: 'acct__qr-placeholder' }, el('span', { text: '二维码加载失败' })));
        setQrStatus('二维码加载失败，请点击「刷新二维码」重试', false);
      },
      { once: true },
    );
    mount(holder, image);
  }

  function setQrStatus(text, busy = true) {
    const node = $('#accountModalBody .acct__status');
    if (!node) return;
    mount(node, busy ? el('span', { class: 'spinner' }) : null, el('span', { text: text }));
  }

  function showQrRefresh() {
    const button = $('#accountModalBody .js-qr-refresh');
    if (button) button.hidden = false;
  }

  function startQrPoll() {
    stopQrPoll();
    state.qr.timer = setInterval(async () => {
      if (document.hidden) return;
      try {
        const data = await api('/account/qr/status', { silent: true, timeout: 8000, retry: false });
        handleQrStatus(data);
      } catch (error) {
        /* 单次失败忽略 */
      }
    }, 1500);
  }

  function stopQrPoll() {
    if (state.qr.timer) {
      clearInterval(state.qr.timer);
      state.qr.timer = 0;
    }
  }

  function handleQrStatus(data) {
    const status = str(data && data.status);
    const message = str(data && data.message);
    setQrStatus(message || QR_STATUS_TEXT[status] || '等待扫码…', status !== 'success');
    if (status === 'success') {
      stopQrPoll();
      setQrStatus('登录成功', true);
      toast('网易云登录成功', 'success');
      setTimeout(async () => {
        closeModal('#accountModal');
        await loadAccount();
        await loadPlaylists({ silent: true });
        if (state.netease.open) loadNeteasePlaylists();
      }, 650);
    } else if (status === 'expired' || status === 'error') {
      stopQrPoll();
      showQrRefresh();
      if (status === 'expired') toast('二维码已过期，请刷新后重新扫码', 'warn');
    }
  }

  async function logoutAccount() {
    try {
      await api('/account/logout', { method: 'POST', silent: true, timeout: 12000 });
      state.account = { loggedIn: false, nickname: '', uid: null, avatar: '', vip: false, reason: '' };
      renderAccountChip();
      renderSettingsAccount();
      closeModal('#accountModal');
      toast('已退出网易云账号', 'success');
      state.netease.playlists = [];
      await loadPlaylists({ silent: true });
      if (state.netease.open) loadNeteasePlaylists();
    } catch (error) {
      toastApiError(error, '退出登录失败');
    }
  }

  /* ========================================================================
   * 19. 动作：搜索 / 歌单 / 队列 / 本地 / 下载 / 设置
   * ====================================================================== */
  async function runSearch() {
    const query = str(state.search.q).trim();
    if (!query) {
      renderSearchIdle();
      return;
    }
    const seq = ++state.search.seq;
    state.search.loading = true;
    renderSearchSkeleton();
    setText($('#searchHint'), '正在搜索「' + query + '」…');
    try {
      const data = await api('/search', { params: { q: query }, silent: true, timeout: 20000 });
      if (seq !== state.search.seq) return;
      state.search.songs = normalizeSongs(data);
      state.search.loading = false;
      renderSearchResults(state.search.songs);
      setText(
        $('#searchHint'),
        state.search.songs.length
          ? '找到 ' + state.search.songs.length + ' 首「' + query + '」，点击播放推送到音箱'
          : '没有找到与「' + query + '」相关的歌曲',
      );
    } catch (error) {
      if (seq !== state.search.seq) return;
      state.search.loading = false;
      renderSearchError(error && error.message);
      setText($('#searchHint'), '搜索失败');
      if (!(error && error.status === 401)) {
        toast('搜索失败：' + (error && error.message ? error.message : '网络异常'), 'error');
      }
    }
  }

  function setTargetPlaylist(value) {
    state.targetPlaylistId = str(value);
    writeStore(STORAGE.target, state.targetPlaylistId);
    renderTargetSelects();
  }

  async function playSearchResult(index, button) {
    const song = state.search.songs[index];
    if (!song) return;
    const query = str(state.search.q).trim() || song.name;
    await withLoading(button, async () => {
      try {
        const data = await api('/play', {
          method: 'POST',
          params: { q: query, i: index },
          silent: true,
          timeout: 25000,
        });
        const played = data && data.song && typeof data.song === 'object' ? data.song : song;
        toast('正在播放「' + (played.name || song.name) + '」', 'success');
        setTimeout(pollState, 600);
      } catch (error) {
        if (error && error.status === 409) {
          toast('该曲目需要 VIP 或暂无可用直链，换一首试试', 'warn');
        } else if (error && error.status === 404) {
          toast('没有找到这首歌，可能已下架', 'warn');
        } else {
          toastApiError(error, '播放失败');
        }
      }
    });
  }

  async function addSearchSongToPlaylist(index, button) {
    const song = state.search.songs[index];
    if (!song) return;
    const playlistId = state.targetPlaylistId || state.currentPlaylistId;
    if (!playlistId) {
      toast('还没有歌单，请先在「歌单」页新建', 'warn');
      return;
    }
    await withLoading(button, async () => {
      try {
        const data = await api('/playlist/add', {
          method: 'POST',
          params: {
            id: playlistId,
            song_id: song.songId,
            name: song.name,
            artist: song.artist,
            album: song.album,
          },
          silent: true,
          timeout: 15000,
        });
        const added = !data || data.added !== false;
        toast(
          added ? '已加入「' + playlistNameById(playlistId) + '」' : '这首歌已在歌单中',
          added ? 'success' : 'info',
        );
        if (added) loadPlaylists({ skipTracks: true, silent: true });
      } catch (error) {
        toastApiError(error, '加入歌单失败');
      }
    });
  }

  async function downloadSearchSong(index, button) {
    const song = state.search.songs[index];
    if (!song) return;
    await withLoading(button, async () => {
      try {
        await api('/download/song', {
          method: 'POST',
          params: {
            song_id: song.songId,
            name: song.name,
            artist: song.artist,
            album: song.album,
            quality: state.settings.quality,
          },
          silent: true,
          timeout: 15000,
        });
        toast('已加入下载队列，可在「下载」查看进度', 'success');
        syncDownloadPoll();
        if (state.tab === 'downloads') loadDownloads();
      } catch (error) {
        toastApiError(error, '下载入队失败');
      }
    });
  }

  async function createPlaylist() {
    const name = await promptDialog({
      title: '新建歌单',
      label: '歌单名称',
      placeholder: '例如：通勤歌单',
      confirmText: '创建',
    });
    if (!name) return;
    try {
      const data = await api('/playlist/create', { method: 'POST', params: { name }, silent: true, timeout: 12000 });
      toast('已创建歌单「' + name + '」', 'success');
      const newId = data && data.id !== undefined ? String(data.id) : '';
      await loadPlaylists({ skipTracks: true, silent: true });
      if (newId) {
        state.currentPlaylistId = newId;
        state.targetPlaylistId = newId;
        writeStore(STORAGE.playlist, newId);
        writeStore(STORAGE.target, newId);
        renderPlaylistSelect();
        renderTargetSelects();
        await loadTracks();
      }
    } catch (error) {
      toastApiError(error, '创建歌单失败');
    }
  }

  async function renamePlaylist() {
    const playlist = currentPlaylist();
    if (!playlist) return;
    const name = await promptDialog({
      title: '重命名歌单',
      label: '新名称',
      value: playlist.name,
      confirmText: '保存',
    });
    if (!name || name === playlist.name) return;
    try {
      await api('/playlist/rename', {
        method: 'POST',
        params: { id: playlist.id, name },
        silent: true,
        timeout: 12000,
      });
      toast('歌单已重命名', 'success');
      await loadPlaylists({ skipTracks: true, silent: true });
    } catch (error) {
      toastApiError(error, '重命名失败');
    }
  }

  async function deletePlaylist() {
    const playlist = currentPlaylist();
    if (!playlist) return;
    const confirmed = await confirmDialog({
      title: '删除歌单',
      message: '确定删除歌单「' + playlist.name + '」吗？仅删除歌单本身，本地已下载的音乐文件会保留。',
      confirmText: '删除',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api('/playlist/delete', {
        method: 'POST',
        params: { id: playlist.id },
        silent: true,
        timeout: 12000,
      });
      state.currentPlaylistId = '';
      state.tracks = [];
      toast('歌单已删除', 'success');
      await loadPlaylists({ silent: true });
    } catch (error) {
      toastApiError(error, '删除失败');
    }
  }

  async function playWholePlaylist() {
    const playlist = currentPlaylist();
    if (!playlist) return;
    if (!state.tracks.length) {
      toast('这个歌单还没有曲目', 'info');
      return;
    }
    try {
      await api('/playlist/play', {
        method: 'POST',
        params: { id: playlist.id, i: 0, whole: 1 },
        silent: true,
        timeout: 25000,
      });
      toast('开始播放「' + playlist.name + '」', 'success');
      setTimeout(pollState, 600);
      if (state.tab === 'queue') setTimeout(() => loadQueue({ silent: true }), 900);
    } catch (error) {
      toastApiError(error, '播放歌单失败');
    }
  }

  async function playTrackIndex(index) {
    const playlist = currentPlaylist();
    const track = state.tracks[index];
    if (!playlist || !track) return;
    try {
      await api('/playlist/play', {
        method: 'POST',
        params: { id: playlist.id, i: index },
        silent: true,
        timeout: 25000,
      });
      toast('正在播放「' + track.name + '」', 'success');
      setTimeout(pollState, 600);
      if (state.tab === 'queue') setTimeout(() => loadQueue({ silent: true }), 900);
    } catch (error) {
      toastApiError(error, '播放失败');
    }
  }

  async function playQueueIndex(index) {
    const queue = state.queue;
    if (!queue || queue.playlistId === null) return;
    const track = queue.tracks[index];
    try {
      await api('/playlist/play', {
        method: 'POST',
        params: { id: queue.playlistId, i: index },
        silent: true,
        timeout: 25000,
      });
      toast(track ? '正在播放「' + track.name + '」' : '已切换曲目', 'success');
      setTimeout(pollState, 600);
      setTimeout(() => loadQueue({ silent: true }), 900);
    } catch (error) {
      toastApiError(error, '切歌失败');
    }
  }

  async function removeTrack(track) {
    const playlist = currentPlaylist();
    if (!playlist || !track) return;
    try {
      await api('/playlist/remove', {
        method: 'POST',
        params: { id: playlist.id, track: track.id },
        silent: true,
        timeout: 12000,
      });
      toast('已从歌单移除「' + track.name + '」', 'success');
      await loadTracks({ silent: true });
      loadPlaylists({ skipTracks: true, silent: true });
    } catch (error) {
      toastApiError(error, '移除失败');
    }
  }

  async function moveTrack(from, to) {
    if (!state.tracks.length) return;
    if (to < 0 || to >= state.tracks.length || from === to) return;
    const previous = state.tracks.slice();
    const next = state.tracks.slice();
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    state.tracks = next;
    renderTracks();
    try {
      await api('/playlist/reorder', {
        method: 'POST',
        body: { id: idValue(state.currentPlaylistId), order: next.map((track) => track.id) },
        silent: true,
        timeout: 15000,
      });
      toast('顺序已保存', 'success');
    } catch (error) {
      state.tracks = previous;
      renderTracks();
      toastApiError(error, '排序保存失败');
    }
  }

  async function downloadTrack(track, button) {
    if (!track || track.songId === null || track.songId === undefined) {
      toast('本地曲目无需下载', 'info');
      return;
    }
    await withLoading(button, async () => {
      try {
        await api('/download/song', {
          method: 'POST',
          params: {
            song_id: track.songId,
            name: track.name,
            artist: track.artist,
            album: track.album,
            quality: state.settings.quality,
          },
          silent: true,
          timeout: 15000,
        });
        toast('已加入下载队列：' + track.name, 'success');
        syncDownloadPoll();
      } catch (error) {
        toastApiError(error, '下载入队失败');
      }
    });
  }

  async function downloadWholePlaylist() {
    const playlist = currentPlaylist();
    if (!playlist) return;
    try {
      const data = await api('/download/playlist', {
        method: 'POST',
        params: { id: playlist.id },
        silent: true,
        timeout: 20000,
      });
      const queued = Number(data && data.queued) || 0;
      toast(
        queued > 0 ? '已排队 ' + queued + ' 首，正在后台下载' : '没有需要下载的曲目（可能都已下载或在队列中）',
        queued > 0 ? 'success' : 'info',
      );
      syncDownloadPoll();
      if (state.tab === 'downloads') loadDownloads();
    } catch (error) {
      toastApiError(error, '下载整张失败');
    }
  }

  function toggleNeteasePanel(force) {
    const open = typeof force === 'boolean' ? force : !state.netease.open;
    state.netease.open = open;
    const panel = $('#neteasePanel');
    if (panel) panel.hidden = !open;
    const toggle = $('#neteaseToggleBtn');
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !state.netease.playlists.length) loadNeteasePlaylists();
  }

  async function importNeteasePlaylist(remoteId, name, button) {
    await withLoading(button, async () => {
      try {
        const data = await api('/netease/import', {
          method: 'POST',
          params: { id: remoteId, name },
          silent: true,
          timeout: 90000,
        });
        toast('已导入「' + (str(data && data.name) || name) + '」，新增 ' + (Number(data && data.added) || 0) + ' 首', 'success');
        await loadPlaylists({ silent: true });
        if (data && data.playlist_id !== undefined) {
          state.currentPlaylistId = String(data.playlist_id);
          renderPlaylistSelect();
          await loadTracks();
        }
        if (state.netease.open) loadNeteasePlaylists();
      } catch (error) {
        toastApiError(error, '导入歌单失败');
      }
    });
  }

  async function syncNeteasePlaylist(localId, name, button) {
    await withLoading(button, async () => {
      try {
        const data = await api('/netease/sync', {
          method: 'POST',
          params: { id: localId },
          silent: true,
          timeout: 90000,
        });
        const added = Number(data && data.added) || 0;
        toast('已同步「' + name + '」，新增 ' + added + ' 首', 'success');
        await loadPlaylists({ silent: true });
        if (state.netease.open) loadNeteasePlaylists();
      } catch (error) {
        toastApiError(error, '同步歌单失败');
      }
    });
  }

  async function syncAllNetease(button) {
    await withLoading(button, async () => {
      try {
        await api('/netease/sync_all', { method: 'POST', silent: true, timeout: 20000 });
        toast('已开始全量同步，进度会显示在上方', 'info');
        showSyncProgress(true);
        renderSyncProgress({ running: true, done: 0, total: 0, added: 0, current: '准备中' });
        startSyncPoll();
      } catch (error) {
        toastApiError(error, '启动同步失败');
      }
    });
  }

  async function playLocalTrack(index) {
    const track = state.local.tracks[index];
    if (!track) return;
    try {
      await api('/local/play', { method: 'POST', params: { id: track.id }, silent: true, timeout: 20000 });
      toast('正在播放「' + (track.title || '本地曲目') + '」', 'success');
      setTimeout(pollState, 600);
    } catch (error) {
      toastApiError(error, '本地播放失败');
    }
  }

  async function addLocalToPlaylist(index, button) {
    const track = state.local.tracks[index];
    if (!track) return;
    const playlistId = state.targetPlaylistId || state.currentPlaylistId;
    if (!playlistId) {
      toast('还没有歌单，请先在「歌单」页新建', 'warn');
      return;
    }
    await withLoading(button, async () => {
      try {
        const data = await api('/local/add', {
          method: 'POST',
          params: { id: playlistId, track: track.id },
          silent: true,
          timeout: 15000,
        });
        const added = !data || data.added !== false;
        toast(
          added ? '已加入「' + playlistNameById(playlistId) + '」' : '这首歌已在歌单中',
          added ? 'success' : 'info',
        );
        if (added) loadPlaylists({ skipTracks: true, silent: true });
      } catch (error) {
        toastApiError(error, '加入歌单失败');
      }
    });
  }

  function setLocalScanning(active) {
    state.localScanning = active;
    ['#localScanBtn', '#settingsScanBtn'].forEach((selector) => {
      const button = $(selector);
      if (!button) return;
      button.classList.toggle('is-loading', active);
      button.disabled = active;
    });
  }

  async function scanLocal() {
    if (state.localScanning) return;
    setLocalScanning(true);
    try {
      const data = await api('/local/scan', { method: 'POST', silent: true, timeout: 180000 });
      const added = Number(data && data.added) || 0;
      const updated = Number(data && data.updated) || 0;
      const removed = Number(data && data.removed) || 0;
      const total = Number(data && data.total) || 0;
      toast('扫描完成：新增 ' + added + ' · 更新 ' + updated + ' · 移除 ' + removed + ' · 共 ' + total + ' 首', 'success');
      await loadLocal({ silent: true });
    } catch (error) {
      toastApiError(error, '扫描曲库失败');
      await loadLocal({ silent: true });
    } finally {
      setLocalScanning(false);
    }
  }

  async function retryDownload(job, button) {
    await withLoading(button, async () => {
      try {
        await api('/download/retry', { method: 'POST', params: { id: job.id }, silent: true, timeout: 12000 });
        toast('已重新加入下载队列', 'success');
        await loadDownloads();
      } catch (error) {
        toastApiError(error, '重试失败');
      }
    });
  }

  async function cancelDownload(job, button) {
    await withLoading(button, async () => {
      try {
        await api('/download/cancel', { method: 'POST', params: { id: job.id }, silent: true, timeout: 12000 });
        toast('已取消下载', 'success');
        await loadDownloads();
      } catch (error) {
        toastApiError(error, '取消失败');
      }
    });
  }

  async function retryAllDownloads(button) {
    await withLoading(button, async () => {
      try {
        const data = await api('/download/retry', { method: 'POST', silent: true, timeout: 12000 });
        const count = Number(data && data.retried) || 0;
        toast(count > 0 ? '已重新排队 ' + count + ' 个失败任务' : '没有需要重试的任务', count > 0 ? 'success' : 'info');
        await loadDownloads();
      } catch (error) {
        toastApiError(error, '重试失败');
      }
    });
  }

  /* ---------------- 免打扰 ---------------- */
  async function toggleQuiet() {
    const enabled = !!state.settings.quietHours;
    try {
      if (enabled) {
        await api('/quiet', { method: 'POST', params: { off: 1 }, silent: true, timeout: 12000 });
        state.settings.quietHours = '';
        state.settings.quietActive = false;
        state.settingsDirtyUntil = Date.now() + 2500;
        renderQuiet();
        toast('已关闭免打扰', 'success');
      } else {
        const start = ($('#quietStart') && $('#quietStart').value) || '23:30';
        const end = ($('#quietEnd') && $('#quietEnd').value) || '08:00';
        if (start === end) {
          toast('开始与结束时间不能相同', 'error');
          return;
        }
        const range = start + '-' + end;
        await api('/quiet', { method: 'POST', params: { set: range }, silent: true, timeout: 12000 });
        state.settings.quietHours = range;
        state.settingsDirtyUntil = Date.now() + 2500;
        renderQuiet();
        toast('免打扰已启用：' + range, 'success');
      }
    } catch (error) {
      renderQuiet();
      toastApiError(error, '免打扰设置失败');
    }
  }

  const commitQuietTimes = debounce(async () => {
    if (!state.settings.quietHours) return;
    const startNode = $('#quietStart');
    const endNode = $('#quietEnd');
    if (!startNode || !endNode) return;
    const start = startNode.value;
    const end = endNode.value;
    if (!start || !end || start === end) return;
    const range = start + '-' + end;
    if (range === state.settings.quietHours) return;
    try {
      await api('/quiet', { method: 'POST', params: { set: range }, silent: true, timeout: 12000 });
      state.settings.quietHours = range;
      state.settingsDirtyUntil = Date.now() + 2500;
      renderQuiet();
      toast('免打扰时段已更新：' + range, 'success');
    } catch (error) {
      toastApiError(error, '免打扰时段更新失败');
      renderQuiet();
    }
  }, 500);

  /* ---------------- 本地缓存 ---------------- */
  async function clearLocalCache() {
    const confirmed = await confirmDialog({
      title: '清除本地缓存',
      message: '将清除浏览器保存的标签页记忆、目标歌单与音量记录，并刷新页面。服务器上的歌单、曲库与已下载文件不受影响。',
      confirmText: '清除并刷新',
      danger: true,
    });
    if (!confirmed) return;
    Object.values(STORAGE).forEach((key) => {
      try {
        window.localStorage.removeItem(key);
      } catch (error) {
        /* 忽略 */
      }
    });
    toast('本地缓存已清除，正在刷新…', 'success');
    setTimeout(() => window.location.reload(), 600);
  }

  /* ========================================================================
   * 20. 事件绑定
   * ====================================================================== */
  function bindTopbar() {
    const deviceSelect = $('#deviceSelect');
    if (deviceSelect) {
      deviceSelect.addEventListener('change', () => chooseDevice(deviceSelect.value));
    }
    const scanBtn = $('#scanBtn');
    if (scanBtn) scanBtn.addEventListener('click', () => scanDevices());
    const accountBtn = $('#accountBtn');
    if (accountBtn) accountBtn.addEventListener('click', () => openAccountModal());
    const authReload = $('#authReloadBtn');
    if (authReload) authReload.addEventListener('click', () => window.location.reload());
  }

  function bindPlayer() {
    const prev = $('#prevBtn');
    const next = $('#nextBtn');
    const play = $('#playToggleBtn');
    const stop = $('#stopBtn');
    const mute = $('#muteBtn');
    const resume = $('#interruptResumeBtn');
    const closeBanner = $('#interruptCloseBtn');
    const quality = $('#qualitySelect');
    const slider = $('#volumeSlider');
    const modeGroup = $('#modeGroup');

    if (prev) prev.addEventListener('click', () => playerAction('prev'));
    if (next) next.addEventListener('click', () => playerAction('next'));
    if (play) play.addEventListener('click', () => togglePlay());
    if (stop) stop.addEventListener('click', () => playerAction('stop', { message: '已停止播放' }));
    if (mute) mute.addEventListener('click', () => toggleMute());
    if (resume) resume.addEventListener('click', () => resumePlayback());
    if (closeBanner) {
      closeBanner.addEventListener('click', () => {
        const info = state.player.interrupted;
        state.interruptDismissedKey = info
          ? str(info.at) + '|' + str(info.title) + '|' + str(info.position) + '|' + str(info.reason)
          : '';
        renderInterrupt();
      });
    }
    if (quality) quality.addEventListener('change', () => setQuality(quality.value));
    if (slider) {
      slider.addEventListener('input', () => {
        setVolume(Number(slider.value));
      });
    }
    if (modeGroup) {
      modeGroup.addEventListener('click', (event) => {
        const button = event.target.closest('.seg-btn');
        if (!button || !modeGroup.contains(button)) return;
        setMode(button.dataset.mode);
      });
    }
  }

  function bindProgress() {
    const node = $('#progress');
    const bubble = $('#progressBubble');
    if (!node || !bubble) return;

    const applyPointer = (event) => {
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0) return;
      const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1);
      const duration = Number(state.player.duration) || 0;
      state.seek.value = Math.round(ratio * duration * 10) / 10;
      renderProgress();
      const left = clamp(event.clientX - rect.left, 28, Math.max(28, rect.width - 28));
      bubble.style.left = left + 'px';
    };

    node.addEventListener('pointerdown', (event) => {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      if (!(Number(state.player.duration) > 0)) {
        toast('当前曲目无法跳转（设备未上报时长）', 'info');
        return;
      }
      state.seek.dragging = true;
      node.classList.add('is-dragging');
      if (node.setPointerCapture) {
        try {
          node.setPointerCapture(event.pointerId);
        } catch (error) {
          /* 忽略 */
        }
      }
      applyPointer(event);
      event.preventDefault();
    });

    node.addEventListener('pointermove', (event) => {
      if (!(Number(state.player.duration) > 0)) return;
      if (state.seek.dragging) {
        applyPointer(event);
        return;
      }
      if (event.pointerType === 'mouse') {
        node.classList.add('is-hover');
        applyPointer(event);
        bubble.textContent = fmtTime(state.seek.value);
      }
    });

    const finish = () => {
      if (!state.seek.dragging) return;
      state.seek.dragging = false;
      node.classList.remove('is-dragging', 'is-hover');
      const target = state.seek.value;
      seekTo(target);
    };

    node.addEventListener('pointerup', finish);
    node.addEventListener('pointercancel', finish);
    node.addEventListener('pointerleave', () => {
      if (!state.seek.dragging) node.classList.remove('is-hover');
    });
    node.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      event.stopPropagation();
      seekBy(event.key === 'ArrowLeft' ? -5 : 5);
    });
  }

  function bindTabs() {
    const tabs = $('#tabs');
    if (!tabs) return;
    tabs.addEventListener('click', (event) => {
      const button = event.target.closest('.tab');
      if (!button || !tabs.contains(button)) return;
      switchTab(button.dataset.tab);
    });
    tabs.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      const buttons = $$('.tab', tabs);
      const current = buttons.indexOf(document.activeElement);
      if (current < 0) return;
      event.preventDefault();
      const next = (current + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
      switchTab(buttons[next].dataset.tab);
    });
    window.addEventListener('resize', debounce(moveTabIndicator, 120));
  }

  function bindSearch() {
    const input = $('#searchInput');
    const clear = $('#searchClear');
    const target = $('#searchTarget');
    const list = $('#searchList');
    if (!input) return;

    input.addEventListener('input', () => {
      state.search.q = input.value;
      if (clear) clear.hidden = !input.value;
      if (!input.value.trim()) {
        state.search.seq += 1;
        state.search.songs = [];
        renderSearchIdle();
        return;
      }
      clearTimeout(state.search.timer);
      state.search.timer = setTimeout(() => runSearch(), SEARCH_DEBOUNCE_MS);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        clearTimeout(state.search.timer);
        runSearch();
      } else if (event.key === 'Escape') {
        input.value = '';
        state.search.q = '';
        if (clear) clear.hidden = true;
        state.search.seq += 1;
        state.search.songs = [];
        renderSearchIdle();
        input.blur();
      }
    });
    if (clear) {
      clear.addEventListener('click', () => {
        input.value = '';
        state.search.q = '';
        clear.hidden = true;
        state.search.seq += 1;
        state.search.songs = [];
        renderSearchIdle();
        input.focus();
      });
    }
    if (target) target.addEventListener('change', () => setTargetPlaylist(target.value));
    if (list) {
      list.addEventListener('click', (event) => {
        const button = event.target.closest('[data-action]');
        if (!button) return;
        const index = Number(button.dataset.index);
        const action = button.dataset.action;
        if (action === 'play') playSearchResult(index, button);
        else if (action === 'add') addSearchSongToPlaylist(index, button);
        else if (action === 'download') downloadSearchSong(index, button);
      });
    }
  }

  function clearDropMarks() {
    $$('#tracksList .track').forEach((row) => row.classList.remove('is-drop-before', 'is-drop-after', 'is-dragging'));
  }

  function bindPlaylists() {
    const select = $('#playlistSelect');
    if (select) {
      select.addEventListener('change', async () => {
        state.currentPlaylistId = select.value;
        writeStore(STORAGE.playlist, state.currentPlaylistId);
        renderPlaylistsPanel();
        await loadTracks();
      });
    }
    const newBtn = $('#newPlaylistBtn');
    const renameBtn = $('#renamePlaylistBtn');
    const deleteBtn = $('#deletePlaylistBtn');
    const playAllBtn = $('#playAllBtn');
    const downloadBtn = $('#downloadWholeBtn');
    const neteaseToggle = $('#neteaseToggleBtn');
    const neteaseClose = $('#neteaseCloseBtn');
    const syncAllBtn = $('#syncAllBtn');

    if (newBtn) newBtn.addEventListener('click', () => createPlaylist());
    if (renameBtn) renameBtn.addEventListener('click', () => renamePlaylist());
    if (deleteBtn) deleteBtn.addEventListener('click', () => deletePlaylist());
    if (playAllBtn) playAllBtn.addEventListener('click', () => playWholePlaylist());
    if (downloadBtn) downloadBtn.addEventListener('click', () => downloadWholePlaylist());
    if (neteaseToggle) neteaseToggle.addEventListener('click', () => toggleNeteasePanel());
    if (neteaseClose) neteaseClose.addEventListener('click', () => toggleNeteasePanel(false));
    if (syncAllBtn) syncAllBtn.addEventListener('click', () => syncAllNetease(syncAllBtn));

    const tracksList = $('#tracksList');
    if (tracksList) {
      tracksList.addEventListener('click', (event) => {
        const row = event.target.closest('.track');
        if (!row) return;
        const index = Number(row.dataset.index);
        const track = state.tracks[index];
        if (!track) return;
        const button = event.target.closest('[data-action]');
        if (!button) return;
        const action = button.dataset.action;
        if (action === 'play') playTrackIndex(index);
        else if (action === 'download') downloadTrack(track, button);
        else if (action === 'remove') removeTrack(track);
        else if (action === 'moveup') moveTrack(index, index - 1);
        else if (action === 'movedown') moveTrack(index, index + 1);
      });

      tracksList.addEventListener('dragstart', (event) => {
        const row = event.target.closest('.track');
        if (!row) return;
        state.drag.from = Number(row.dataset.index);
        row.classList.add('is-dragging');
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = 'move';
          try {
            event.dataTransfer.setData('text/plain', String(state.drag.from));
          } catch (error) {
            /* 忽略 */
          }
        }
      });

      tracksList.addEventListener('dragover', (event) => {
        if (state.drag.from < 0) return;
        const row = event.target.closest('.track');
        if (!row) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
        const rect = row.getBoundingClientRect();
        const before = event.clientY < rect.top + rect.height / 2;
        clearDropMarks();
        row.classList.add(before ? 'is-drop-before' : 'is-drop-after');
      });

      tracksList.addEventListener('drop', (event) => {
        if (state.drag.from < 0) return;
        const row = event.target.closest('.track');
        if (!row) return;
        event.preventDefault();
        const rect = row.getBoundingClientRect();
        const before = event.clientY < rect.top + rect.height / 2;
        let to = Number(row.dataset.index) + (before ? 0 : 1);
        const from = state.drag.from;
        clearDropMarks();
        state.drag.from = -1;
        if (to > from) to -= 1;
        if (from !== to) moveTrack(from, to);
      });

      tracksList.addEventListener('dragend', () => {
        state.drag.from = -1;
        clearDropMarks();
      });
    }

    const neteaseList = $('#neteaseList');
    if (neteaseList) {
      neteaseList.addEventListener('click', (event) => {
        const button = event.target.closest('[data-action]');
        if (!button) return;
        const action = button.dataset.action;
        if (action === 'import') {
          importNeteasePlaylist(button.dataset.remote, button.dataset.name, button);
        } else if (action === 'sync') {
          syncNeteasePlaylist(button.dataset.local, button.dataset.name, button);
        }
      });
    }
  }

  function bindQueue() {
    const list = $('#queueList');
    if (!list) return;
    const activate = (row) => {
      if (!row) return;
      playQueueIndex(Number(row.dataset.index));
    };
    list.addEventListener('click', (event) => {
      const row = event.target.closest('.track');
      if (row) activate(row);
    });
    list.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const row = event.target.closest('.track');
      if (!row) return;
      event.preventDefault();
      activate(row);
    });
  }

  function bindLocal() {
    const input = $('#localInput');
    const clear = $('#localClear');
    const scanBtn = $('#localScanBtn');
    const target = $('#localTarget');
    const list = $('#localList');
    if (!input) return;

    input.addEventListener('input', () => {
      state.local.q = input.value;
      if (clear) clear.hidden = !input.value;
      clearTimeout(state.local.timer);
      state.local.timer = setTimeout(() => loadLocal(), LOCAL_DEBOUNCE_MS);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        clearTimeout(state.local.timer);
        loadLocal();
      } else if (event.key === 'Escape') {
        input.value = '';
        state.local.q = '';
        if (clear) clear.hidden = true;
        loadLocal();
      }
    });
    if (clear) {
      clear.addEventListener('click', () => {
        input.value = '';
        state.local.q = '';
        clear.hidden = true;
        loadLocal();
        input.focus();
      });
    }
    if (scanBtn) scanBtn.addEventListener('click', () => scanLocal());
    if (target) target.addEventListener('change', () => setTargetPlaylist(target.value));
    if (list) {
      list.addEventListener('click', (event) => {
        const button = event.target.closest('[data-action]');
        if (!button) return;
        const index = Number(button.dataset.index);
        const action = button.dataset.action;
        if (action === 'play') playLocalTrack(index);
        else if (action === 'add') addLocalToPlaylist(index, button);
      });
    }
  }

  function bindDownloads() {
    const retryAll = $('#downloadRetryAllBtn');
    const list = $('#downloadList');
    if (retryAll) retryAll.addEventListener('click', () => retryAllDownloads(retryAll));
    if (list) {
      list.addEventListener('click', (event) => {
        const button = event.target.closest('[data-action]');
        if (!button) return;
        const row = button.closest('.download');
        if (!row) return;
        const job = state.downloads.jobs.find((item) => String(item.id) === row.dataset.id);
        if (!job) return;
        if (button.dataset.action === 'retry') retryDownload(job, button);
        else if (button.dataset.action === 'cancel') cancelDownload(job, button);
      });
    }
  }

  function bindSettings() {
    const settingsQuality = $('#settingsQuality');
    const settingsModes = $('#settingsModes');
    const quietToggle = $('#quietToggle');
    const quietStart = $('#quietStart');
    const quietEnd = $('#quietEnd');
    const scanBtn = $('#settingsScanBtn');
    const deviceList = $('#settingsDeviceList');
    const accountHolder = $('#settingsAccount');
    const clearBtn = $('#clearCacheBtn');

    if (settingsQuality) settingsQuality.addEventListener('change', () => setQuality(settingsQuality.value));
    if (settingsModes) {
      settingsModes.addEventListener('click', (event) => {
        const button = event.target.closest('.seg-btn');
        if (!button || !settingsModes.contains(button)) return;
        setMode(button.dataset.mode);
      });
    }
    if (quietToggle) quietToggle.addEventListener('click', () => toggleQuiet());
    if (quietStart) quietStart.addEventListener('change', () => commitQuietTimes());
    if (quietEnd) quietEnd.addEventListener('change', () => commitQuietTimes());
    if (scanBtn) scanBtn.addEventListener('click', () => scanDevices());
    if (deviceList) {
      deviceList.addEventListener('click', (event) => {
        const button = event.target.closest('.device-item');
        if (!button) return;
        chooseDevice(button.dataset.ip);
      });
    }
    if (accountHolder) {
      accountHolder.addEventListener('click', (event) => {
        const button = event.target.closest('[data-action]');
        if (!button) return;
        const action = button.dataset.action;
        if (action === 'account-login') openAccountModal();
        else if (action === 'account-logout') logoutAccount();
        else if (action === 'account-refresh') loadAccount();
      });
    }
    if (clearBtn) clearBtn.addEventListener('click', () => clearLocalCache());
  }

  function bindModals() {
    const accountClose = $('#accountModalClose');
    if (accountClose) accountClose.addEventListener('click', () => closeModal('#accountModal'));
    const dialogClose = $('#dialogClose');
    if (dialogClose) dialogClose.addEventListener('click', () => closeDialog(null));

    $$('.modal').forEach((modal) => {
      modal.addEventListener('click', (event) => {
        const closer = event.target.closest('[data-close]');
        if (!closer) return;
        if (modal.id === 'dialog') closeDialog(null);
        else closeModal('#' + modal.id);
      });
    });

    const dialogBody = $('#dialogBody');
    if (dialogBody) {
      dialogBody.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && event.target.tagName === 'INPUT') {
          event.preventDefault();
          if (state.dialog.confirmBtn) state.dialog.confirmBtn.click();
        }
      });
    }
  }

  function bindKeyboard() {
    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      const tagName = target && target.tagName ? target.tagName : '';
      const typing =
        tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT' || (target && target.isContentEditable);

      if (isModalOpen()) {
        if (event.key === 'Escape') {
          event.preventDefault();
          closeTopModal();
        }
        return;
      }
      if (typing) {
        if (event.key === 'Escape' && target.blur) target.blur();
        return;
      }
      const interactive = target && target.closest
        ? target.closest('button, a, [role="button"], [role="switch"], [role="radio"]')
        : null;
      // 开关/单选类控件保留空格的原生语义；普通按钮上仍允许全局快捷键
      if (interactive && event.key === ' ' && interactive.matches('[role="switch"], [role="radio"], input[type="checkbox"]')) {
        return;
      }

      switch (event.key) {
        case ' ':
          event.preventDefault();
          togglePlay();
          break;
        case 'ArrowLeft':
          event.preventDefault();
          seekBy(-10);
          break;
        case 'ArrowRight':
          event.preventDefault();
          seekBy(10);
          break;
        case 'ArrowUp':
          event.preventDefault();
          nudgeVolume(5);
          break;
        case 'ArrowDown':
          event.preventDefault();
          nudgeVolume(-5);
          break;
        case '/':
        case '？':
          event.preventDefault();
          focusSearch();
          break;
        case 'Escape':
          if (state.seek.dragging) {
            state.seek.dragging = false;
            renderProgress();
          }
          break;
        default:
          break;
      }
    });
  }

  function focusSearch() {
    if (state.tab !== 'search') switchTab('search');
    const input = $('#searchInput');
    if (!input) return;
    setTimeout(() => {
      input.focus();
      if (input.select) input.select();
    }, 40);
  }

  function bindVisibility() {
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        clearTimeout(state.timers.state);
        state.timers.state = 0;
        if (state.timers.download) {
          clearInterval(state.timers.download);
          state.timers.download = 0;
        }
        return;
      }
      pollState();
      if (state.tab === 'downloads') loadDownloads();
      if (state.qr.timer) {
        /* 二维码轮询回调内部会检查 document.hidden，这里无需重启 */
      }
    });
  }

  /* ========================================================================
   * 21. 启动
   * ====================================================================== */
  function bindAll() {
    bindTopbar();
    bindPlayer();
    bindProgress();
    bindTabs();
    bindSearch();
    bindPlaylists();
    bindQueue();
    bindLocal();
    bindDownloads();
    bindSettings();
    bindModals();
    bindKeyboard();
    bindVisibility();
  }

  async function init() {
    document.body.classList.add('booting');

    state.tab = normalizeTab(readStore(STORAGE.tab, 'search'));
    const storedVolume = Number(readStore(STORAGE.volume, '30'));
    state.volume.value = Number.isFinite(storedVolume) ? clamp(storedVolume, 0, 100) : 30;
    state.volume.mutedFrom = state.volume.value || 30;

    bindAll();

    // 首屏先渲染本地默认值（无网络也不白屏）
    renderConnection();
    renderVolumeUI();
    renderQualitySelects();
    renderModeGroups();
    renderPlayToggle();
    renderBadge();
    renderProgress();
    renderAccountChip();
    renderPlaylistsPanel();
    renderSearchIdle();
    renderSettings();
    switchTab(state.tab, { silent: true });

    setTimeout(finishBoot, BOOT_TIMEOUT_MS);
    requestAnimationFrame(moveTabIndicator);

    // 并发初始化（互不依赖）
    loadDevices({ silent: true });
    loadAccount();
    loadPlaylists({ silent: true });
    pollState();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
