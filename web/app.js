/* 拾帧 FrameSeek · 前端（Vue 3 + Element Plus）
   依赖全部在 web/vendor 下本地自持，断网也能跑。
   组件模板写在 index.html 的 <script type="text/x-template"> 里，
   HTML 就是 HTML，不用在 JS 里拼字符串。 */
(function () {
'use strict';

const { reactive, computed, ref, watch, onMounted, onUnmounted,
        onActivated, nextTick } = Vue;

/* ==================== 工具 ==================== */
const api = (url, opt) => fetch(url, opt).then(r => r.json());
const post = (url, body) => api(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});

function esc(s) {
  // 会被喂数字（接口返回的秒数等），先转字符串再替换，
  // 否则 "replace is not a function" 会让整个渲染炸掉。
  return String(s ?? '').replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function hl(t, q) {
  let s = esc(t);
  if (q) {
    const p = esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    s = s.replace(new RegExp(p, 'gi'), m => `<mark>${m}</mark>`);
  }
  return s;
}
const mediaUrl = (p, root) =>
  `/media?p=${encodeURIComponent(p || '')}&root=${encodeURIComponent(root || '')}`;
const hdUrl = f => `/hd/media?p=${encodeURIComponent(f || '')}`;

function fmtDur(s) {
  s = s || 0;
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}时${m}分` : `${m}分${Math.floor(s % 60)}秒`;
}
function fmtAgo(t) {
  if (!t) return '从未';
  const d = Date.now() / 1000 - t;
  if (d < 60) return '刚刚';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false });
}
function fmtSec(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  const m = Math.floor(s / 60);
  return m ? `${m}:${String(s % 60).padStart(2, '0')}` : `${s}s`;
}
const toast = (msg, type) => ElementPlus.ElMessage({
  message: String(msg), type: type || 'info', duration: 2600, showClose: false,
});
const LS = {
  get(k, d) {
    try { const v = localStorage.getItem('fs_' + k); return v === null ? d : JSON.parse(v); }
    catch (e) { return d; }
  },
  set(k, v) { try { localStorage.setItem('fs_' + k, JSON.stringify(v)); } catch (e) {} },
};

/* 极简事件总线：实时通道要通知各页的进度气泡，但那些状态住在组件里 */
const BUS = {
  _m: {},
  on(k, fn) { (this._m[k] = this._m[k] || []).push(fn); },
  emit(k, ...a) { (this._m[k] || []).forEach(f => { try { f(...a); } catch (e) { console.warn(e); } }); },
};

/* ==================== 全局状态 ==================== */
const S = reactive({
  tab: 'libs',
  libs: [],            // 登记的素材库
  builtinLibs: [],     // 内置只读库（AI 生成）
  activeRoot: '',
  hubDb: '',
  totals: { videos: 0, lines: 0 },
  ocr: { ok: true, err: '', loaded: false },
  step: 0.2,           // OCR 采样步长，跟服务端 config.OCR_STEP 保持一致
  all: [],             // 跨库素材全量
  viewCount: 0,        // 素材页当前过滤后的条数（给侧栏徽标）
  boxNoBox: 0,         // 方框复查无框条数（侧栏徽标）
  clips: [],
  pre: 5, post: 5, fast: true,
  tasks: {},
  dockOpen: false,      // 右下角浮窗默认收起（有任务也只露一条细条，要进度展开）
  player: { on: false, title: '', src: '', cur: null },
});

/* ---------- 实时进度：WebSocket → SSE → 轮询 ---------- */
const LIVE = { on: false, mode: 'none', ws: null, es: null, poll: null, known: new Set() };

const PHASE = { ocr: 'OCR 识别', shots: '截图分片', calib: '字幕标定',
                cover: '封面', gif: '动图', lines: '台词', clear: '清理旧台词' };

function onLive(ev) {
  if (!ev || ev.type === 'ping' || ev.type === 'hello') return;

  if (ev.type === 'video') {
    // 索引过程中素材实时入库：直接插进列表，不用等整库跑完再刷新
    const v = ev.video || {};
    if (!v.id) return;
    const key = v.root + '#' + v.id;
    if (LIVE.known.has(key)) return;
    LIVE.known.add(key);
    if (!S.all.some(x => x.root === v.root && x.id === v.id)) S.all.push(v);
    BUS.emit('assets:refresh');
    return;
  }

  if (ev.type === 'progress') {
    const k = ev.root || '';
    if (!k) return;
    const prev = S.tasks[k] || {};
    const merged = Object.assign({}, prev, ev);
    // 阶段事件只带 frames_*，phase 可能为空——沿用上一阶段的 phase，
    // 否则帧进度条会在两帧之间来回闪。
    if (!ev.phase && !ev.frames_total && prev.phase) merged.phase = '';
    merged.lib = ev.lib || prev.lib || '';
    S.tasks[k] = merged;
    if (ev.kind === 'gen') BUS.emit('gen:tick', k, ev.done || 0, ev.message || '');
    if (ev.kind === 'handdraw') BUS.emit('hd:tick', k, ev.done || 0, ev.message || '');
    return;
  }

  if (ev.type === 'task_done') {
    const k = ev.root || '';
    delete S.tasks[k];       // 先删卡片，否则会一直显示 100% 不消失
    const s = ev.summary || {};

    if (s.handdraw) {
      BUS.emit('hd:done', s);
      toast(s.error ? `手绘任务失败：${s.error}` : `手绘完成：${s.name || ''}`,
            s.error ? 'error' : 'success');
    } else if (s.gen) {
      BUS.emit('gen:done', k, s);
      toast(s.error ? `视频生成失败：${s.error}`
                    : `生成完成：${s.name || ''}（${s.seconds || '?'}s），已落入「AI生成」目录`,
            s.error ? 'error' : 'success');
    } else if (s.single) {
      const SRC = { external: '外挂 SRT', embedded: '内嵌字幕',
                    ocr: 'OCR 识别', asr: '语音识别' };
      toast(s.error ? `「${s.name}」识别失败：${s.error}`
                    : `「${s.name}」识别完成：清掉旧台词 ${s.cleared || 0} 条，`
                      + `新出 ${s.lines} 条（${SRC[s.source] || s.source || '未知'}）`,
            s.error ? 'error' : 'success');
    } else {
      toast(`「${ev.lib}」索引完成：${s.total} 个素材，跳过 ${s.skipped} 个重复`
            + (s.failed && s.failed.length ? `，失败 ${s.failed.length}` : ''), 'success');
    }
    loadLibs();
    BUS.emit('assets:refresh');
    BUS.emit('box:refresh');
    return;
  }
}

function connectLive() {
  if (LIVE.on) return;
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  let ws;
  try { ws = new WebSocket(url); } catch (e) { return connectSSE(); }

  const fail = () => { LIVE.ws = null; if (!LIVE.on) connectSSE(); };
  ws.onopen = () => { LIVE.on = true; LIVE.mode = 'ws'; stopSSE(); };
  ws.onmessage = e => { try { onLive(JSON.parse(e.data)); } catch (err) {} };
  ws.onerror = fail;
  ws.onclose = () => {
    if (LIVE.mode === 'ws') { LIVE.on = false; fail(); setTimeout(connectLive, 3000); }
  };
  LIVE.ws = ws;
}
function connectSSE() {
  if (LIVE.es || LIVE.mode === 'ws') return;
  const es = new EventSource('/api/events');
  es.onmessage = e => { try { onLive(JSON.parse(e.data)); } catch (err) {} };
  es.onerror = () => { es.close(); LIVE.es = null; startFallbackPoll(); };
  LIVE.es = es; LIVE.mode = 'sse';
}
function stopSSE() { if (LIVE.es) { LIVE.es.close(); LIVE.es = null; } }
function startFallbackPoll() {
  if (LIVE.poll) return;
  LIVE.mode = 'poll';
  LIVE.poll = setInterval(async () => {
    let ts = [];
    try { ts = await api('/api/tasks'); } catch (e) { return; }
    const next = {};
    (ts || []).forEach(t => {
      next[t.root || t.lib] = Object.assign({}, t, { lib: t.lib });
      if (t.kind === 'gen') BUS.emit('gen:tick', t.root, t.done || 0, t.message || '');
    });
    S.tasks = next;
    if (!ts || !ts.length) {
      clearInterval(LIVE.poll); LIVE.poll = null;
      loadLibs();
    }
  }, 1000);
}
const startPoll = () => connectLive();

/* ---------- 素材库数据 ---------- */
async function loadLibs() {
  const s = await api('/api/state');
  S.libs = s.libraries || [];
  S.builtinLibs = s.builtin_libs || [];
  S.activeRoot = s.active || '';
  S.ocr = s.ocr || { ok: true, err: '' };
  S.hubDb = s.hub_db || '';
  S.totals = s.total || { videos: 0, lines: 0 };
  // 采样步长必须跟服务端一致：覆盖率 = hits / (span/step + 1)。
  // 曾经前端写死 0.5，服务端改成 0.2 后覆盖率虚高 2.5 倍、误报一片。
  if (s.ocr_step) S.step = +s.ocr_step;
  BUS.emit('libs:loaded');
  BUS.emit('assets:refresh');
}

/* ---------- 播放器 ---------- */
function play(video_id, start, title, root) {
  S.player.cur = { video_id, root: root || '' };
  S.player.title = title || '预览';
  S.player.src = `/stream/${video_id}?root=${encodeURIComponent(root || '')}`;
  S.player.start = start || 0;
  S.player.on = true;
  nextTick(() => {
    const v = document.querySelector('.fs-player-video');
    if (!v) return;
    v.src = S.player.src;
    v.onloadedmetadata = () => {
      v.currentTime = Math.max(0, (start || 0) - Math.min(S.pre || 5, 2));
      v.play().catch(() => {});
    };
  });
}

/* ---------- 片段导出 ---------- */
async function addClips(r) {
  if (!r || !r.ok) { toast((r && r.err) || '导出失败', 'error'); return; }
  S.clips = (r.clips || []).concat(S.clips);
  S.tab = 'clips';
}
function clipLine(line_id, root) {
  return api('/api/clip', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ root, line_id, pre: S.pre, post: S.post, fast: S.fast }),
  }).then(addClips);
}

/* ==================== 素材库 ==================== */
const LibsView = {
  template: '#tpl-libs',
  setup() {
    const form = reactive({ path: '', name: '' });
    const dlg = reactive({ scan: false, upload: false, ignored: false });
    const scanOpt = reactive({
      asr: LS.get('use_asr', true),
      gif: LS.get('with_gif', true),
      retry: LS.get('retry_empty', true),
    });
    const target = ref(null);            // 当前操作的库
    const uploadFiles = ref([]);
    const uploadMsg = ref('');
    const ignoredRows = ref([]);

    const dlgScanTitle = computed(() =>
      target.value ? `索引「${target.value.name}」` : '索引全部素材库');
    const dlgUploadTitle = computed(() =>
      target.value ? `上传到「${target.value.name}」` : '上传');
    const dlgIgnoredTitle = computed(() =>
      target.value ? `「${target.value.name}」已忽略 ${ignoredRows.value.length} 个` : '已忽略');

    /* OCR 耗时提示（实测：8 进程 1.5~2.1 倍实时） */
    const scanHint = computed(() => {
      const l = target.value;
      if (!S.ocr.ok) {
        return `<span class="bad">OCR 不可用：${esc(S.ocr.err || '')}</span>`
             + '没有字幕的素材将拿不到台词。';
      }
      if (!l) {
        return '开启后没有字幕的视频会用 <b>OCR 识别画面硬字幕</b>，'
             + '实测 8 进程并行约 1.5~2.1 倍实时（1 小时素材约 30~40 分钟）。';
      }
      const hours = (l.duration || 0) / 3600;
      if (!hours) return '开启后没有字幕的视频会用 OCR 识别画面硬字幕。';
      const est = hours / 1.8;
      const estTxt = est < 1 ? Math.round(est * 60) + ' 分钟' : est.toFixed(1) + ' 小时';
      return `这个库共 ${hours.toFixed(1)} 小时素材，OCR 识别画面硬字幕约 <b>${estTxt}</b>`
        + '（按 1.8 倍实时保守算）。'
        + '<div style="margin-top:5px">台词来源优先级：<b>同名 .srt → 内嵌字幕 → OCR 识别画面</b>，'
        + '前两种是秒级，OCR 最慢。</div>'
        + '<div style="margin-top:3px">OCR 出来的台词会写成视频同名 .srt，改完错字在素材页点'
        + '「重新导入同名 SRT」就行，不用重跑 OCR。</div>'
        + '<div style="margin-top:3px">开跑前会花十几秒标定这部片的字幕位置，'
        + '画面里的台标、片内数字、片头演员表会自动滤掉。</div>';
    });

    async function addLib() {
      const p = (form.path || '').trim();
      if (!p) { toast('先填素材目录路径', 'warning'); return; }
      const r = await post('/api/libraries', { path: p, name: (form.name || '').trim() });
      if (!r.ok) {
        ElementPlus.ElMessageBox.alert(r.err || '目录不存在或无法访问', '登记失败');
        return;
      }
      form.path = ''; form.name = '';
      toast(`已登记「${r.name}」`, 'success');
      loadLibs();
    }

    function askScan(l) {
      target.value = l || null;
      scanOpt.asr = LS.get('use_asr', true);
      scanOpt.gif = LS.get('with_gif', true);
      scanOpt.retry = LS.get('retry_empty', true);
      dlg.scan = true;
    }

    async function doScan() {
      LS.set('use_asr', scanOpt.asr);
      LS.set('with_gif', scanOpt.gif);
      LS.set('retry_empty', scanOpt.retry);
      await post('/api/settings', { use_asr: scanOpt.asr, with_gif: scanOpt.gif });
      const url = target.value
        ? `/api/libraries/${target.value.id}/scan` : '/api/libraries/scan_all';
      await post(url, { use_asr: scanOpt.asr, with_gif: scanOpt.gif,
                        retry_empty: scanOpt.retry });
      dlg.scan = false;
      S.tab = 'libs';
      startPoll();
      toast(scanOpt.asr ? '开始索引（转写较慢，看右下角进度）' : '开始索引');
    }

    function askUpload(l) {
      target.value = l || null;
      uploadFiles.value = []; uploadMsg.value = '';
      dlg.upload = true;
    }
    function onPickFiles(f) { uploadFiles.value.push(f); }
    async function doUpload() {
      if (!uploadFiles.value.length) return;
      const fd = new FormData();
      fd.append('root', (target.value || {}).path || '');
      uploadFiles.value.forEach(f => fd.append('file', f.raw || f));
      uploadMsg.value = '上传中…';
      const r = await api('/api/upload', { method: 'POST', body: fd });
      if (!r.ok) { uploadMsg.value = '失败：' + (r.err || ''); return; }
      uploadMsg.value = '已上传 ' + (r.saved || []).join('、') + '，正在入库…';
      startPoll();
      setTimeout(() => { dlg.upload = false; S.tab = 'libs'; }, 900);
    }

    async function askIgnored(l) {
      target.value = l;
      try { ignoredRows.value = await api(`/api/ignored?root=${encodeURIComponent(l.path)}`); }
      catch (e) { ignoredRows.value = []; }
      dlg.ignored = true;
    }
    async function restoreOne(r) {
      await post('/api/ignored/restore', { root: (target.value || {}).path, md5: r.md5 });
      dlg.ignored = false;
      await loadLibs();
      toast('已放回', 'success');
    }
    async function restoreAll() {
      await post('/api/ignored/restore', { root: (target.value || {}).path, all: true });
      dlg.ignored = false;
      await loadLibs();
      toast('已全部放回，下次索引会重新入库', 'success');
    }

    function delLib(l) {
      ElementPlus.ElMessageBox.confirm(
        `只删项目库 hub.db 里的一条登记记录。<br>`
        + `磁盘上的素材文件、以及该目录下的 .frameseek 索引全部保留；`
        + `重新登记这个文件夹，索引还在。<br><br>`
        + `<span class="dim">${esc(l.path)}</span>`,
        `移除「${l.name}」`,
        { dangerouslyUseHTMLString: true, confirmButtonText: '移除登记',
          cancelButtonText: '取消', type: 'warning' },
      ).then(async () => {
        const r = await post(`/api/libraries/${l.id}/delete`);
        await loadLibs();
        toast(r && r.ok ? `已移除「${l.name}」` : '移除失败，看服务端日志',
              r && r.ok ? 'success' : 'error');
      }).catch(() => {});
    }

    return {
      S, form, dlg, scanOpt, target, uploadFiles, uploadMsg, ignoredRows,
      libs: computed(() => S.libs), hubDb: computed(() => S.hubDb),
      scanTarget: computed(() => target.value),
      dlgScanTitle, dlgUploadTitle, dlgIgnoredTitle, scanHint,
      addLib, askScan, doScan, askUpload, onPickFiles, doUpload,
      askIgnored, restoreOne, restoreAll, delLib,
      fmtDur, fmtAgo,
    };
  },
};

/* ==================== 台词检索 ==================== */
const SearchView = {
  template: '#tpl-search',
  setup() {
    const q = ref('');
    const rows = ref([]);
    const searched = ref(false);

    const pre = computed({
      get: () => S.pre, set: v => { S.pre = v == null ? 0 : v; },
    });
    const post_ = computed({
      get: () => S.post, set: v => { S.post = v == null ? 0 : v; },
    });
    const fast = computed({ get: () => S.fast, set: v => { S.fast = !!v; } });

    async function doSearch() {
      const kw = (q.value || '').trim();
      if (!kw) return;
      searched.value = true;
      rows.value = await api(
        `/api/search?q=${encodeURIComponent(kw)}&scope=all&limit=300`);
    }
    function onClear() { searched.value = false; rows.value = []; }

    /* 方框凭证标记：这条台词在画面上有没有一个真实的框撑着 */
    function boxMark(r) {
      const b = r.box;
      if (!b) {
        return (r.src && r.src !== 'ocr')
          ? `<span class="boxinfo" title="${esc(r.src)} 来源字幕，本就没有画面框">· 无框(正常)</span>`
          : '<span class="boxwarn" title="这条台词没有画面上的方框凭证，可能不是画面字幕">· 无框凭证</span>';
      }
      const ar = b.ar, hits = b.hits || 0, conf = b.conf;
      const span = Math.max(0, (r.end || 0) - (r.start || 0));
      const cov = span > 0.6 ? hits / (span / S.step + 1) : 1;
      const bad = ar < 0.55 || ar > 1.45 || cov < 0.34 || cov > 1.25 || conf < 0.62;
      const fnt = b.font || {};
      const tip = `框 cx=${b.cx} cy=${b.cy} 宽=${b.bw} 字高=${b.bh}\n`
        + `字宽比 ${ar} · ${hits} 帧支持(覆盖 ${Math.round(cov * 100)}%) · 置信度 ${conf}`
        + (b.drift != null ? ` · 跨帧漂移 ${b.drift}` : '')
        + (fnt.ink ? `\n字体：墨密度 ${fnt.ink} · 笔画 ${fnt.stroke}px`
                     + ` · 字高占画面 ${(fnt.px_h * 100).toFixed(1)}%` : '');
      const mark = bad
        ? `<span class="boxwarn" title="${esc(tip)}">· 方框异常</span>`
        : `<span class="boxok" title="${esc(tip)}">· 方框已验</span>`;
      // 字体偏离与"方框异常"是两个维度，徽标分开；阈值同 config.OCR_FONT_DEV_MAX
      if ((b.font_dev || 0) > 1.5) {
        return mark + `<span class="boxfont" title="${esc(tip + '\n\n字体偏离度 ' + b.font_dev
          + '：与本部片主流字幕的字体/字号不一致，纠错时优先怀疑这一条。')}">· 字体异常</span>`;
      }
      return mark;
    }

    return {
      q, rows, searched, pre, post: post_, fast,
      doSearch, onClear, boxMark,
      hl: t => hl(t, q.value),
      shotUrl: r => mediaUrl(r.shot || r.cover, r.root),
      playRow: r => play(r.video_id, r.start, r.name, r.root),
      clipRow: r => clipLine(r.id, r.root),
    };
  },
};

