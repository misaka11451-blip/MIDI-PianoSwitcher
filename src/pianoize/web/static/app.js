/* ============================================================================
 * pianoize · 前端逻辑（原生 ES6，无任何依赖）
 *
 * 结构：
 *   0  后端地址
 *   1  DOM 速查 & 全局状态
 *   2  通用工具函数
 *   3  提示 / 错误 / 忙碌状态
 *   4  后端连通性探测
 *   5  上传音轨
 *   6  波形画布（高分屏绘制 / 播放头 / 框选）
 *   7  剪辑操作列表（增删改序 + 预计时长）
 *   8  方案预设（纯前端导入导出）
 *   9  执行：试听 / 完整渲染 / 结果与报告
 *   10 初始化
 *
 * 后端契约（详见 index.html 里的说明与后端代码）：
 *   POST /api/project   multipart/form-data 字段名 file
 *   POST /api/preview   { project_id, operations }
 *   POST /api/render    { project_id, operations, audio_format[], bitdepth,
 *                         want_score, want_pdf, lufs, piano, drop_dup, split }
 *   GET  /api/files/{project_id}/{filename}
 *   失败一律返回 { ok: false, error: "中文错误信息" }
 * ========================================================================== */

(function () {
  'use strict';

  /* ========================================================================
   * 0. 后端地址
   * ====================================================================== */

  /**
   * 正常使用方式：后端把本页面（以及 app.css / app.js）原样发出来，
   * 此时页面与 API 天然同源 —— 直接用相对路径最稳，端口随便改都不会坏。
   *
   * 只有一种例外：用户直接用 file:// 打开 index.html 调试。
   * 那时没有同源可言，于是回退到 index.html 里 <meta name="pianoize-backend">
   * 配置的地址（默认 127.0.0.1:8765）。注意这种模式下浏览器会做跨域检查，
   * 需要后端返回 CORS 头才通得过。
   */
  const API_BASE = (function () {
    if (location.protocol === 'http:' || location.protocol === 'https:') return '';
    const meta = document.querySelector('meta[name="pianoize-backend"]');
    const host = (meta && meta.content) ? meta.content.trim() : '127.0.0.1:8765';
    return location.protocol.replace(/^file:$/, 'http:') + '//' + host;
  })();

  /** 页面启动时展示给用户的“后端在哪”文本 */
  const API_ORIGIN_TEXT = API_BASE || location.origin;

  /* ========================================================================
   * 1. DOM 速查 & 全局状态
   * ====================================================================== */

  const $ = function (id) { return document.getElementById(id); };

  const els = {
    // 顶栏
    conn: $('conn'), connText: $('conn-text'), connDetail: $('conn-detail'),
    // 全局错误条
    alert: $('alert-error'), alertText: $('alert-error-text'), alertClose: $('alert-error-close'),
    // 上传
    dropzone: $('dropzone'), fileInput: $('file-input'),
    uploadProgress: $('upload-progress'), uploadProgressBar: $('upload-progress-bar'),
    // 元信息
    metaEmpty: $('meta-empty'), metaGrid: $('meta-grid'),
    metaFilename: $('meta-filename'), metaDuration: $('meta-duration'),
    metaTracks: $('meta-tracks'), metaNotes: $('meta-notes'),
    metaBpm: $('meta-bpm'), metaTimesig: $('meta-timesig'),
    dupList: $('dup-list'),
    // 波形与播放器
    canvas: $('wave-canvas'), selectionInfo: $('selection-info'),
    player: $('player'), transportNote: $('transport-note'),
    // 操作表单
    kindTabs: $('kind-tabs'), kindDesc: $('kind-desc'), opPreview: $('op-preview'),
    opStart: $('op-start'), opEnd: $('op-end'), opRepeat: $('op-repeat'),
    opSemitones: $('op-semitones'), opDb: $('op-db'), semitoneNote: $('semitone-note'),
    btnUseSelection: $('btn-use-selection'), btnAddOp: $('btn-add-op'),
    opError: $('op-error'),
    // 操作列表
    opList: $('op-list'), opEmpty: $('op-empty'), opsSummary: $('ops-summary'),
    opWarnings: $('op-warnings'), btnClearOps: $('btn-clear-ops'),
    // 预设
    presetName: $('preset-name'), presetNote: $('preset-note'),
    btnExportPreset: $('btn-export-preset'), btnImportPreset: $('btn-import-preset'),
    presetFile: $('preset-file'),
    // 执行
    btnPreview: $('btn-preview'), btnRender: $('btn-render'),
    busy: $('busy'), busyText: $('busy-text'), busyTimer: $('busy-timer'),
    optBitdepth: $('opt-bitdepth'), optLufs: $('opt-lufs'), optPiano: $('opt-piano'),
    optDropdup: $('opt-dropdup'), optScore: $('opt-score'), optPdf: $('opt-pdf'),
    // 结果
    resultEmpty: $('result-empty'), resultFiles: $('result-files'),
    resultScore: $('result-score'), btnOpenScore: $('btn-open-score'),
    reportWrap: $('report-wrap'), reportTable: $('report-table'),
    warningsWrap: $('warnings-wrap'), logWrap: $('log-wrap'), logText: $('log-text'),
    // 浮层
    toast: $('toast'),
  };

  /** 全局状态。整个应用只有这一份可变数据，改完记得调用对应的 render 函数。 */
  const state = {
    projectId: null,
    filename: '',
    duration: 0,        // 原始音轨时长（秒）
    nTracks: 0,
    nNotes: 0,
    bpm: 0,
    timesig: '',
    peaks: [],          // 后端给的 800 个 0~1 浮点数
    operations: [],     // 剪辑操作列表，顺序即施加顺序
    kind: 'trim',       // 当前正在新建的操作类型
    selection: null,    // { start, end } 原始时间轴上的选区（秒）
    playhead: null,     // 播放头位置，原始时间轴（秒）
    audioDuration: 0,   // 当前试听音频的时长（处理后时间轴）
    segments: null,     // 原始↔处理后 的时间轴映射（后端返回，可能为 null）
    busy: false,
    scoreUrl: null,
  };

  /** 波形画布的内部状态 */
  const wave = {
    colors: null,       // 从 CSS 变量读来的配色缓存
    drag: null,         // 当前拖拽：{ mode:'seek'|'select', ... }
    rafId: 0,           // 播放头跟随的动画帧
    programmaticSeekAt: 0, // 程序性 seek 的时间戳，用来避免事件回环
  };

  /** 允许上传的扩展名 */
  const ALLOWED_EXT = ['.mid', '.midi', '.kar', '.rmi'];

  const KIND_LABEL = {
    trim: '删除片段',
    duplicate: '复制片段',
    pitch: '升降音高',
    volume: '调整音量',
  };

  const KIND_DESC = {
    trim: '删除所选区间的音乐，后面的部分整体前移。',
    duplicate: '把所选区间的内容再演奏若干次，插在该段结束之后。',
    pitch: '把所选区间整体升高或降低若干半音（±12 半音 = 一个八度）。',
    volume: '把所选区间整体音量增减若干分贝（按振幅算，+6 dB 约等于响一倍）。',
  };

  /** 操作字段的规范顺序，用于列表里展示“即将发给后端的 JSON” */
  const OP_FIELDS = ['kind', 'start', 'end', 'repeat', 'semitones', 'db'];

  const FONT_STACK = '-apple-system, "Segoe UI", "Microsoft YaHei", sans-serif';
  const MONO_STACK = 'ui-monospace, Consolas, "Cascadia Mono", monospace';

  /* ========================================================================
   * 2. 通用工具函数
   * ====================================================================== */

  /** 把秒格式化成 3:45.2 / 1:03:05.1 这样的可读时长 */
  function formatTime(sec, digits) {
    const d = (digits === undefined) ? 1 : digits;
    let s = Number(sec);
    if (!isFinite(s) || s < 0) s = 0;
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const rest = s % 60;
    // d 位小数时整数部分至少要占 2 位：5.2 -> 05.2
    const ss = rest.toFixed(d).padStart(d > 0 ? 3 + d : 2, '0');
    return h > 0
      ? h + ':' + String(m).padStart(2, '0') + ':' + ss
      : m + ':' + ss;
  }

  /** 坐标轴刻度用的时钟文本（默认不带小数） */
  function formatClock(sec, withTenths) {
    return formatTime(sec, withTenths ? 1 : 0);
  }

  /** 人性化文件大小：7340032 -> "7.0 MB" */
  function humanSize(bytes) {
    const b = Number(bytes);
    if (!isFinite(b) || b < 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    let v = b;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    const text = (i === 0) ? String(Math.round(v)) : v.toFixed(v >= 100 ? 0 : 1);
    return text + ' ' + units[i];
  }

  /** 数值保留 3 位小数（避免 10.000000000000002 这种脏值发给后端） */
  function round3(v) {
    return Math.round(Number(v) * 1000) / 1000;
  }

  /** 像 Python 的 :g 一样输出数字：-6.0 -> "-6"，-1.0 -> "-1" */
  function fmtG(v) {
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    return String(Number(n.toFixed(6)));
  }

  /** 限制在 [lo, hi] */
  function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
  }

  /** HTML 转义（服务端文本也走一遍，避免任何注入） */
  function esc(v) {
    return String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /** 把后端给的相对路径补成可访问 URL（已经是绝对 URL 就原样返回） */
  function apiUrl(url) {
    const u = String(url || '');
    if (!u) return '';
    if (/^[a-z][a-z0-9+.-]*:/i.test(u)) return u;   // 已带协议
    return API_BASE + u;
  }

  /** 从错误响应里挖出「人能看懂」的中文信息 */
  function extractError(data, status) {
    if (data && typeof data.error === 'string' && data.error.trim()) return data.error.trim();
    if (status === 0) return '连不上后端服务。';
    return '请求失败（HTTP ' + status + '）';
  }

  function isAllowedFile(name) {
    const lower = String(name || '').toLowerCase();
    return ALLOWED_EXT.some(function (ext) { return lower.endsWith(ext); });
  }

  /* ========================================================================
   * 3. 提示 / 错误 / 忙碌状态
   * ====================================================================== */

  function showError(msg) {
    const text = String(msg || '发生未知错误');
    els.alertText.textContent = text;
    els.alert.hidden = false;
    // 错误条在顶部；用户可能已经滚到下面的执行卡片，滚回去让他看得见
    try { els.alert.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (_) {}
    showToast(text, true);
  }

  function clearError() {
    els.alert.hidden = true;
    els.alertText.textContent = '';
  }

  let toastTimer = 0;
  function showToast(msg, isError) {
    els.toast.textContent = String(msg || '');
    els.toast.classList.toggle('is-error', !!isError);
    els.toast.hidden = false;
    // 先让它进入布局，再加类，过渡才会生效
    requestAnimationFrame(function () { els.toast.classList.add('is-show'); });
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      els.toast.classList.remove('is-show');
      setTimeout(function () { els.toast.hidden = true; }, 240);
    }, isError ? 4200 : 2600);
  }

  let busyTimerId = 0;
  let busyStartedAt = 0;

  /**
   * 统一的忙碌态：禁用会发请求的按钮 + 显示计时器。
   * 渲染可能要几十秒，必须让用户看到“还在动”，否则会以为界面卡死。
   */
  function setBusy(on, text) {
    state.busy = !!on;
    els.busy.hidden = !on;
    if (text) els.busyText.textContent = text;
    els.btnPreview.disabled = !!on;
    els.btnRender.disabled = !!on;

    if (on) {
      busyStartedAt = Date.now();
      els.busyTimer.textContent = '0.0s';
      if (busyTimerId) clearInterval(busyTimerId);
      busyTimerId = setInterval(function () {
        els.busyTimer.textContent = ((Date.now() - busyStartedAt) / 1000).toFixed(1) + 's';
      }, 100);
    } else if (busyTimerId) {
      clearInterval(busyTimerId);
      busyTimerId = 0;
    }
  }

  /** 警告条（服务端 warnings 与本地预校验共用） */
  function fillWarnBox(box, list) {
    box.innerHTML = '';
    if (!Array.isArray(list) || list.length === 0) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    list.forEach(function (w) {
      const div = document.createElement('div');
      div.className = 'warn-item';
      div.appendChild(document.createTextNode(String(w)));
      box.appendChild(div);
    });
  }

  /* ========================================================================
   * 4. 后端连通性探测
   * ====================================================================== */

  function setConn(cls, text, detail) {
    els.conn.classList.remove('is-on', 'is-off', 'is-unknown');
    els.conn.classList.add('is-' + cls);
    els.connText.textContent = text;
    els.connDetail.textContent = detail || '';
    els.connDetail.title = detail || '';
  }

  /**
   * 探测后端：只要拿到任何 HTTP 响应（哪怕是 404 / 405）就说明服务活着，
   * 只有网络层抛异常才算断开 —— 这正是「只要没报错就算连上」的口径。
   */
  function probeBackend() {
    setConn('unknown', '正在检测后端…', API_ORIGIN_TEXT);
    return fetch(API_BASE + '/', { method: 'GET', cache: 'no-store' })
      .then(function (res) {
        setConn('on', '后端已连接', API_ORIGIN_TEXT + ' · HTTP ' + res.status);
        return true;
      })
      .catch(function () {
        setConn('off', '后端未连接', '请先启动后端服务：' + API_ORIGIN_TEXT);
        return false;
      });
  }

  /* ========================================================================
   * 5. 上传音轨
   * ====================================================================== */

  function bindDropzone() {
    const dz = els.dropzone;

    // 点击 / 键盘触发隐藏的文件选择框
    dz.addEventListener('click', function () { els.fileInput.click(); });
    dz.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        els.fileInput.click();
      }
    });

    els.fileInput.addEventListener('change', function () {
      const f = els.fileInput.files && els.fileInput.files[0];
      els.fileInput.value = '';   // 清空，允许再次选择同一个文件
      if (f) uploadProject(f);
    });

    // 拖拽高亮
    ['dragenter', 'dragover'].forEach(function (type) {
      dz.addEventListener(type, function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        dz.classList.add('is-over');
      });
    });
    ['dragleave', 'drop'].forEach(function (type) {
      dz.addEventListener(type, function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        // dragleave 在子元素之间移动时也会触发，用 relatedTarget 过滤掉噪声
        if (type === 'dragleave' && dz.contains(ev.relatedTarget)) return;
        dz.classList.remove('is-over');
      });
    });

    dz.addEventListener('drop', function (ev) {
      const dt = ev.dataTransfer;
      const f = dt && dt.files && dt.files[0];
      if (f) uploadProject(f);
    });

    // 拖到页面其他地方时，阻止浏览器直接打开文件（否则会丢掉当前页面）
    ['dragover', 'drop'].forEach(function (type) {
      window.addEventListener(type, function (ev) {
        if (ev.target === dz || dz.contains(ev.target)) return;
        ev.preventDefault();
      });
    });
  }

  /**
   * 上传音轨。用 XMLHttpRequest 而不是 fetch，因为只有 XHR 能拿到上传进度事件。
   */
  function uploadProject(file) {
    if (!isAllowedFile(file.name)) {
      showError('只支持 MIDI 类文件（.mid / .midi / .kar / .rmi）。音频输入（wav / mp3）暂不支持。');
      return;
    }
    if (state.busy) return;

    clearError();
    setBusy(true, '正在上传并解析 ' + file.name + '…');
    els.dropzone.classList.add('is-busy');
    els.uploadProgress.hidden = false;
    els.uploadProgressBar.style.width = '0%';

    const cleanup = function () {
      setBusy(false);
      els.dropzone.classList.remove('is-busy');
      els.uploadProgress.hidden = true;
      els.uploadProgressBar.style.width = '0%';
    };

    const fd = new FormData();
    fd.append('file', file, file.name);   // 字段名必须是 "file"

    const xhr = new XMLHttpRequest();
    xhr.open('POST', API_BASE + '/api/project', true);
    xhr.responseType = 'text';

    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable && ev.total > 0) {
        els.uploadProgressBar.style.width = ((ev.loaded / ev.total) * 100).toFixed(1) + '%';
        els.busyText.textContent = '正在上传 ' + file.name + '…';
      }
    };
    xhr.upload.onload = function () {
      // 上传完毕，剩下的时间都花在服务端解析上
      els.busyText.textContent = '正在解析 ' + file.name + '…（大文件可能要几秒）';
      els.uploadProgressBar.style.width = '100%';
    };

    xhr.onload = function () {
      cleanup();
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { data = null; }
      if (xhr.status < 200 || xhr.status >= 300 || !data || data.ok === false) {
        showError(extractError(data, xhr.status));
        return;
      }
      handleProject(data);
      showToast('音轨已载入：' + (data.filename || file.name));
    };

    xhr.onerror = function () {
      cleanup();
      showError('上传失败：连不上后端服务（' + API_ORIGIN_TEXT + '）。请确认后端已经在运行。');
    };
    xhr.ontimeout = function () {
      cleanup();
      showError('上传超时：后端长时间没有响应。');
    };

    xhr.send(fd);
  }

  /** 拿到 /api/project 的响应后，刷新整页的音轨相关信息 */
  function handleProject(data) {
    state.projectId = data.project_id || null;
    state.filename = data.filename || '';
    state.duration = Number(data.duration) || 0;
    state.nTracks = Number(data.n_tracks) || 0;
    state.nNotes = Number(data.n_notes) || 0;
    state.bpm = Number(data.bpm) || 0;
    state.timesig = data.time_signature || '';
    state.peaks = Array.isArray(data.peaks) ? data.peaks : [];

    // 换了音轨，旧的时间轴相关状态一律作废
    state.selection = null;
    state.playhead = null;
    state.audioDuration = 0;
    state.segments = null;

    // 停掉并清空播放器，避免拿着上一首的音频继续放
    try {
      els.player.pause();
      els.player.removeAttribute('src');
      els.player.load();
    } catch (_) {}
    els.transportNote.textContent = '试听音频会在点击「试听」后出现在这里。';

    renderMeta();
    renderDuplicates(data.duplicate_tracks);
    renderSelectionInfo();
    renderOperations();   // 顺带刷新预计时长与本地预校验
    drawWave();

    // 服务端在解析阶段给出的 warnings（比如“某些轨道没有音符”）
    if (Array.isArray(data.warnings) && data.warnings.length) {
      fillWarnBox(els.warningsWrap, data.warnings);
    } else {
      fillWarnBox(els.warningsWrap, []);
    }

    if (!state.projectId) {
      showError('后端没有返回 project_id，后续操作无法进行。');
    }
  }

  function renderMeta() {
    const has = !!state.projectId;
    els.metaEmpty.hidden = has;
    els.metaGrid.hidden = !has;
    if (!has) return;

    els.metaFilename.textContent = state.filename || '—';
    els.metaFilename.title = state.filename || '';
    els.metaDuration.textContent = formatTime(state.duration);
    els.metaTracks.textContent = String(state.nTracks);
    els.metaNotes.textContent = String(state.nNotes);
    els.metaBpm.textContent = state.bpm ? state.bpm.toFixed(1) : '—';
    els.metaTimesig.textContent = state.timesig || '—';
  }

  function renderDuplicates(list) {
    els.dupList.innerHTML = '';
    if (!Array.isArray(list) || list.length === 0) {
      els.dupList.hidden = true;
      return;
    }
    els.dupList.hidden = false;

    const head = document.createElement('div');
    head.className = 'dup-item';
    head.innerHTML = '<strong>检测到 ' + list.length + ' 组疑似重复轨</strong>' +
      '（渲染时按「重复轨处理」选项决定是否丢弃）';
    els.dupList.appendChild(head);

    list.forEach(function (d) {
      const row = document.createElement('div');
      row.className = 'dup-item';
      const sim = (d && typeof d.similarity === 'number')
        ? (d.similarity * 100).toFixed(0) + '%'
        : '—';
      row.innerHTML =
        '轨道 <span class="mono">' + esc(d && d.a) + '</span> 与轨道 ' +
        '<span class="mono">' + esc(d && d.b) + '</span> 相似度 ' +
        '<span class="mono">' + esc(sim) + '</span>，保留 ' +
        '<span class="mono">' + esc(d && d.keep) + '</span>、丢弃 ' +
        '<span class="mono">' + esc(d && d.drop) + '</span>';
      els.dupList.appendChild(row);
    });
  }

  /* ========================================================================
   * 6. 波形画布
   * ====================================================================== */

  /** 画布内边距等常量（CSS 像素） */
  const GEOM = { padX: 14, rulerH: 26, gap: 8, axisH: 22 };

  /** 从 CSS 变量里读配色，保证「改主题只改 app.css」 */
  function getColors() {
    if (wave.colors) return wave.colors;
    const cs = getComputedStyle(document.documentElement);
    const g = function (name, fallback) {
      const v = (cs.getPropertyValue(name) || '').trim();
      return v || fallback;
    };
    wave.colors = {
      bg: g('--wave-bg', '#0f1217'),
      bar: g('--wave-bar', '#4a566a'),
      barHi: g('--wave-bar-hi', '#8ea6cc'),
      grid: g('--grid-line', 'rgba(255,255,255,0.05)'),
      axis: g('--axis-text', '#6f7a8b'),
      ruler: g('--ruler-bg', '#12161c'),
      head: g('--playhead', '#ff7a59'),
      veil: g('--played-veil', 'rgba(255,255,255,0.045)'),
      selFill: g('--sel-fill', 'rgba(91,140,255,0.20)'),
      selEdge: g('--sel-edge', '#5b8cff'),
      mute: g('--text-mute', '#6f7a8b'),
    };
    return wave.colors;
  }

  /** 当前画布的几何布局（全部用 CSS 像素，绘制时再乘 dpr） */
  function getGeom() {
    const cv = els.canvas;
    const cssW = cv.clientWidth || cv.getBoundingClientRect().width || 600;
    const cssH = cv.clientHeight || 160;
    const x0 = GEOM.padX;
    const x1 = Math.max(x0 + 20, cssW - GEOM.padX);
    const rulerBottom = GEOM.rulerH;
    const waveTop = rulerBottom + GEOM.gap;
    const waveBottom = Math.max(waveTop + 24, cssH - GEOM.axisH);
    return {
      cssW: cssW, cssH: cssH,
      x0: x0, x1: x1,
      rulerTop: 0, rulerBottom: rulerBottom,
      waveTop: waveTop, waveBottom: waveBottom,
      width: x1 - x0,
    };
  }

  function timeToX(t) {
    const g = getGeom();
    if (!(state.duration > 0)) return g.x0;
    return g.x0 + (clamp(Number(t) || 0, 0, state.duration) / state.duration) * g.width;
  }

  function xToTime(x) {
    const g = getGeom();
    if (!(state.duration > 0) || g.width <= 0) return 0;
    return clamp((x - g.x0) / g.width, 0, 1) * state.duration;
  }

  /**
   * 选一个「好看的」刻度间隔：优先 1/2/5/10/15/30/60 秒这类整数，
   * 同时保证相邻刻度之间至少有 78px，文字才不会叠在一起。
   */
  function pickTickStep(dur, pxWidth) {
    const minPx = 78;
    const maxTicks = Math.max(2, Math.floor(pxWidth / minPx));
    const raw = dur / maxTicks;
    const cands = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    for (let i = 0; i < cands.length; i += 1) {
      if (cands[i] >= raw) return cands[i];
    }
    return cands[cands.length - 1];
  }

  /* ---------------------------- 绘制 ---------------------------- */

  function drawWave() {
    const cv = els.canvas;
    if (!cv) return;
    const ctx = cv.getContext('2d');
    if (!ctx) return;

    const col = getColors();
    // 高分屏：把画布后备存储放大 dpr 倍，再整体缩放坐标系，避免糊
    const dpr = clamp(window.devicePixelRatio || 1, 1, 3);
    const g = getGeom();
    const pxW = Math.max(1, Math.round(g.cssW * dpr));
    const pxH = Math.max(1, Math.round(g.cssH * dpr));
    if (cv.width !== pxW || cv.height !== pxH) {
      cv.width = pxW;
      cv.height = pxH;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, g.cssW, g.cssH);

    // 底：波形区 + 顶部刻度条
    ctx.fillStyle = col.bg;
    ctx.fillRect(0, 0, g.cssW, g.cssH);
    ctx.fillStyle = col.ruler;
    ctx.fillRect(0, g.rulerTop, g.cssW, g.rulerBottom - g.rulerTop);

    const hasData = state.peaks.length > 0 && state.duration > 0;

    if (!hasData) {
      ctx.fillStyle = col.mute;
      ctx.font = '12px ' + FONT_STACK;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('上传 MIDI 后在这里显示波形', g.cssW / 2, (g.waveTop + g.waveBottom) / 2);
      ctx.textAlign = 'left';
      drawRulerAndGrid(ctx, g, col, 0);
      return;
    }

    const n = state.peaks.length;
    const barW = g.width / n;
    const midY = (g.waveTop + g.waveBottom) / 2;
    const half = Math.max(6, (g.waveBottom - g.waveTop) / 2 - 1);
    const gapPx = barW > 3 ? Math.min(1.5, barW * 0.28) : 0;
    const headX = (state.playhead === null) ? null : timeToX(state.playhead);

    // 播放头扫过的区域压一层极淡的白，形成“已播过”的进度感
    if (headX !== null && headX > g.x0) {
      ctx.fillStyle = col.veil;
      ctx.fillRect(g.x0, g.waveTop, headX - g.x0, g.waveBottom - g.waveTop);
    }

    // 波形柱：以中线上下镜像
    for (let i = 0; i < n; i += 1) {
      let p = Number(state.peaks[i]);
      if (!isFinite(p)) p = 0;
      p = clamp(p, 0, 1);
      const h = Math.max(1, p * half);
      const x = g.x0 + i * barW;
      const w = Math.max(1, barW - gapPx);
      const passed = (headX !== null) && (x + w / 2 <= headX);
      ctx.fillStyle = passed ? col.barHi : col.bar;
      ctx.fillRect(x, midY - h, w, h * 2);
    }

    // 刻度与网格（画在波形之上，用极淡的颜色，不抢主体）
    drawRulerAndGrid(ctx, g, col, state.duration);

    // 选区：半透明填充 + 两条边界线
    const sel = state.selection;
    if (sel && Math.abs(sel.end - sel.start) > 1e-6) {
      const a = timeToX(Math.min(sel.start, sel.end));
      const b = timeToX(Math.max(sel.start, sel.end));
      const w = Math.max(1, b - a);
      ctx.fillStyle = col.selFill;
      ctx.fillRect(a, g.waveTop, w, g.waveBottom - g.waveTop);
      ctx.strokeStyle = col.selEdge;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(Math.round(a) + 0.5, g.waveTop);
      ctx.lineTo(Math.round(a) + 0.5, g.waveBottom);
      ctx.moveTo(Math.round(b) - 0.5, g.waveTop);
      ctx.lineTo(Math.round(b) - 0.5, g.waveBottom);
      ctx.stroke();
      // 刻度条上也标一段，一眼能看出选区在哪
      ctx.fillStyle = col.selEdge;
      ctx.fillRect(a, g.rulerTop, w, 2.5);
    }

    // 播放头：竖线 + 刻度条上的小三角（提示这里可以拖）
    if (headX !== null) {
      const hx = Math.round(headX) + 0.5;
      ctx.strokeStyle = col.head;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(hx, g.rulerBottom - 6);
      ctx.lineTo(hx, g.waveBottom);
      ctx.stroke();

      ctx.fillStyle = col.head;
      ctx.beginPath();
      ctx.moveTo(hx - 5, g.rulerBottom - 12);
      ctx.lineTo(hx + 5, g.rulerBottom - 12);
      ctx.lineTo(hx, g.rulerBottom - 5);
      ctx.closePath();
      ctx.fill();
    }
  }

  /** 时间轴刻度：竖网格线 + 刻度文字 + 刻度条下边线 */
  function drawRulerAndGrid(ctx, g, col, dur) {
    ctx.save();
    ctx.strokeStyle = col.grid;
    ctx.lineWidth = 1;

    if (dur > 0 && g.width > 0) {
      const step = pickTickStep(dur, g.width);

      ctx.beginPath();
      for (let t = 0; t <= dur + 1e-9; t += step) {
        const x = Math.round(timeToX(t)) + 0.5;
        ctx.moveTo(x, g.waveTop);
        ctx.lineTo(x, g.waveBottom);
      }
      ctx.stroke();

      ctx.font = '10px ' + MONO_STACK;
      ctx.textBaseline = 'top';
      ctx.textAlign = 'left';
      ctx.fillStyle = col.axis;
      for (let t = 0; t <= dur + 1e-9; t += step) {
        const x = timeToX(t);
        const label = formatClock(t, step < 1);
        const w = ctx.measureText(label).width;
        // 最后一个刻度贴着右边界时，文字改为向左对齐，避免被裁掉
        const tx = clamp(x + 3, 2, Math.max(2, g.cssW - w - 2));
        ctx.fillText(label, tx, g.rulerTop + 5);
      }
    }

    // 刻度条与波形区的分界线
    ctx.strokeStyle = col.grid;
    ctx.beginPath();
    const yb = Math.round(g.rulerBottom) + 0.5;
    ctx.moveTo(0, yb);
    ctx.lineTo(g.cssW, yb);
    ctx.stroke();
    ctx.restore();
  }

  /* --------------------------- 交互 --------------------------- */

  function localPos(ev) {
    const r = els.canvas.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  }

  function renderSelectionInfo() {
    const el = els.selectionInfo;
    const sel = state.selection;
    if (!sel || sel.end - sel.start < 1e-6) {
      el.textContent = '未选择区间';
      el.classList.remove('has-sel');
      return;
    }
    el.textContent = '选区 ' + sel.start.toFixed(2) + 's – ' + sel.end.toFixed(2) + 's（长 ' +
      (sel.end - sel.start).toFixed(2) + 's）';
    el.classList.add('has-sel');
  }

  /**
   * 移动播放头，并（如果已有试听音频）把音频也跳到对应位置。
   *
   * 注意时间轴的差异：波形画的是**原始** MIDI，而试听音频是施加剪辑之后的
   * 结果，长度可能因为 trim / duplicate 变了。所以这里走 mapSrcToOut()，
   * 用后端返回的 segments 做精确换算；后端没给 segments 时才退回比例映射。
   */
  function seekToTime(t) {
    if (!(state.duration > 0)) return;
    state.playhead = clamp(Number(t) || 0, 0, state.duration);
    syncAudioToPlayhead();
    drawWave();
  }

  /* ------------------------------------------------------------------ *
   * 时间轴映射：原始 ↔ 处理后
   *
   * 波形画的是**原始** MIDI 的峰值，而试听音频是**施加剪辑之后**的结果 ——
   * 长度可能不同（trim 变短、duplicate 变长）。所以播放头和音频时间之间
   * 不能按比例硬凑，必须按后端给的 segments 精确换算。
   *
   * segments 形如 [{out_start, out_end, src_start, src_end}, ...]：
   * 处理后的 [out_start,out_end) 对应原始的 [src_start,src_end)，段内线性。
   * 后端没给（没剪辑、或操作太碎）时退回比例映射，保证不会比原来更差。
   * ------------------------------------------------------------------ */
  function mapSrcToOut(srcSec) {
    const segs = state.segments;
    if (!Array.isArray(segs) || segs.length === 0) {
      return state.duration > 0
        ? (srcSec / state.duration) * state.audioDuration
        : srcSec;
    }
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      if (srcSec >= s.src_start && srcSec <= s.src_end) {
        const span = s.src_end - s.src_start;
        const t = span > 0 ? (srcSec - s.src_start) / span : 0;
        return s.out_start + t * (s.out_end - s.out_start);
      }
    }
    // 落在被删掉的区间里：贴到最近的段边界，避免播放头乱跳
    let best = segs[0];
    let bestD = Infinity;
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      const d = Math.min(Math.abs(srcSec - s.src_start), Math.abs(srcSec - s.src_end));
      if (d < bestD) { bestD = d; best = s; }
    }
    return srcSec < best.src_start ? best.out_start : best.out_end;
  }

  function mapOutToSrc(outSec) {
    const segs = state.segments;
    if (!Array.isArray(segs) || segs.length === 0) {
      return state.audioDuration > 0
        ? (outSec / state.audioDuration) * state.duration
        : outSec;
    }
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      if (outSec >= s.out_start && outSec <= s.out_end) {
        const span = s.out_end - s.out_start;
        const t = span > 0 ? (outSec - s.out_start) / span : 0;
        return s.src_start + t * (s.src_end - s.src_start);
      }
    }
    let best = segs[0];
    let bestD = Infinity;
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      const d = Math.min(Math.abs(outSec - s.out_start), Math.abs(outSec - s.out_end));
      if (d < bestD) { bestD = d; best = s; }
    }
    return outSec < best.out_start ? best.src_start : best.src_end;
  }

  function syncAudioToPlayhead() {
    const a = els.player;
    if (!a.src) return;
    if (!isFinite(a.duration) || a.duration <= 0) return;
    if (!(state.duration > 0) || state.playhead === null) return;
    const target = mapSrcToOut(state.playhead);
    if (!isFinite(target)) return;
    wave.programmaticSeekAt = Date.now();
    try { a.currentTime = clamp(target, 0, Math.max(0, a.duration - 0.02)); } catch (_) {}
  }

  /** 播放中的动画帧：让播放头跟着音频走 */
  function tickPlayhead() {
    const a = els.player;
    if (state.duration > 0 && a.src && isFinite(a.duration) && a.duration > 0) {
      state.playhead = clamp(mapOutToSrc(a.currentTime), 0, state.duration);
      drawWave();
    }
    if (!a.paused && !a.ended) {
      wave.rafId = requestAnimationFrame(tickPlayhead);
    }
  }

  function bindCanvas() {
    const cv = els.canvas;

    cv.addEventListener('pointerdown', function (ev) {
      if (!(state.duration > 0)) return;
      if (ev.pointerType === 'mouse' && ev.button !== 0) return;
      const g = getGeom();
      const p = localPos(ev);
      ev.preventDefault();
      try { cv.setPointerCapture(ev.pointerId); } catch (_) {}

      if (p.y <= g.rulerBottom) {
        // 刻度条：单击/拖动 = 移动播放头
        wave.drag = { mode: 'seek', pointerId: ev.pointerId };
        seekToTime(xToTime(p.x));
        return;
      }

      // 波形区：先按「框选」处理；如果松手时几乎没移动，再当成单击定位
      const t = xToTime(p.x);
      wave.drag = { mode: 'select', pointerId: ev.pointerId, anchor: t, startX: p.x, moved: false };
      state.selection = { start: t, end: t };
      renderSelectionInfo();
      drawWave();
    });

    cv.addEventListener('pointermove', function (ev) {
      if (!(state.duration > 0)) return;
      const d = wave.drag;
      if (!d || d.pointerId !== ev.pointerId) return;
      const p = localPos(ev);

      if (d.mode === 'seek') {
        seekToTime(xToTime(p.x));
        return;
      }
      if (Math.abs(p.x - d.startX) > 3) d.moved = true;
      const t = xToTime(p.x);
      state.selection = { start: Math.min(d.anchor, t), end: Math.max(d.anchor, t) };
      renderSelectionInfo();
      drawWave();
    });

    const endDrag = function (ev) {
      const d = wave.drag;
      if (!d) return;
      if (ev && d.pointerId !== undefined && ev.pointerId !== d.pointerId) return;
      wave.drag = null;

      if (d.mode !== 'select') return;

      if (!d.moved) {
        // 单击波形 = 移动播放头，不留下选区
        state.selection = null;
        renderSelectionInfo();
        seekToTime(d.anchor);
        return;
      }

      const sel = state.selection;
      if (!sel || sel.end - sel.start < 0.01) {
        state.selection = null;
        renderSelectionInfo();
        showToast('框选太短了：至少要 0.01 秒');
      } else {
        applySelectionToForm();
      }
      drawWave();
    };

    cv.addEventListener('pointerup', endDrag);
    cv.addEventListener('pointercancel', endDrag);

    // 尺寸变化（窗口缩放 / 手机旋转 / 侧栏塌陷）后重画，保持清晰
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(function () { drawWave(); });
      ro.observe(cv);
    } else {
      window.addEventListener('resize', function () { drawWave(); });
    }
  }

  /** 把当前选区填进操作表单的起点/终点 */
  function applySelectionToForm() {
    const sel = state.selection;
    if (!sel || sel.end - sel.start < 0.01) {
      showToast('先在波形上拖出一段区间，或用「用当前选区」按钮');
      return;
    }
    els.opStart.value = sel.start.toFixed(2);
    els.opEnd.value = sel.end.toFixed(2);
    updateOpPreview();
    showToast('已填入选区：' + sel.start.toFixed(2) + 's – ' + sel.end.toFixed(2) + 's');
  }

  function bindPlayer() {
    const a = els.player;

    a.addEventListener('play', function () {
      if (wave.rafId) cancelAnimationFrame(wave.rafId);
      wave.rafId = requestAnimationFrame(tickPlayhead);
    });
    a.addEventListener('pause', function () {
      if (wave.rafId) { cancelAnimationFrame(wave.rafId); wave.rafId = 0; }
    });
    a.addEventListener('ended', function () {
      if (wave.rafId) { cancelAnimationFrame(wave.rafId); wave.rafId = 0; }
      if (state.duration > 0) {
        state.playhead = state.duration;
        drawWave();
      }
    });
    a.addEventListener('loadedmetadata', function () {
      state.audioDuration = isFinite(a.duration) ? a.duration : 0;
      updateTransportNote();
    });
    a.addEventListener('seeked', function () {
      // 程序性 seek 会立刻回调，忽略掉以免和用户拖动打架
      if (Date.now() - wave.programmaticSeekAt < 250) return;
      if (!(state.duration > 0) || !isFinite(a.duration) || a.duration <= 0) return;
      state.playhead = clamp((a.currentTime / a.duration) * state.duration, 0, state.duration);
      drawWave();
    });
    a.addEventListener('error', function () {
      if (!a.src) return;
      showError('音频加载失败：可以试试重新点「试听」生成一次。');
    });
  }

  let lastTransportInfo = '';
  function setTransportInfo(text) {
    lastTransportInfo = text;
    updateTransportNote();
  }
  function updateTransportNote() {
    if (!lastTransportInfo) return;
    els.transportNote.textContent = lastTransportInfo;
  }

  /* ========================================================================
   * 7. 剪辑操作列表
   * ====================================================================== */

  /**
   * 生成操作的中文描述。
   * 措辞与后端 pianoize.ops.Operation.describe() 保持一致，
   * 这样列表里看到的和「执行报告」里看到的不会出现两种说法。
   */
  function describeOp(op) {
    const a = Number(op.start) || 0;
    const b = Number(op.end) || 0;
    const rng = a.toFixed(2) + 's–' + b.toFixed(2) + 's';
    const len = Math.max(0, b - a);

    if (op.kind === 'trim') {
      return '删除 ' + rng + '（' + len.toFixed(2) + 's）';
    }
    if (op.kind === 'duplicate') {
      const rep = Number(op.repeat) || 1;
      const extra = rep > 1 ? ' ×' + rep : '';
      return '复制 ' + rng + ' 再播 ' + rep + ' 次（多出 ' + (len * rep).toFixed(2) + 's）';
    }
    if (op.kind === 'pitch') {
      const s = Number(op.semitones) || 0;
      const sign = s >= 0 ? '+' : '';
      return '区间 ' + rng + ' 音高 ' + sign + s + ' 半音（' + sign + fmtG(s / 12) + ' 个八度）';
    }
    if (op.kind === 'volume') {
      const d = Number(op.db) || 0;
      const sign = d >= 0 ? '+' : '';
      return '区间 ' + rng + ' 音量 ' + sign + fmtG(d) + ' dB';
    }
    return String(op.kind) + ' ' + rng;
  }

  /** 只保留后端认识的字段，并按固定顺序输出成紧凑 JSON */
  function opToPayload(op) {
    const out = {};
    OP_FIELDS.forEach(function (k) {
      if (op[k] !== undefined) out[k] = op[k];
    });
    return out;
  }

  /** 列表里每行下方那串“原样发给后端的东西” */
  function opSummaryLine(op) {
    return JSON.stringify(opToPayload(op));
  }

  /**
   * 读取新建操作表单并做本地校验。
   * 校验口径与后端 pianoize.ops.Operation.__post_init__ 对齐：
   * 区间必须非零长度、repeat ≥ 1 等，尽量在本地就拦住，省一次往返。
   * @throws {Error} 校验不通过时抛出中文原因
   */
  function readOperationForm() {
    const kind = state.kind;
    if (!KIND_LABEL[kind]) throw new Error('未知的操作类型');

    const rawStart = els.opStart.value.trim();
    const rawEnd = els.opEnd.value.trim();
    if (!rawStart) throw new Error('请填写起点时间（秒）');
    if (!rawEnd) throw new Error('请填写终点时间（秒）');

    const start = Number(rawStart);
    const end = Number(rawEnd);
    if (!isFinite(start)) throw new Error('起点必须是数字');
    if (!isFinite(end)) throw new Error('终点必须是数字');
    if (start < 0) throw new Error('起点不能是负数');
    if (end < start) throw new Error('终点不能小于起点');
    if (end - start <= 1e-6) throw new Error('区间长度必须大于 0');

    const op = { kind: kind, start: round3(start), end: round3(end) };

    if (kind === 'duplicate') {
      const r = Number(els.opRepeat.value);
      if (!isFinite(r)) throw new Error('重复次数必须是数字');
      if (r < 1) throw new Error('重复次数至少为 1');
      op.repeat = Math.max(1, Math.round(r));
    }
    if (kind === 'pitch') {
      const s = Number(els.opSemitones.value);
      if (!isFinite(s)) throw new Error('升降半音数必须是数字');
      op.semitones = Math.round(s);
    }
    if (kind === 'volume') {
      const d = Number(els.opDb.value);
      if (!isFinite(d)) throw new Error('增减分贝数必须是数字');
      op.db = round3(d);
    }
    return op;
  }

  function updateOpPreview() {
    const el = els.opPreview;
    if (!el) return;
    if (!els.opStart.value.trim() && !els.opEnd.value.trim()) {
      el.textContent = '';
      return;
    }
    try {
      el.textContent = '将添加 → ' + describeOp(readOperationForm());
    } catch (err) {
      el.textContent = '还差一点：' + (err && err.message ? err.message : '输入不完整');
    }
  }

  function updateSemitoneNote() {
    const s = Number(els.opSemitones.value);
    if (!isFinite(s)) {
      els.semitoneNote.textContent = '±12 半音 = 一个八度';
      return;
    }
    els.semitoneNote.textContent =
      (s >= 0 ? '+' : '') + s + ' 半音 = ' + (s >= 0 ? '+' : '') + fmtG(s / 12) + ' 个八度';
  }

  function setKind(kind) {
    if (!KIND_LABEL[kind]) return;
    state.kind = kind;
    Array.prototype.forEach.call(els.kindTabs.querySelectorAll('.kind-tab'), function (b) {
      b.classList.toggle('is-active', b.dataset.kind === kind);
    });
    els.kindDesc.textContent = KIND_DESC[kind] || '';
    Array.prototype.forEach.call(document.querySelectorAll('.kind-extra'), function (box) {
      box.hidden = box.dataset.for !== kind;
    });
    els.opError.textContent = '';
    updateOpPreview();
  }

  /** 估算所有操作对总时长的影响（与后端 ops.total_shift_seconds 同口径） */
  function estimateDuration() {
    if (!(state.duration > 0)) return null;
    let delta = 0;
    state.operations.forEach(function (op) {
      const len = Math.max(0, (Number(op.end) || 0) - (Number(op.start) || 0));
      if (op.kind === 'duplicate') delta += len * (Number(op.repeat) || 1);
      else if (op.kind === 'trim') delta -= len;
    });
    return { delta: delta, total: Math.max(0, state.duration + delta) };
  }

  /** 本地预校验，措辞与后端 ops.validate_against_duration 对齐（不阻断，只提醒） */
  function clientWarnings() {
    const dur = state.duration;
    if (!(dur > 0)) return [];
    const out = [];
    state.operations.forEach(function (op, i) {
      const tag = '第 ' + (i + 1) + ' 条（' + (KIND_LABEL[op.kind] || op.kind) + '）';
      if (op.start > dur + 1e-6) {
        out.push(tag + '：起点 ' + op.start.toFixed(2) + 's 已经超过曲子长度 ' + dur.toFixed(2) + 's');
      } else if (op.end > dur + 1e-6) {
        out.push(tag + '：终点 ' + op.end.toFixed(2) + 's 超出曲子长度 ' + dur.toFixed(2) +
          's，会被按 ' + dur.toFixed(2) + 's 处理');
      }
      if (op.kind === 'pitch' && op.semitones === 0) out.push(tag + '：移调 0 半音，等于没改');
      if (op.kind === 'volume' && Math.abs(op.db) < 1e-9) out.push(tag + '：音量变化 0 dB，等于没改');
      if (op.kind === 'pitch' && Math.abs(op.semitones) >= 24) {
        out.push(tag + '：移调 ' + op.semitones + ' 半音幅度很大，超出音域的音会被钳住，旋律会变形');
      }
    });
    return out;
  }

  function iconBtn(label, title, disabled, onClick, danger) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'icon-btn' + (danger ? ' is-danger' : '');
    b.textContent = label;
    b.title = title;
    b.setAttribute('aria-label', title);
    b.disabled = !!disabled;
    b.addEventListener('click', onClick);
    return b;
  }

  function renderOperations() {
    const list = els.opList;
    const ops = state.operations;
    list.innerHTML = '';
    els.opEmpty.hidden = ops.length > 0;

    ops.forEach(function (op, i) {
      const row = document.createElement('div');
      row.className = 'op-row';

      const idx = document.createElement('span');
      idx.className = 'op-index';
      idx.textContent = String(i + 1);
      idx.title = '第 ' + (i + 1) + ' 步施加';
      row.appendChild(idx);

      const tag = document.createElement('span');
      tag.className = 'op-kind-tag';
      tag.dataset.kind = op.kind;
      tag.textContent = KIND_LABEL[op.kind] || op.kind;
      row.appendChild(tag);

      const main = document.createElement('div');
      main.className = 'op-main';
      const text = document.createElement('span');
      text.className = 'op-text';
      text.textContent = describeOp(op);
      main.appendChild(text);
      const sub = document.createElement('span');
      sub.className = 'op-sub';
      sub.textContent = opSummaryLine(op);
      main.appendChild(sub);
      row.appendChild(main);

      const actions = document.createElement('div');
      actions.className = 'op-actions';
      actions.appendChild(iconBtn('↑', '上移（更早施加）', i === 0, function () { moveOp(i, -1); }));
      actions.appendChild(iconBtn('↓', '下移（更晚施加）', i === ops.length - 1, function () { moveOp(i, 1); }));
      actions.appendChild(iconBtn('✕', '删除这一条', false, function () { removeOp(i); }, true));
      row.appendChild(actions);

      list.appendChild(row);
    });

    renderOpsSummary();
    renderOpWarnings();
  }

  function renderOpsSummary() {
    const n = state.operations.length;
    const est = estimateDuration();
    if (!est) {
      els.opsSummary.textContent = '共 ' + n + ' 条操作';
      return;
    }
    const sign = est.delta >= 0 ? '+' : '−';
    els.opsSummary.textContent = '共 ' + n + ' 条操作 · 预计时长 ' + formatTime(est.total) +
      '（' + sign + Math.abs(est.delta).toFixed(1) + 's）';
  }

  function renderOpWarnings() {
    fillWarnBox(els.opWarnings, clientWarnings());
  }

  function moveOp(index, dir) {
    const ops = state.operations;
    const target = index + dir;
    if (index < 0 || index >= ops.length) return;
    if (target < 0 || target >= ops.length) return;
    const tmp = ops[index];
    ops[index] = ops[target];
    ops[target] = tmp;
    renderOperations();
    showToast('已' + (dir < 0 ? '上移' : '下移') + '到第 ' + (target + 1) + ' 步');
  }

  function removeOp(index) {
    if (index < 0 || index >= state.operations.length) return;
    state.operations.splice(index, 1);
    renderOperations();
  }

  function bindOperationForm() {
    // 操作类型切换
    els.kindTabs.addEventListener('click', function (ev) {
      const btn = ev.target.closest ? ev.target.closest('.kind-tab') : null;
      if (!btn) return;
      setKind(btn.dataset.kind);
    });

    // 快捷数值按钮（-12 / -1 / +1 / +12、-6 / -3 / +3 / +6、重复次数）
    Array.prototype.forEach.call(document.querySelectorAll('.quick-btn'), function (b) {
      b.addEventListener('click', function () {
        const target = document.getElementById(b.dataset.target);
        if (!target) return;
        target.value = b.dataset.value;
        target.dispatchEvent(new Event('input', { bubbles: true }));
        target.focus();
      });
    });

    // 表单变化时刷新「将添加 → …」预览
    [els.opStart, els.opEnd, els.opRepeat, els.opDb].forEach(function (input) {
      input.addEventListener('input', function () {
        els.opError.textContent = '';
        updateOpPreview();
      });
    });
    els.opSemitones.addEventListener('input', function () {
      updateSemitoneNote();
      updateOpPreview();
    });

    els.btnUseSelection.addEventListener('click', applySelectionToForm);

    els.btnAddOp.addEventListener('click', function () {
      els.opError.textContent = '';
      let op = null;
      try {
        op = readOperationForm();
      } catch (err) {
        els.opError.textContent = err.message;
        return;
      }

      // 超出曲子长度只警告不拦截 —— 用户可能故意先写好再换文件
      state.operations.push(op);
      renderOperations();

      const over = (state.duration > 0) && (op.end > state.duration + 1e-6);
      showToast(over
        ? '已添加第 ' + state.operations.length + ' 条（终点超出曲子长度，执行时会按结尾处理）'
        : '已添加第 ' + state.operations.length + ' 条：' + describeOp(op));

      updateOpPreview();
    });

    els.btnClearOps.addEventListener('click', function () {
      if (state.operations.length === 0) {
        showToast('列表已经是空的了');
        return;
      }
      if (!window.confirm('确定要清空全部 ' + state.operations.length + ' 条操作吗？')) return;
      state.operations = [];
      renderOperations();
      showToast('操作列表已清空');
    });
  }

  function collectOperations() {
    return state.operations.map(opToPayload);
  }

  /* ========================================================================
   * 8. 方案预设（纯前端：Blob 导出 + FileReader 导入）
   * ====================================================================== */

  /**
   * 校验并规范化预设。
   *
   * 额外做了一件事：把每条操作**只保留后端认识的字段**。
   * 因为后端 Operation.from_dict 遇到未知字段会直接报错，
   * 手工编辑过的预设很可能带了个 label / comment 之类的多余键，
   * 在这里洗掉比让后端报“无法识别的字段”友好得多。
   */
  function validatePreset(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      throw new Error('预设必须是一个 JSON 对象');
    }
    if (data.operations === undefined) throw new Error('预设里缺少 operations 字段');
    if (!Array.isArray(data.operations)) throw new Error('operations 必须是数组');

    const ops = data.operations.map(function (o, i) {
      const n = i + 1;
      if (!o || typeof o !== 'object' || Array.isArray(o)) throw new Error('第 ' + n + ' 条操作不是对象');
      if (!KIND_LABEL[o.kind]) {
        throw new Error('第 ' + n + ' 条操作的 kind 非法：' + JSON.stringify(o.kind) +
          '（只能是 trim / duplicate / pitch / volume）');
      }
      const start = Number(o.start);
      const end = Number(o.end);
      if (!isFinite(start)) throw new Error('第 ' + n + ' 条操作的 start 必须是数字');
      if (!isFinite(end)) throw new Error('第 ' + n + ' 条操作的 end 必须是数字');
      if (start < 0) throw new Error('第 ' + n + ' 条操作的 start 不能是负数');
      if (end < start) throw new Error('第 ' + n + ' 条操作的 end 不能小于 start');
      if (end - start <= 1e-6) throw new Error('第 ' + n + ' 条操作的区间长度必须大于 0');

      const op = { kind: o.kind, start: round3(start), end: round3(end) };
      if (o.kind === 'duplicate') {
        const r = Number(o.repeat === undefined ? 1 : o.repeat);
        if (!isFinite(r) || r < 1) throw new Error('第 ' + n + ' 条操作的 repeat 至少为 1');
        op.repeat = Math.round(r);
      }
      if (o.kind === 'pitch') {
        const s = Number(o.semitones === undefined ? 0 : o.semitones);
        if (!isFinite(s)) throw new Error('第 ' + n + ' 条操作的 semitones 必须是数字');
        op.semitones = Math.round(s);
      }
      if (o.kind === 'volume') {
        const d = Number(o.db === undefined ? 0 : o.db);
        if (!isFinite(d)) throw new Error('第 ' + n + ' 条操作的 db 必须是数字');
        op.db = round3(d);
      }
      return op;
    });

    return {
      name: (typeof data.name === 'string' && data.name.trim()) ? data.name.trim() : '未命名方案',
      note: (typeof data.note === 'string') ? data.note : '',
      operations: ops,
    };
  }

  function exportPreset() {
    try {
      const payload = {
        name: (els.presetName.value || '').trim() || '未命名方案',
        note: (els.presetNote.value || '').trim(),
        operations: collectOperations(),
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: 'application/json;charset=utf-8',
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'pianoize-preset.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // 立刻 revoke 在部分浏览器上会把下载掐断，所以延后释放
      setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
      showToast('预设已导出：pianoize-preset.json（' + payload.operations.length + ' 条操作）');
    } catch (err) {
      showError('预设导出失败：' + (err && err.message ? err.message : '未知错误'));
    }
  }

  function bindPresets() {
    els.btnExportPreset.addEventListener('click', exportPreset);
    els.btnImportPreset.addEventListener('click', function () { els.presetFile.click(); });

    els.presetFile.addEventListener('change', function () {
      const f = els.presetFile.files && els.presetFile.files[0];
      els.presetFile.value = '';   // 允许重复导入同一个文件
      if (!f) return;

      const reader = new FileReader();
      reader.onerror = function () { showError('读取预设文件失败，请确认文件可读。'); };
      reader.onload = function () {
        try {
          const parsed = JSON.parse(String(reader.result || ''));
          const preset = validatePreset(parsed);
          state.operations = preset.operations;
          els.presetName.value = preset.name;
          els.presetNote.value = preset.note;
          renderOperations();
          showToast('已导入预设「' + preset.name + '」，共 ' + preset.operations.length + ' 条操作');
        } catch (err) {
          showError('预设导入失败：' + (err && err.message ? err.message : '不是合法的 JSON'));
        }
      };
      reader.readAsText(f, 'utf-8');
    });
  }

  /* ========================================================================
   * 9. 执行：试听 / 完整渲染 / 结果与报告
   * ====================================================================== */

  /** 统一的 JSON POST：把「HTTP 错误」「ok:false」「网络不通」都收敛成中文 Error */
  function postJSON(path, body) {
    return fetch(API_BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body),
    }).then(function (res) {
      return res.text().then(function (text) {
        let data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (_) { data = null; }
        }
        if (!res.ok || !data || data.ok === false) {
          throw new Error(extractError(data, res.status));
        }
        return data;
      });
    }, function (err) {
      // fetch 本身 reject：DNS / 连接被拒 / CORS 都属于这一类
      throw new Error('连不上后端服务（' + API_ORIGIN_TEXT + '）：' +
        (err && err.message ? err.message : '网络错误'));
    });
  }

  function needProject() {
    if (state.projectId) return true;
    showError('请先上传一个 MIDI 文件。');
    return false;
  }

  /* ---------------------------- 试听 ---------------------------- */

  function doPreview() {
    if (state.busy || !needProject()) return;
    clearError();
    setBusy(true, '正在生成试听音频…');

    postJSON('/api/preview', {
      project_id: state.projectId,
      operations: collectOperations(),
    }).then(function (data) {
      // 音频进播放器
      if (data.audio_url) {
        els.player.src = apiUrl(data.audio_url);
        state.audioDuration = Number(data.duration) || 0;
      state.segments = Array.isArray(data.segments) ? data.segments : null;
        state.playhead = 0;
        try { els.player.load(); } catch (_) {}

        const bits = ['试听音频已就绪 · 时长 ' + formatTime(data.duration)];
        if (typeof data.peak === 'number') bits.push('峰值 ' + data.peak.toFixed(3));
        if (typeof data.lufs === 'number') bits.push(data.lufs.toFixed(1) + ' LUFS');
        bits.push('（播放头已按剪辑结果精确对齐）');
        setTransportInfo(bits.join(' · '));
      } else {
        showError('后端没有返回试听音频地址。');
      }

      renderReport(data.report);
      fillWarnBox(els.warningsWrap, data.warnings);
      hideLog();
      // 试听不产出可下载文件，把结果区收起来避免误导
      renderFiles([]);
      renderScore(null);
      drawWave();
      showToast('试听已生成');
    }).catch(function (err) {
      showError(err && err.message ? err.message : '试听失败');
    }).then(function () {
      setBusy(false);
    });
  }

  /* -------------------------- 完整渲染 -------------------------- */

  function collectRenderOptions() {
    const formats = Array.prototype.slice
      .call(document.querySelectorAll('input[name="fmt"]:checked'))
      .map(function (i) { return i.value; });
    return {
      audio_format: formats,
      bitdepth: Number(els.optBitdepth.value) || 24,
      want_score: !!els.optScore.checked,
      want_pdf: !!els.optPdf.checked,
      lufs: Number(els.optLufs.value),
      piano: els.optPiano.value,
      drop_dup: els.optDropdup.value,
      split: null,
    };
  }

  function doRender() {
    if (state.busy || !needProject()) return;

    const opts = collectRenderOptions();
    if (opts.audio_format.length === 0) {
      showError('请至少选择一种输出格式（mp3 / flac / wav / ogg）。');
      return;
    }
    clearError();
    setBusy(true, '渲染中…这可能需要几十秒，请不要关闭页面');

    const body = {
      project_id: state.projectId,
      operations: collectOperations(),
      audio_format: opts.audio_format,
      bitdepth: opts.bitdepth,
      want_score: opts.want_score,
      want_pdf: opts.want_pdf,
      lufs: opts.lufs,
      piano: opts.piano,
      drop_dup: opts.drop_dup,
      split: opts.split,
    };

    postJSON('/api/render', body).then(function (data) {
      const files = Array.isArray(data.files) ? data.files : [];
      renderFiles(files);
      renderScore(data.score_url);
      renderReport(data.report);
      fillWarnBox(els.warningsWrap, data.warnings);
      renderLog(data.log);

      // 顺手把渲染出来的 mp3 塞进播放器，省得用户再点一次试听
      const mp3 = files.filter(function (f) {
        return /\.mp3$/i.test(String(f && f.name));
      })[0];
      if (mp3 && mp3.url) {
        els.player.src = apiUrl(mp3.url);
        state.audioDuration = Number(data.duration) || 0;
        state.playhead = 0;
        try { els.player.load(); } catch (_) {}
        setTransportInfo('已在播放器中载入渲染结果 · 时长 ' + formatTime(data.duration) +
          '（播放头已按剪辑结果精确对齐）');
      }
      drawWave();
      showToast('渲染完成，共 ' + files.length + ' 个文件');
    }).catch(function (err) {
      showError(err && err.message ? err.message : '渲染失败');
    }).then(function () {
      setBusy(false);
    });
  }

  /* --------------------------- 结果展示 --------------------------- */

  function renderFiles(files) {
    const box = els.resultFiles;
    box.innerHTML = '';

    if (!Array.isArray(files) || files.length === 0) {
      box.hidden = true;
      els.resultEmpty.hidden = false;
      els.resultEmpty.textContent = state.projectId
        ? '还没有产物。执行「试听」或「完整渲染」后会列在这里。'
        : '还没有产物。先上传一个 MIDI 文件吧。';
      return;
    }

    box.hidden = false;
    els.resultEmpty.hidden = true;

    files.forEach(function (f) {
      const name = String((f && f.name) || '未命名文件');
      const url = apiUrl(f && f.url);
      const row = document.createElement('div');
      row.className = 'file-row';

      const main = document.createElement('div');
      main.className = 'file-main';
      const nm = document.createElement('span');
      nm.className = 'file-name';
      nm.textContent = name;
      nm.title = name;
      main.appendChild(nm);
      const sz = document.createElement('span');
      sz.className = 'file-size';
      sz.textContent = humanSize(f && f.size);
      main.appendChild(sz);
      row.appendChild(main);

      if (url) {
        const a = document.createElement('a');
        a.className = 'btn btn-ghost btn-small';
        a.href = url;
        a.setAttribute('download', name);
        a.textContent = '下载';
        a.title = '下载 ' + name;
        row.appendChild(a);
      }
      box.appendChild(row);
    });
  }

  function renderScore(scoreUrl) {
    state.scoreUrl = scoreUrl ? apiUrl(scoreUrl) : null;
    if (!state.scoreUrl) {
      els.resultScore.hidden = true;
      return;
    }
    els.resultScore.hidden = false;
    els.btnOpenScore.onclick = function () {
      window.open(state.scoreUrl, '_blank', 'noopener');
    };
  }

  function hideLog() {
    els.logWrap.hidden = true;
    els.logWrap.open = false;
    els.logText.textContent = '';
  }

  function renderLog(log) {
    if (!log || !String(log).trim()) {
      hideLog();
      return;
    }
    els.logWrap.hidden = false;
    els.logText.textContent = String(log);
  }

  /**
   * 执行报告表格。
   *
   * report 里每条记录的统计字段是**动态**的（trim 有「删除音符」，
   * pitch 有「移调音符 / 越界被钳」……），所以这里取所有 key 的并集当列，
   * 缺的格子显示「—」。这样后端新增统计字段时前端不用改。
   */
  const REPORT_BASE_KEYS = ['index', 'kind', 'describe'];

  function renderReport(report) {
    const table = els.reportTable;
    table.innerHTML = '';

    if (!Array.isArray(report) || report.length === 0) {
      els.reportWrap.hidden = true;
      return;
    }
    els.reportWrap.hidden = false;

    const statKeys = [];
    report.forEach(function (row) {
      if (!row || typeof row !== 'object') return;
      Object.keys(row).forEach(function (k) {
        if (REPORT_BASE_KEYS.indexOf(k) === -1 && statKeys.indexOf(k) === -1) statKeys.push(k);
      });
    });

    const thead = document.createElement('thead');
    const htr = document.createElement('tr');
    ['#', '操作', '说明'].concat(statKeys).forEach(function (label) {
      const th = document.createElement('th');
      th.textContent = label;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    report.forEach(function (row, i) {
      const tr = document.createElement('tr');

      const idxTd = document.createElement('td');
      const rawIdx = (row && row.index !== undefined && row.index !== null) ? Number(row.index) : i;
      idxTd.textContent = String((isFinite(rawIdx) ? rawIdx : i) + 1);
      tr.appendChild(idxTd);

      const kindTd = document.createElement('td');
      kindTd.textContent = (row && KIND_LABEL[row.kind]) ? KIND_LABEL[row.kind]
        : ((row && row.kind) ? String(row.kind) : '—');
      tr.appendChild(kindTd);

      const descTd = document.createElement('td');
      descTd.className = 'col-describe';
      descTd.textContent = (row && row.describe) ? String(row.describe) : '—';
      tr.appendChild(descTd);

      statKeys.forEach(function (k) {
        const td = document.createElement('td');
        td.className = 'num';
        const v = row ? row[k] : undefined;
        td.textContent = (v === undefined || v === null) ? '—' : String(v);
        tr.appendChild(td);
      });

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }

  function bindExecution() {
    els.btnPreview.addEventListener('click', doPreview);
    els.btnRender.addEventListener('click', doRender);
    els.alertClose.addEventListener('click', clearError);
    els.conn.addEventListener('click', function () {
      if (state.busy) return;
      probeBackend().then(function (ok) {
        showToast(ok ? '后端连接正常' : '连不上后端，请确认服务已启动', !ok);
      });
    });
  }

  /* ========================================================================
   * 10. 初始化
   * ====================================================================== */

  function bindGlobalErrorHandlers() {
    // 兜底：任何没被 catch 的异常都变成可见的中文提示，而不是静默卡死
    window.addEventListener('error', function (ev) {
      if (ev && ev.message) showError('界面出现异常：' + ev.message);
    });
    window.addEventListener('unhandledrejection', function (ev) {
      const r = ev && ev.reason;
      showError('操作失败：' + (r && r.message ? r.message : String(r)));
    });
  }

  function init() {
    wave.colors = null;                 // 确保 CSS 已就绪后再读配色
    getColors();

    bindGlobalErrorHandlers();
    bindDropzone();
    bindCanvas();
    bindPlayer();
    bindOperationForm();
    bindPresets();
    bindExecution();

    // 初始渲染
    setKind('trim');
    renderMeta();
    renderOperations();
    renderSelectionInfo();
    renderFiles([]);
    drawWave();

    // 样式表可能还没完全应用，load 之后再校准一次尺寸与配色
    window.addEventListener('load', function () {
      wave.colors = null;
      drawWave();
    });

    probeBackend();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