/* ==================== 素材（跨库） ==================== */
const AssetsView = {
  template: '#tpl-assets',
  setup() {
    const kw = ref('');
    const showFilter = ref(false);
    const libSel = reactive({});          // { libId: bool }
    const durSel = ref([]);               // 时长分档

    const allLibs = computed(() => (S.libs || []).concat(S.builtinLibs || []));

    // 「全部」勾上 = 不设库条件；勾其它项时自动取消「全部」
    const libsAll = computed({
      get: () => Object.values(libSel).every(v => !v),
      set: v => { if (v) Object.keys(libSel).forEach(k => { libSel[k] = false; }); },
    });

    const libSet = computed(() => new Set(
      Object.keys(libSel).filter(k => libSel[k]).map(Number)));

    const view = computed(() => {
      const f = (kw.value || '').trim().toLowerCase();
      const libs = libSet.value, dur = new Set(durSel.value);
      return S.all.filter(r => {
        if (libs.size && !libs.has(r.lib_id)) return false;
        if (dur.size) {
          const d = +(r.duration || 0);
          const hit = (dur.has('s30') && d > 0 && d < 30)
                   || (dur.has('m30') && d >= 30 && d < 300)
                   || (dur.has('m5_20') && d >= 300 && d < 1200)
                   || (dur.has('l20') && d >= 1200);
          if (!hit) return false;
        }
        if (f && !(r.name || '').toLowerCase().includes(f)
              && !(r.lib || '').toLowerCase().includes(f)) return false;
        return true;
      });
    });

    watch(view, v => { S.viewCount = v.length; }, { immediate: true });

    const summary = computed(() => {
      const bits = [];
      if (libSet.value.size) {
        bits.push('库：' + [...libSet.value].map(id => {
          const l = allLibs.value.find(x => +x.id === +id);
          return l ? l.name : id;
        }).join('、'));
      }
      if (durSel.value.length) bits.push('时长：' + durSel.value.join('/'));
      return `命中 ${view.value.length} / ${S.all.length} 部`
           + (bits.length ? ' · ' + bits.join(' · ') : ' · 未设条件');
    });

    async function reload() {
      const qs = libSet.value.size
        ? `&libs=${[...libSet.value].join(',')}` : '';
      S.all = await api(`/api/videos?scope=all${qs}`);
    }
    // 切换页签会重新挂载，监听必须跟着解绑，否则回调会越积越多
    const offRefresh = BUS.on('assets:refresh', reload);
    onUnmounted(offRefresh);
    watch(libSet, reload);

    function resetFilters() {
      Object.keys(libSel).forEach(k => { libSel[k] = false; });
      durSel.value = [];
      kw.value = '';
      return reload();
    }

    const picking = ref(null);
    const dlg = reactive({ re: false, asr: false });
    const reOpt = reactive({ force: false });
    const asrOpt = reactive({ model: 'small', info: null });

    function onCmd(cmd, v) {
      if (cmd === 'preview') { play(v.id, 0, v.name, v.root); return; }
      if (cmd === 'dir') {
        const rel = (v.rel || '').replace(/[\\/]+[^\\/]*$/, '');
        post('/api/open', { path: rel ? v.root + '\\' + rel : v.root });
        return;
      }
      if (cmd === 'reimport') return askReimport(v);
      if (cmd === 'retranscribe') return askRetranscribe(v);
      if (cmd === 'asr') return askAsr(v);
      if (cmd === 'remove') return askDel(v, false);
      if (cmd === 'purge') return askDel(v, true);
    }

    const PREV = { external: '外挂 SRT', embedded: '内嵌字幕',
                   ocr: 'OCR 识别', asr: '语音转写', pending: '识别中' };

    function askRetranscribe(v) {
      picking.value = v;
      // 上次是 OCR 出来的话，视频旁边的同名 .srt 是我们自己的产物，
      // 不跳过它等于把旧结果读回来——所以这种情况默认强制重跑。
      reOpt.force = v.line_source === 'ocr';
      dlg.re = true;
    }
    async function doRetranscribe() {
      const v = picking.value;
      dlg.re = false;
      const r = await post('/api/video/retranscribe',
        { root: v.root, video_id: v.id, force: !!reOpt.force });
      if (!r.ok) { toast(r.err || '启动失败', 'error'); return; }
      startPoll();
      toast('已清空旧台词，开始重新识别「' + v.name + '」');
    }

    async function askAsr(v) {
      picking.value = v;
      let info = {};
      try { info = await api('/api/asr/info'); } catch (e) { info = {}; }
      asrOpt.info = info;
      asrOpt.model = info.default_model || 'small';
      dlg.asr = true;
    }
    async function doAsr() {
      const v = picking.value;
      dlg.asr = false;
      const r = await post('/api/video/transcribe_asr',
        { root: v.root, video_id: v.id, model: asrOpt.model });
      if (!r.ok) { toast(r.err || '启动失败', 'error'); return; }
      startPoll();
      toast('开始语音识别「' + v.name + '」');
    }
    const asrEta = computed(() => {
      const v = picking.value, i = asrOpt.info;
      if (!v || !i) return '';
      const eta = i.cuda ? (v.duration || 0) / 10 : (v.duration || 0) / 1.8;
      return (i.cuda ? 'GPU' : 'CPU') + ' · 预计 '
        + (eta >= 60 ? Math.round(eta / 60) + ' 分钟' : Math.round(eta) + ' 秒');
    });

    function askReimport(v) {
      ElementPlus.ElMessageBox.confirm(
        `读取视频同目录的同名 .srt（必须是 `
        + `<code>${esc((v.name || '').replace(/\.[^.]+$/, ''))}.srt</code>），`
        + `覆盖数据库里的台词并重抽截图。<br>`
        + `<span class="dim">适合用记事本/剪映改完错字、调完时间点之后用——不用重新转写。</span>`,
        '重新导入字幕',
        { dangerouslyUseHTMLString: true, confirmButtonText: '导入' },
      ).then(async () => {
        const r = await post('/api/video/reimport', { root: v.root, video_id: v.id });
        if (!r.ok) { toast(r.err || '导入失败', 'error'); return; }
        toast(`已导入 ${r.lines} 条字幕`, 'success');
        await reload();
      }).catch(() => {});
    }

    function askDel(v, withFile) {
      const full = [v.root, v.rel].filter(Boolean).join('\\');
      const body = `<div><b>${esc(v.name)}</b></div>`
        + `<div class="dim">${esc(full)}</div>`
        + `<div class="dim">${v.duration_str || ''} · 台词 ${v.line_count || 0}`
        + ` · 库「${esc(v.lib || '')}」</div>`
        + (withFile
          ? `<div style="margin-top:8px" class="bad"><b>会把磁盘上的文件一起删掉，`
            + `不进回收站，不可恢复。</b><br>同时删除：视频原文件、同名外挂字幕、`
            + `封面、动图、该片所有台词截图。</div>`
          : `<div class="dim" style="margin-top:6px">只把这条记录从库里去掉，磁盘文件保留。</div>`);
      // 删文件要点两次才算数：第一次按只改按钮文字，什么都不做
      let armed = !withFile;
      ElementPlus.ElMessageBox.confirm(body,
        withFile ? '删除素材及文件' : '从库中移除',
        { dangerouslyUseHTMLString: true,
          confirmButtonText: withFile ? '删除' : '移除',
          type: withFile ? 'error' : 'warning',
          beforeClose: (action, inst, done) => {
            if (action !== 'confirm') { done(); return; }
            if (!armed) {
              armed = true;
              inst.confirmButtonText = '再点一次：永久删除';
              return;                       // 不 done()，弹窗留着
            }
            inst.confirmButtonLoading = true;
            post('/api/video/delete',
              { root: v.root, video_id: v.id, delete_file: withFile })
              .then(async r => {
                inst.confirmButtonLoading = false;
                done();
                await loadLibs();
                toast(r && r.ok
                  ? (withFile ? `已删除「${v.name}」及磁盘文件（${(r.deleted || []).length} 个）`
                              : `已从库里移除「${v.name}」，文件还在`)
                  : '失败：' + ((r && r.err) || '看服务端日志'),
                  r && r.ok ? 'success' : 'error');
              });
          },
        }).catch(() => {});
    }

    // keep-alive 下 onActivated 首次挂载也会触发，切回素材页时刷新一遍
    // （实时事件推来的素材在别的时候也要能进来，靠 assets:refresh 兜着）
    onActivated(reload);

    return {
      kw, showFilter, libSel, durSel,
      allLibs, libsAll, view, summary, picking, dlg, reOpt, asrOpt, asrEta,
      resetFilters, onCmd, doRetranscribe, doAsr,
      viewCount: computed(() => view.value.length),
      coverUrl: v => mediaUrl(v.cover, v.root),
      preview: v => play(v.id, 0, v.name, v.root),
      hoverGif(ev, v, on) {
        if (!v.gif) return;
        const el = ev.target;
        if (on) {
          if (el.dataset.gif === '1') return;
          el.dataset.src = el.src; el.dataset.gif = '1';
          el.src = mediaUrl(v.gif, v.root);
        } else {
          if (el.dataset.gif !== '1') return;
          el.src = el.dataset.src; el.dataset.gif = '0';
        }
      },
      prevSrc: computed(() => {
        const v = picking.value;
        return v ? PREV[v.line_source || ''] || '无' : '';
      }),
      prevCount: computed(() => (picking.value || {}).line_count || 0),
      pickName: computed(() => (picking.value || {}).name || ''),
    };
  },
};

/* BUS.on 返回取消函数，组件销毁时解绑，避免已卸载的面板还在响应实时事件 */
{
  const raw = BUS.on.bind(BUS);
  BUS.on = function (k, fn) {
    raw(k, fn);
    return () => {
      const a = BUS._m[k] || [];
      const i = a.indexOf(fn);
      if (i >= 0) a.splice(i, 1);
    };
  };
}

/* ==================== AI 生成（对话窗口） ==================== */
const GenView = {
  template: '#tpl-gen',
  setup() {
    const gen = reactive({ ready: false, missing: [], aspects: [], outDir: '',
                           ref: '', refName: '', engine: '' });
    const prompt = ref('');
    const dur = ref(8), aspect = ref(''), mp = ref(0.3);
    const seed = ref(0), fname = ref(''), adv = ref(false);
    const msgs = ref([]);
    const refFile = ref(null);

    const examples = [
      { t: '演播室口播',
        p: 'Professional digital human news anchor in a modern broadcast studio, speaking calmly to the camera. Medium shot, soft studio lighting, clean background.' },
      { t: '咖啡特写',
        p: 'Close-up of a coffee cup on a wooden table, steam rising, morning light through the window, slow zoom in.' },
      { t: '航拍日出',
        p: 'A drone shot flying over a rice terrace at sunrise, mist in the valleys, cinematic wide shot.' },
    ];

    async function loadInfo() {
      let r = {};
      try { r = await api('/api/gen/info'); } catch (e) { r = {}; }
      gen.ready = !!r.ready;
      gen.missing = r.missing || [];
      gen.aspects = r.aspects || [];
      gen.outDir = r.out_dir || '';
      gen.engine = r.engine || '';
      if (!aspect.value && gen.aspects.length) {
        aspect.value = gen.aspects.find(a => a.startsWith('16:9')) || gen.aspects[0];
      }
    }

    function pickRef() { if (refFile.value) refFile.value.click(); }
    async function onRef(e) {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append('root', S.activeRoot);
      fd.append('file', f);
      const r = await api('/api/gen/upload', { method: 'POST', body: fd });
      if (!r.ok) { toast('参考图上传失败：' + (r.err || ''), 'error'); return; }
      gen.ref = r.path; gen.refName = r.name || '';
      e.target.value = '';
    }
    function dropRef() { gen.ref = ''; gen.refName = ''; }

    async function submit() {
      const p = (prompt.value || '').trim();
      if (!p) { toast('先写提示词', 'warning'); return; }
      if (!gen.ready) { toast('生成引擎未就绪：' + (gen.missing || []).join('、'), 'error'); return; }
      const body = {
        prompt: p, duration: Number(dur.value), aspect: aspect.value,
        megapixels: Number(mp.value), seed: Number(seed.value || 0),
        name: (fname.value || '').trim(), ref_image: gen.ref,
        index: 'none', root: S.activeRoot,
      };
      msgs.value.push({ role: 'me', prompt: p, dur: dur.value, aspect: aspect.value,
                        mp: mp.value, ref: !!gen.ref });
      const bub = { role: 'sys', status: 'run', pct: 0, msg: '已提交', root: body.root };
      msgs.value.push(bub);
      prompt.value = '';
      try {
        const r = await post('/api/generate', body);
        if (!r.ok) { bub.status = 'error'; bub.msg = r.err || '生成没起来'; return; }
        bub.msg = '生成中…';
        startPoll();
      } catch (e) {
        bub.status = 'error'; bub.msg = String(e);
      }
    }

    const offTick = BUS.on('gen:tick', (root, pct, msg) => {
      for (let i = msgs.value.length - 1; i >= 0; i--) {
        const m = msgs.value[i];
        if (m.role === 'sys' && m.status === 'run' && (!root || m.root === root)) {
          m.pct = Math.max(m.pct || 0, pct || 0);
          if (msg) m.msg = msg;
          return;
        }
      }
    });
    const offDone = BUS.on('gen:done', (root, s) => {
      for (let i = msgs.value.length - 1; i >= 0; i--) {
        const m = msgs.value[i];
        if (m.role === 'sys' && m.status === 'run' && (!root || m.root === root)) {
          if (s.error) { m.status = 'error'; m.msg = s.error; }
          else {
            m.status = 'done';
            m.file = s.file; m.name = s.name || ''; m.seconds = s.seconds;
            m.w = s.w; m.h = s.h;
          }
          return;
        }
      }
    });
    onUnmounted(() => { offTick(); offDone(); });

    onActivated(loadInfo);   // keep-alive：切回 AI 生成页时重新摸一遍引擎状态

    // 回归脚本要读引擎状态决定走「正常发送」还是「未就绪拦截」断言
    window.__FS.gen = gen;

    return {
      gen, prompt, dur, aspect, mp, seed, fname, adv, msgs, refFile, examples,
      submit, pickRef, onRef, dropRef,
      clearChat: () => { msgs.value = []; },
      goAssets: () => { S.tab = 'assets'; },
      mediaUrl,
    };
  },
};

/* ==================== 白板手绘 ==================== */
const HanddrawView = {
  template: '#tpl-hd',
  setup() {
    const hd = reactive({ ready: false, missing: [], engine: '', voice: '',
                          base: '', outDir: '', project: '',
                          upScript: '', upScriptName: '',
                          upImage: '', upImageName: '' });
    const step = ref(1);
    const pname = ref(''), ptitle = ref('');
    const targetSec = ref(30), sceneId = ref('scene-01');
    const ann = ref(''), annMsg = ref('');
    const scenes = ref([]), cues = ref([]);
    const tlStatus = ref('-'), tlAdvice = ref('');
    const voiceText = ref(''), allowNet = ref(false);
    const chats = ref([]);

    const hdRoot = () => S.activeRoot || (S.libs[0] && S.libs[0].path) || '';

    function say(who, text, meta) { chats.value.push({ kind: 'text', who, text, meta }); }
    function err(text) { chats.value.push({ kind: 'err', who: 'sys', text }); }
    function media(file, text, isImage) {
      chats.value.push({ kind: 'media', who: 'sys', file, text, image: !!isImage });
    }
    function kv(text, rows) { chats.value.push({ kind: 'kv', who: 'sys', text, rows }); }

    /* 引擎摸底 */
    async function loadInfo() {
      try {
        const d = await api('/api/handdraw/info');
        if (d.ok === false) throw new Error(d.err || '引擎异常');
        hd.ready = !!d.ready;
        hd.missing = d.missing || [];
        hd.engine = d.engine || '';
        hd.voice = d.default_voice || '';
        hd.base = d.project_root_dir || '';
        hd.outDir = d.out_dir || '';
      } catch (e) {
        hd.ready = false;
        hd.missing = [String((e && e.message) || e)];
      }
    }

    async function refresh() {
      if (!hd.project) return;
      try {
        const d = await api(`/api/handdraw/status?root=${encodeURIComponent(hdRoot())}`
          + `&project=${encodeURIComponent(hd.project)}`);
        if (d.ok === false) { hd.project = ''; return; }
        tlStatus.value = d.timelineStatus || '-';
        tlAdvice.value = d.advice || '';
      } catch (e) { /* 工程可能还没建 */ }
    }

    const projText = computed(() => {
      if (!hd.project) return '';
      const n = Array.isArray(scenes.value) ? scenes.value.length : 0;
      return `工程 <b>${esc(hd.project)}</b>`;
    });

    /* 上传：直接造一个 input，不用在模板里塞隐藏控件 */
    function upload(kind) {
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = kind === 'script' ? '.txt,.srt' : '.png,.jpg,.jpeg,.webp,.bmp';
      inp.onchange = async () => {
        const f = inp.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append('file', f);
        const d = await api('/api/handdraw/upload', { method: 'POST', body: fd });
        if (d.ok === false) { err(d.err || '上传失败'); return; }
        if (kind === 'script') {
          hd.upScript = d.path;
          hd.upScriptName = `${d.name}（${d.kind === 'srt' ? '字幕' : '文稿'}）`;
          if (!pname.value) pname.value = (d.name || '').replace(/\.(txt|srt)$/i, '');
        } else {
          hd.upImage = d.path;
          hd.upImageName = d.name;
        }
        toast('已上传 ' + d.name, 'success');
      };
      inp.click();
    }

    async function init() {
      if (!hd.upScript) { err('先上传一个 .txt 文稿或 .srt 字幕'); return; }
      say('me', '① 建工程', pname.value || '(自动命名)');
      const d = await post('/api/handdraw/init', {
        root: hdRoot(),
        script: /\.srt$/i.test(hd.upScript) ? '' : hd.upScript,
        srt: /\.srt$/i.test(hd.upScript) ? hd.upScript : '',
        name: pname.value, title: ptitle.value,
      });
      if (d.ok === false) { err(d.err || '建工程失败'); return; }
      hd.project = pname.value || (d.project || '').replace(/^.*[\\/]/, '');
      hd.upImage = ''; hd.upImageName = '';
      say('sys', `工程就绪：${d.project} · ${d.cues} 条字幕 · 时间轴「${d.timelineStatus}」（预估，非配音对齐结果）`);
      step.value = 2;
      await refresh();
      await suggest();
    }

    async function suggest() {
      if (!hd.project) { err('还没有工程，先做第 1 步'); return; }
      const d = await post('/api/handdraw/scenes', {
        root: hdRoot(), project: hd.project, target_sec: +targetSec.value || 30 });
      if (d.ok === false) { err(d.err || '出分幕建议失败'); return; }
      cues.value = d.cues || [];
      scenes.value = d.scenes || [];
      const total = scenes.value.reduce((a, s) => a + (s.sceneDurationMs || 0), 0);
      const cueMs = cues.value.length ? cues.value[cues.value.length - 1].endMs : 0;
      say('sys', `② 分幕建议：${scenes.value.length} 幕`,
        `字幕 ${(cueMs / 1000).toFixed(1)}s / 建议画面 ${(total / 1000).toFixed(1)}s`
        + '（画面总长要 ≥ 字幕总长，否则引擎拒绝正式渲染）');
      step.value = 3;
      await refresh();
    }

    function sceneText(s) {
      const [a, b] = s.cueRange || [];
      const t = cues.value.filter(c => c.index >= a && c.index <= b)
        .map(c => c.text).join(' ');
      return esc(t.slice(0, 80)) + (t.length > 80 ? '…' : '');
    }

    async function regions() {
      if (!hd.project) { err('还没有工程'); return; }
      const d = await post('/api/handdraw/preview',
        { root: hdRoot(), project: hd.project, scene_id: sceneId.value });
      if (d.ok === false) { err(d.err || '出检查图失败'); return; }
      const f = d.result || d.output;
      if (f) media(f, `④ 分区检查图 · ${sceneId.value}（核对编号顺序与区域边界）`, true);
    }

    function tplAnn() {
      const tpl = {
        sceneId: sceneId.value || 'scene-01',
        canvas: { width: 1920, height: 1080 },
        storyBasis: '这一幕在讲什么',
        sceneDurationMs: 24000,
        elements: [
          { id: 'main', label: '主体', sequence: 1,
            narrativeRole: '场景铺垫：先画出主体', subtitle: '对应的字幕原文',
            type: 'structure', region: { x: 300, y: 300, width: 500, height: 400 },
            reveal: { direction: 'left_to_right', startMs: 300, durationMs: 2600,
                      maskPaddingPx: 20, protectedRegions: [] },
            handPath: { start: [300, 500], end: [800, 500], easing: 'easeInOut' } },
          { id: 'sub', label: '第二主体', sequence: 2,
            narrativeRole: '关键变化：讲清结论', subtitle: '第二条字幕',
            type: 'structure', region: { x: 1000, y: 300, width: 500, height: 400 },
            reveal: { direction: 'left_to_right', startMs: 3100, durationMs: 2600,
                      maskPaddingPx: 20, protectedRegions: [] },
            handPath: { start: [1000, 500], end: [1500, 500], easing: 'easeInOut' } },
        ],
      };
      ann.value = JSON.stringify(tpl, null, 2);
      toast('模板已载入（记得改 canvas 成源图真实像素）');
    }

    async function annotate() {
      if (!hd.project) { err('还没有工程'); return; }
      if (!hd.upImage) { err('先上传这一幕的线稿图'); return; }
      let obj;
      try { obj = JSON.parse(ann.value); }
      catch (e) { err('标注不是合法 JSON：' + e.message); return; }
      say('me', `③ 写标注 · ${sceneId.value}`,
        `${obj.elements ? obj.elements.length : 0} 个区域`);
      const d = await post('/api/handdraw/annotate', {
        root: hdRoot(), project: hd.project, scene_id: sceneId.value,
        image: hd.upImage, annotation: obj });
      if (d.ok === false) { err(d.err || '写标注失败'); return; }
      say('sys', `标注已登记：${d.scene} · ${d.elements} 个区域 · 幕时长 `
        + `${(d.sceneDurationMs / 1000).toFixed(1)}s · 全片位置 ${(d.sourceStartMs / 1000).toFixed(1)}s`);
      annMsg.value = '已登记';
      step.value = 4;
      await refresh();
    }

    async function approve() {
      if (!hd.project) { err('还没有工程'); return; }
      const d = await post('/api/handdraw/approve',
        { root: hdRoot(), project: hd.project });
      if (d.ok === false) { err(d.err || '批准失败'); return; }
      say('sys', `时间轴已批准（画面 ${(d.picture_total_ms / 1000).toFixed(1)}s ≥ 字幕 `
        + `${(d.cue_total_ms / 1000).toFixed(1)}s），现在可以正式渲染`);
      step.value = 5;
      await refresh();
    }

    async function render(preview) {
      if (!hd.project) { err('还没有工程'); return; }
      say('me', preview ? `⑤ 短预览 · ${sceneId.value}` : `⑤ 渲染这一幕 · ${sceneId.value}`);
      const d = await post('/api/handdraw/render', {
        root: hdRoot(), project: hd.project, scene_id: sceneId.value,
        preview_ms: preview ? 3000 : 0 });
      if (d.ok === false) { err(d.err || '渲染未启动'); return; }
      chats.value.push({ run: true, who: 'sys',
        text: preview ? `短预览渲染中 · ${sceneId.value}…` : `渲染中 · ${sceneId.value}…`,
        pct: 0, meta: d.eta_hint || '' });
    }

    async function audition() {
      const text = (voiceText.value || '').trim();
      if (!text) { err('先填试听文案'); return; }
      if (!allowNet.value) { err('试听是联网合成（Edge TTS），请先勾「联网许可」'); return; }
      const d = await post('/api/handdraw/voice', {
        root: hdRoot(), text, allow_network: true, project: hd.project || '' });
      if (d.ok === false) { err(d.err || '试听失败'); return; }
      kv('音色试听', Object.entries(d).filter(([k]) => k !== 'ok')
        .map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v).slice(0, 120)]));
    }

    async function mix() {
      if (!hd.project) { err('还没有工程'); return; }
      if (!allowNet.value) { err('配音是联网合成，请先勾「联网许可」'); return; }
      say('me', '⑥ 配音合成', '成片落项目目录/AI生成');
      const d = await post('/api/handdraw/mix',
        { root: hdRoot(), project: hd.project, index: 'none' });
      if (d.ok === false) { err(d.err || '合成未启动'); return; }
      chats.value.push({ run: true, who: 'sys', text: '配音合成中…', pct: 0,
                         meta: d.eta_hint || '' });
    }

    async function qa() {
      if (!hd.project) { err('还没有工程'); return; }
      const d = await api(`/api/handdraw/status?root=${encodeURIComponent(hdRoot())}`
        + `&project=${encodeURIComponent(hd.project)}`);
      if (d.ok === false) { err(d.err || '质检失败'); return; }
      kv('质检', [
        ['时间轴状态', d.timelineStatus],
        ['幕数', Array.isArray(d.scenes) ? d.scenes.length : d.scenes],
        ['画面总长', (d.picture_total_ms / 1000).toFixed(1) + 's'],
        ['字幕总长', (d.cue_total_ms / 1000).toFixed(1) + 's'],
        ['结论', d.ok ? '时长足够' : ('画面短 ' + (d.shortfall_ms / 1000).toFixed(1) + 's')],
      ]);
    }

    /* 渲染/混流进度 → 最后一条运行中的气泡 */
    function lastRun() {
      for (let i = chats.value.length - 1; i >= 0; i--) {
        if (chats.value[i].run) return chats.value[i];
      }
      return null;
    }
    const offTick = BUS.on('hd:tick', (root, pct, msg) => {
      let m = lastRun();
      if (!m) {
        m = { run: true, who: 'sys', text: '手绘任务进行中…', pct: 0, meta: '' };
        chats.value.push(m);
      }
      m.pct = Math.max(m.pct || 0, pct || 0);
      m.text = msg || m.text;
      m.meta = `${m.pct}%`;
    });
    const offDone = BUS.on('hd:done', s => {
      const m = lastRun();
      if (m) chats.value.splice(chats.value.indexOf(m), 1);
      if (s.error) { err(s.error); return; }
      const f = s.file || s.library_file || '';
      if (f) media(f, `⑥ 完成：${s.name || ''}`, false);
      else say('sys', '手绘任务完成：' + (s.name || ''));
      step.value = 6;
      refresh();
    });
    onUnmounted(() => { offTick(); offDone(); });

    onMounted(loadInfo);

    // 回归脚本的驱动口子：e2e 要直接设工程、触发渲染（不走 UI 一层层点）
    window.__FS.hd = hd;
    window.__FS.hdApi = { refresh, regions, render, suggest, loadInfo };

    return {
      hd, step, pname, ptitle, targetSec, sceneId, ann, annMsg, scenes,
      tlStatus, tlAdvice, voiceText, allowNet, chats, projText,
      upload, init, suggest, regions, tplAnn, annotate, approve, render,
      audition, mix, qa, sceneText,
      clearChat: () => { chats.value = []; },
      hdUrl,
    };
  },
};

/* ==================== 方框复查 ====================
   这个模块回答的是一个 verify.py 回答不了的问题：
   「框到底对不对」最终得用眼睛看。所以这里把三样东西摆在一起——
     台词帧  那一秒的真实画面
     台词框  库里的方框凭证画上去（蓝=库的，绿虚线=现场重检的）
     文字    这条入库文本 vs 这一帧现场重新 OCR 出来的文本                    */
const BoxView = {
  template: '#tpl-box',
  setup() {
    const raw = ref([]);                  // 整份片单（接口只拉一次）
    const libKey = ref('');               // 库筛选
    const vq = ref('');                   // 片名搜索（跟台词过滤的 lq 分开）
    const pick = ref('');                 // "root|id"
    const rep = ref(null);                // 清单报告
    const list = ref([]);                 // 台词条目
    const cur = ref(null);                // 当前台词 id
    const filter = ref('all');
    const lq = ref('');
    const busy = ref(false);
    const probeOut = ref('');
    const liveTxt = ref('');
    const liveCls = ref('dim');
    const liveBox = ref(true);
    const imgSeq = ref(0);
    const imgLoading = ref(true);
    const imgErr = ref('');
    const editDlg = ref(false);
    const editForm = reactive({ text: '', start: 0, end: 0, save_box: false });
    const root_ = ref(''), vid_ = ref(0);

    /* 清单请求的代次号。
       坑（真踩过）：进页面触发一次 loadBoxVideo，select 的 change 又触发一次，
       两个请求同时在飞，先发的那个后回来就把刚保存的改动抹掉——
       表现是"toast 说已保存，面板却又变回旧文字"。
       办法：每次请求领号，回来时号不对就丢弃；保存改动时把号 +1 作废全部在途请求。 */
    let SEQ = 0;

    /* 两级筛选：先按素材库，再按片名关键字（多词要全部命中）。
       全在本地做——接口只拉一次全量概览（一条 SQL 聚合，秒回）。 */
    const videoList = computed(() => {
      const terms = (vq.value || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
      return raw.value.filter(x => {
        if (libKey.value && x.root !== libKey.value) return false;
        if (!terms.length) return true;
        const nm = String(x.name || '').toLowerCase();
        return terms.every(t => nm.includes(t));
      });
    });

    const libOpts = computed(() => {
      const order = [], cnt = new Map(), nm = new Map();
      raw.value.forEach(x => {
        if (!cnt.has(x.root)) { cnt.set(x.root, 0); nm.set(x.root, x.lib); order.push(x.root); }
        cnt.set(x.root, cnt.get(x.root) + 1);
      });
      return [{ value: '', label: `全部库（${raw.value.length}）` }].concat(
        order.map(r => ({ value: r, label: `${nm.get(r) || r}（${cnt.get(r)}）` })));
    });

    const pickOpts = computed(() => {
      const showLib = !libKey.value;      // 全库模式下带库名，否则同名片分不清
      return videoList.value.map(x => ({
        value: `${x.root}|${x.id}`,
        label: `${x.name}${showLib ? ' · ' + (x.lib || '') : ''}`
             + `${x.no_box ? ` · 无框 ${x.no_box}/${x.lines}` : ''}`,
      }));
    });

    const hitText = computed(() => raw.value.length
      ? `命中 ${videoList.value.length} / ${raw.value.length} 部` : '');

    async function loadLibs_() {
      let libs = [];
      try { libs = await api('/api/boxaudit/library'); } catch (e) { libs = []; }
      raw.value = libs;
      S.boxNoBox = libs.reduce((a, x) => a + (x.no_box || 0), 0);
      const keep = pick.value;
      const still = pickOpts.value.some(o => o.value === keep);
      if (still) { pick.value = keep; return; }
      if (pickOpts.value.length) { pick.value = pickOpts.value[0].value; await loadVideo(); }
      else { pick.value = ''; clearRight(); }
    }

    function clearRight(msg) {
      SEQ++;
      rep.value = null; list.value = []; cur.value = null;
      probeOut.value = ''; liveTxt.value = '';
      imgErr.value = msg || '';
    }

    async function loadVideo(force) {
      const v = pick.value;
      if (!v) return;
      if (!force && v === `${root_.value}|${vid_.value}` && rep.value) return;
      const [root, id] = v.split('|');
      const my = ++SEQ;
      root_.value = root; vid_.value = +id;
      imgLoading.value = true;
      let r;
      try { r = await api(`/api/boxaudit/list?root=${encodeURIComponent(root)}&video_id=${id}`); }
      catch (e) { imgErr.value = '读不出来，看服务端日志'; return; }
      if (my !== SEQ) return;             // 已经有更新的请求或改动了，这份作废
      rep.value = r;
      list.value = r.items || [];
      if (list.value.length) select(list.value[0].id);
      else { cur.value = null; imgErr.value = '这个素材没有台词'; }
    }

    const visible = computed(() => {
      const q = (lq.value || '').trim().toLowerCase();
      return list.value.filter(x => {
        if (filter.value === 'bad' && (x.skipped || x.ok)) return false;
        if (filter.value === 'nobox' && x.has_box) return false;
        if (q && !(x.text || '').toLowerCase().includes(q)) return false;
        return true;
      });
    });

    const curRow = computed(() => list.value.find(x => x.id === cur.value) || null);

    function cls(x) {
      if (!x) return '';
      if (x.skipped) return 'skip';
      if (!x.has_box) return 'nobox';
      return x.ok ? 'ok' : 'bad';
    }
    function mark(x) {
      if (!x) return '';
      if (x.skipped) return '无框(正常)';
      if (!x.has_box) return '无框凭证';
      const base = x.ok ? '方框已验' : ((x.reasons || [])[0] || '方框异常');
      // 字体偏离是另一个维度：不判伪，追加一个提示徽标
      return base + (((x.metrics || {}).font_dev || 0) > 1.5 ? ' · 字体异常' : '');
    }

    function select(id) {
      if (cur.value !== id) probeOut.value = '';   // 换句就丢掉上一句的识别报告
      cur.value = id;
      liveTxt.value = ''; liveCls.value = 'dim';
      imgSeq.value++; imgLoading.value = true;
    }

    /* live=1 只在**这一句刚跑过识别**时才传。服务端 live=1 会重抽一帧跑一遍
       OCR（1~3 秒），无条件带上会让翻一句等三秒；没识别过也没东西可叠。 */
    const frameUrl = computed(() => {
      const x = curRow.value;
      if (!x) return '';
      const p = new URLSearchParams({
        root: root_.value, video_id: vid_.value, line_id: x.id, view: 'full',
        live: (liveBox.value && probeOut.value) ? '1' : '0',
        seq: String(imgSeq.value),
      });
      return `/api/boxaudit/frame?${p}`;
    });
    watch(liveBox, () => { imgSeq.value++; });

    function step(d) {
      const rows = visible.value;
      if (!rows.length) return;
      const i = rows.findIndex(x => x.id === cur.value);
      const n = rows[Math.max(0, Math.min(rows.length - 1, (i < 0 ? 0 : i + d)))];
      if (n) select(n.id);
    }
    function onImgErr() { imgErr.value = '这一帧抽不出来（文件可能不在了）'; imgLoading.value = false; }

    const metricsRows = computed(() => {
      const m = (curRow.value || {}).metrics;
      if (!m) return [];
      const rows = [
        ['框心 cx/cy', `${m.cx ?? '—'} / ${m.cy ?? '—'}`],
        ['框宽 × 字高', `${m.bw ?? '—'} × ${m.bh ?? '—'}`],
        ['字宽比 ar', m.ar ?? '—', m.ar != null && (m.ar < 0.55 || m.ar > 1.45) ? 'bad' : 'ok'],
        ['置信度', m.conf ?? '—', m.conf != null && m.conf < 0.62 ? 'bad' : 'ok'],
        ['支持帧数', m.hits ?? 0],
        ['覆盖率', m.cov == null ? '—' : Math.round(m.cov * 100) + '%',
          m.cov != null && (m.cov < 0.34 || m.cov > 1.25) ? 'bad' : 'ok'],
        ['跨帧漂移', m.drift ?? '—', m.drift != null && m.drift > 0.10 ? 'bad' : 'ok'],
        ['框数 / 字数', `${m.nbox ?? '—'} / ${m.nchar ?? '—'}`],
      ];
      const fnt = m.font || {};
      if (fnt.ink != null) {
        rows.push(['字体 墨密度/笔画', `${fnt.ink} / ${fnt.stroke ?? '—'}px`]);
        rows.push(['字高占画面', fnt.px_h != null ? (fnt.px_h * 100).toFixed(1) + '%' : '—']);
        rows.push(['字体偏离度', m.font_dev ?? '—',
          m.font_dev != null && m.font_dev > 1.5 ? 'warn' : 'ok']);
      }
      return rows;
    });

    async function doProbe() {
      const x = curRow.value;
      if (!x || busy.value) return;
      busy.value = true;
      probeOut.value = '<div class="dim">正在抽 3 帧重新跑 OCR，约 1~3 秒…</div>';
      try {
        const r = await post('/api/boxaudit/probe',
          { root: root_.value, video_id: vid_.value, line_id: x.id });
        probeOut.value =
          `<div class="bxpv ${r.verdict === 'OK' ? 'ok' : 'bad'}">
             <b>${r.verdict === 'OK' ? '识别一致' : '识别不一致'}</b>
             <span class="dim">${esc(r.detail || '')}</span></div>
           <table class="bxpbtbl">${(r.frames || []).map(f => `
             <tr class="${f.match === true ? 'ok' : (f.match === false ? 'bad' : '')}">
               <td>${f.t.toFixed(2)}s</td><td>${esc(f.text || '—')}</td>
               <td>${f.box ? `偏移 ${f.dx ?? '?'} / ${f.dy ?? '?'}` : '没找到字幕框'}</td>
               <td>${esc(f.why || '')}</td></tr>`).join('')}</table>`;
        const lt = (r.frames || []).filter(f => f.box).map(f => f.text).filter(Boolean);
        if (lt.length) {
          liveTxt.value = lt[0];
          liveCls.value = r.verdict === 'OK' ? 'ok' : 'bad';
        }
        toast(r.verdict === 'OK' ? '重新识别完成，结果一致'
                                 : '重新识别完成，结果对不上：' + (r.detail || ''),
              r.verdict === 'OK' ? 'success' : 'warning');
      } catch (e) {
        probeOut.value = `<div class="bad">识别失败：${esc(String(e))}</div>`;
      } finally {
        // 报告必须留着：保存后同句重渲染还会用到，且 live 出图靠它判断
        // 要不要真的去重跑 OCR。顺序不能反，反了这次就白出一次不带框的图。
        imgSeq.value++;
        busy.value = false;
      }
    }

    /* 单句语音识别：识别文本**不自动写库**——出现在右侧那一栏，
       用户自己比对后决定改不改。自动覆盖人工校对过的台词不能接受。 */
    async function doAsr() {
      const x = curRow.value;
      if (!x || busy.value) return;
      busy.value = true;
      probeOut.value = '<div class="dim">正在抽这段音频跑语音识别（首次要加载模型，约 10 秒）…</div>';
      try {
        const r = await post('/api/boxaudit/asr',
          { root: root_.value, video_id: vid_.value, line_id: x.id });
        if (!r.ok) throw new Error(r.err || '识别失败');
        probeOut.value =
          `<div class="bxpv ${r.text ? 'ok' : 'bad'}">
             <b>语音识别${r.text ? '完成' : '没听出内容'}</b>
             <span class="dim">${esc(r.dur)}s 音频 · ${esc(r.device || r.model || '')}</span></div>
           <div class="bxfld" style="margin-top:8px"><span>听到的</span>
             <b>${esc(r.text || '（这一段没有人声）')}</b></div>
           ${r.text ? '<div class="dim" style="margin-top:7px">觉得对就点「编辑台词」'
             + '把它抄进去，识别本身不会自动改库。</div>' : ''}`;
        liveTxt.value = r.text || '（没听出内容）';
        liveCls.value = r.text ? 'warn' : 'dim';
        toast(r.text ? '语音识别完成，觉得对就点「编辑台词」抄进去' : '这一段没听出内容',
              r.text ? 'success' : 'warning');
      } catch (e) {
        probeOut.value = `<div class="bad">语音识别失败：${esc(String((e && e.message) || e))}</div>`;
        toast('语音识别失败', 'error');
      } finally { busy.value = false; }
    }

    function openEdit() {
      const x = curRow.value;
      if (!x) return;
      editForm.text = x.text || '';
      editForm.start = x.start || 0;
      editForm.end = x.end || 0;
      editForm.save_box = false;
      editDlg.value = true;
    }

    async function saveEdit() {
      const x = curRow.value;
      if (!x) return;
      editDlg.value = false;
      try {
        const r = await post('/api/boxaudit/edit', {
          root: root_.value, video_id: vid_.value, line_id: x.id,
          text: editForm.text, start: Number(editForm.start),
          end: Number(editForm.end), save_box: !!editForm.save_box,
        });
        if (!r.ok) {
          ElementPlus.ElMessageBox.alert(r.err || '未知原因', '没保存');
          return;
        }
        const v = r.verify || {};
        toast(v.ok ? '已保存，这条现在过校验'
                   : '已保存，但校验提示：' + ((v.reasons || []).join('、') || '框还没写'),
              v.ok ? 'success' : 'warning');
        // 只把这一条的新摘要打回列表，不重载整份清单：
        // 重载会把这部片的每一行都重跑 verify（一部片两千条要好几秒）。
        const patch = r.audit;
        if (patch && patch.id === x.id) {
          SEQ++;                          // 作废在途清单请求，否则它会把改动抹掉
          list.value = list.value.map(y => (y.id === x.id ? patch : y));
          if (rep.value) rep.value.items = list.value;
          refreshSummary();
          select(x.id);
        } else {
          await loadVideo(true);
        }
      } catch (e) {
        ElementPlus.ElMessageBox.alert(String((e && e.message) || e), '保存失败');
      }
    }

    /* 只重算顶部汇总，不重跑整份清单的 verify */
    function refreshSummary() {
      const r = rep.value;
      if (!r) return;
      const items = list.value;
      const checked = items.filter(x => !x.skipped);
      const bad = checked.filter(x => !x.ok);
      const nobox = items.filter(x => !x.has_box);
      const reason = {};
      for (const x of bad) {
        for (const rr of (x.reasons || [])) {
          const k = String(rr).split(' ')[0];
          reason[k] = (reason[k] || 0) + 1;
        }
      }
      r.total = items.length;
      r.checked = checked.length;
      r.passed = checked.length - bad.length;
      r.failed = bad.length;
      r.skipped = items.length - checked.length;
      r.no_box = nobox.length;
      r.rate = checked.length ? (checked.length - bad.length) / checked.length : 0;
      r.reasons = reason;
      S.boxNoBox = nobox.length;
    }

    /* 库/片名筛选变了：选中那部还在结果里就保持，被筛掉就落到第一条 */
    watch([libKey, vq], () => {
      const keep = pick.value;
      if (keep && pickOpts.value.some(o => o.value === keep)) return;
      if (pickOpts.value.length) { pick.value = pickOpts.value[0].value; loadVideo(); }
      else { pick.value = ''; clearRight('没有匹配的片子'); }
    });
    watch(pick, v => { if (v) loadVideo(); });

    function reload() { loadLibs_(); }

    // 回归脚本的状态口子：选片两级筛选的断言要读片单和当前选中
    window.__FS.box = {
      get raw() { return raw.value; },
      get list() { return list.value; },
      get rep() { return rep.value; },
      get pick() { return pick.value; }, set pick(v) { pick.value = v; },
      get libKey() { return libKey.value; }, set libKey(v) { libKey.value = v; },
      get vq() { return vq.value; }, set vq(v) { vq.value = v; },
      get filtered() { return videoList.value.length; },
    };

    onMounted(() => {
      loadLibs_();
      // 切换页签会重新挂载，监听和 keydown 都要跟着解绑，否则越积越多
      const offRefresh = BUS.on('box:refresh', () => {
        if (S.tab === 'box') loadLibs_();
      });
      // ↑/↓ 换句。输入框里打字时不抢按键。
      const onKey = e => {
        if (S.tab !== 'box') return;
        const t = (e.target.tagName || '').toUpperCase();
        if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return;
        if (document.querySelector('.el-overlay')) return;
        if (e.key === 'ArrowDown') { e.preventDefault(); step(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); step(-1); }
      };
      document.addEventListener('keydown', onKey);
      onUnmounted(() => {
        offRefresh();
        document.removeEventListener('keydown', onKey);
      });
    });

    return {
      raw, libKey, vq, pick, rep, list, cur, filter, lq, busy, probeOut,
      liveTxt, liveCls, liveBox, imgLoading, imgErr, editDlg, editForm,
      libOpts, pickOpts, hitText, visible, curRow, frameUrl, metricsRows,
      select, step, cls, mark, doProbe, doAsr, openEdit, saveEdit,
      onImgErr, reload,
      play: () => {
        const x = curRow.value;
        if (x) play(vid_.value, x.start, x.text || '', root_.value);
      },
    };
  },
};

/* ==================== 片段 ==================== */
const ClipsView = {
  template: '#tpl-clips',
  setup() {
    return {
      clips: computed(() => S.clips),
      pre: computed(() => S.pre), post: computed(() => S.post),
      clipUrl: c => `/clip?p=${encodeURIComponent(c.rel)}`
        + `&root=${encodeURIComponent(c.root)}`,
      openDir: c => post('/api/open',
        { path: (c.root || '') + '\\.frameseek\\clips' }),
    };
  },
};

/* ==================== 主应用 ==================== */
const App = {
  components: { LibsView, SearchView, AssetsView, GenView, HanddrawView, BoxView, ClipsView },
  setup() {
    const videoEl = ref(null);

    const statsText = computed(() => (S.libs || []).length
      ? `${S.libs.length} 个库 · ${S.totals.videos} 个素材 · ${S.totals.lines} 条台词`
      : '还没有登记素材库');

    // AI 生成任务不进全局浮窗：生成页的对话气泡里已经有进度条和状态，
    // 右下角再弹一张卡是重复信息，而且生成页切走再切回来（keep-alive）气泡还在。
    const taskList = computed(() => Object.values(S.tasks || {})
      .filter(t => t.kind !== 'gen'));

    function dockPct(t) { return t.total ? Math.min(100, Math.round(t.done / t.total * 100)) : 0; }
    function dockHead(t) {
      if (t.kind === 'video') return t.file || t.message || '';
      if (t.kind === 'gen') return t.message || '';
      return `${t.done || 0}/${t.total || 0} 个素材`;
    }
    function dockPhase(t) { return PHASE[t.phase] || ''; }
    function dockEta(t) {
      if (!(t.eta_min > 0)) return '';
      return ` · 还剩约 ${t.eta_min < 1 ? Math.ceil(t.eta_min * 60) + ' 秒'
                                        : Math.round(t.eta_min) + ' 分钟'}`;
    }

    function onTab(name) {
      S.tab = name;
      if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
    }

    function onPlayerClosed() {
      const v = videoEl.value;
      if (v) { v.pause(); v.src = ''; }
    }
    async function clipHere() {
      const v = videoEl.value;
      if (!v || !S.player.cur) return;
      const c = S.player.cur;
      const r = await api('/api/clip', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root: c.root, video_id: c.video_id,
          start: v.currentTime, end: v.currentTime + 1,
          pre: S.pre, post: S.post, fast: S.fast }),
      });
      await addClips(r);
      S.player.on = false;
    }

    onMounted(async () => {
      await loadLibs();
      const h = (location.hash || '').slice(1);
      const tabs = ['libs', 'search', 'assets', 'gen', 'hd', 'box', 'clips'];
      onTab(tabs.includes(h) ? h : 'libs');
      connectLive();
      // 首屏可能正有任务在跑（服务端线程还活着）。只当临时快照——
      // WS 一旦推来真事件就会覆盖它；WS 不可用时它才长期顶替轮询。
      if (!LIVE.on) {
        let ts = [];
        try { ts = await api('/api/tasks'); } catch (e) { ts = []; }
        (ts || []).forEach(t => {
          if (!t.root || t.status !== 'running') return;
          S.tasks[t.root] = { root: t.root, lib: t.lib, done: t.done, total: t.total,
            message: t.message, phase: '', pct: t.pct, kind: t.kind || '' };
        });
      }
    });

    return {
      S, tab: computed(() => S.tab), onTab, statsText, taskList, videoEl,
      dockPct, dockHead, dockPhase, dockEta, onPlayerClosed, clipHere,
      libs: computed(() => S.libs), ocr: computed(() => S.ocr),
      viewCount: computed(() => S.viewCount),
      boxNoBox: computed(() => S.boxNoBox),
      clips: computed(() => S.clips),
      player: computed(() => S.player),
      dockOpen: computed({ get: () => S.dockOpen, set: v => { S.dockOpen = v; } }),
    };
  },
};

/* ==================== 挂载 ==================== */
const app = Vue.createApp(App);
app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
// 图标全量注册：模板里直接写 <Search/> <Film/> 等
Object.keys(ElementPlusIconsVue || {}).forEach(k => {
  const c = ElementPlusIconsVue[k];
  if (c && (c.render || c.setup || c.template)) app.component(k, c);
});
app.mount('#app');

// 给回归脚本留的口子：Playwright 要在没有真实交互的场景下驱动页面
window.__FS = { S, api, post, play, toast, loadLibs };

})();
