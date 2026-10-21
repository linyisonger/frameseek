/* 拾帧 FrameSeek · 共享状态层（被各功能模块共用）
   这里只放「所有页面都要用」的东西：工具函数、全局响应式状态 S、
   事件总线 BUS、实时进度通道、素材库/播放器/片段导出。
   各功能组件拆在 web/js/*.js，各自引用本文件的 window.FS。 */
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
  activeRoot: '',
  hubDb: '',
  totals: { videos: 0, lines: 0, duration: 0 },
  ocr: { ok: true, err: '', loaded: false },
  step: 0.2,           // OCR 采样步长，跟服务端 config.OCR_STEP 保持一致
  all: [],             // 跨库素材全量
  viewCount: 0,        // 素材页当前过滤后的条数（给侧栏徽标）
  boxNoBox: 0,         // 方框复查无框条数（侧栏徽标）
  clips: [],
  pre: 5, post: 5, fast: true,
  /* 导出烧字幕：auto | off | force。
     auto = 画面本来就带字幕的素材（台词来自 OCR 识别）自动跳过烧录，其余照旧烧，
     避免在已有字幕的画面下方压出一道重影，还白白强制重编码慢好几倍。
     force 是给「OCR 字幕有错字、纠完想重烧一份」留的出口。
     判定逻辑在服务端 core/media.py decide_burn，前后端只有这一处规则。 */
  subMode: 'auto',
  subBand: false,          // 预览字幕带：播放时在画面底部显示当前台词，默认关闭
  tasks: {},
  dockOpen: false,      // 右下角浮窗默认收起（有任务也只露一条细条，要进度展开）
  player: { on: false, title: '', src: '', cur: null, sub: '' },
  theme: 'dark',         // 拾帧只支持深色模式，保留字段以兼容老 LS / 模板引用
});

/* ---------- 实时进度：WebSocket → SSE → 轮询 ---------- */
const LIVE = { on: false, mode: 'none', ws: null, es: null, poll: null, known: new Set() };

const PHASE = { ocr: 'OCR 识别', shots: '截图分片', calib: '字幕标定',
                cover: '封面', gif: '动图', lines: '台词', clear: '清理旧台词',
                proxy: '压 1080p 代理' };

function onLive(ev) {
  if (!ev || ev.type === 'ping' || ev.type === 'hello') return;
  // 事件总线会给新连接重放最近 200 条，好让刚打开的页面立刻知道"正在跑什么"。
  // 但重放的是**历史**——几分钟前就结束的任务也会被补进来，浮窗就会挂着一张
  // 永远不消失的幽灵卡片（进度停在最后一格，停止按钮点了还 404）。
  // 正常进度每 2 秒推一次，所以超过 20 秒的事件一律不当"当前状态"用。
  if (ev.ts && (Date.now() / 1000 - ev.ts) > 20) return;

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
    return;
  }

  if (ev.type === 'task_done') {
    const k = ev.root || '';
    delete S.tasks[k];       // 先删卡片，否则会一直显示 100% 不消失
    const s = ev.summary || {};

    if (s.stopped) {
      // 用户按了「停止」。已完成的部分都留在库里了，下次索引会跳过它们接着跑。
      toast(s.single
        ? `已停止「${s.name || ''}」的识别，下次会重跑这一条`
        : `已停止「${ev.lib || ''}」索引：完成 ${s.done || 0} 个，剩 ${s.remain || 0} 个；`
          + `下次点「索引」接着往下跑`, 'warning');
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
/* 字幕带：开启 S.subBand 后，播放时按当前时间高亮对应台词 */
let _subLines = null;
function _fetchSubLines(video_id, root) {
  _subLines = [];
  fetch(`/api/lines?video_id=${video_id}&root=${encodeURIComponent(root || '')}`)
    .then(r => (r.ok ? r.json() : []))
    .then(rows => { _subLines = Array.isArray(rows) ? rows : []; })
    .catch(() => { _subLines = []; });
}
function _updateSubtitle(t) {
  if (!_subLines || !_subLines.length) {
    if (S.player.sub) S.player.sub = '';
    return;
  }
  let cur = '';
  for (const ln of _subLines) {
    const s = ln.start || 0, e = ln.end || (ln.start || 0) + 3;
    if (t >= s && t < e) { cur = ln.text || ''; break; }
  }
  if (S.player.sub !== cur) S.player.sub = cur;
}

function play(video_id, start, title, root) {
  S.player.cur = { video_id, root: root || '' };
  S.player.title = title || '预览';
  S.player.src = `/stream/${video_id}?root=${encodeURIComponent(root || '')}`;
  S.player.start = start || 0;
  S.player.sub = '';
  S.player.on = true;
  nextTick(() => {
    const v = document.querySelector('.fs-player-video');
    if (!v) return;
    v.src = S.player.src;
    v.onloadedmetadata = () => {
      v.currentTime = Math.max(0, (start || 0) - Math.min(S.pre || 5, 2));
      v.play().catch(() => {});
    };
    // 字幕带：开启时拉取整片台词，随播放进度高亮当前句
    if (S.subBand) {
      v.ontimeupdate = () => _updateSubtitle(v.currentTime);
      _fetchSubLines(video_id, root);
    } else {
      v.ontimeupdate = null;
      _subLines = null;
    }
  });
}

/* ---------- 片段导出 ---------- */
async function addClips(r) {
  if (!r || !r.ok) { toast((r && r.err) || '导出失败', 'error'); return; }
  // 打上导出时刻：列表按时间倒序，后端还没重扫目录时新片段也能排在最前
  const now = Date.now() / 1000;
  const got = (r.clips || []).map(c => Object.assign({ mtime: now }, c));
  S.clips = got.concat(S.clips);
  S.tab = 'clips';
  // 自动跳过烧录时说明原因（比如 OCR 源画面本来就有字幕），否则用户会以为开关没生效
  if (r.sub_note) toast(r.sub_note, 'warning');
}
/* opts 可选：{ audio_track, sub_track, subtitle }
   —— 多轨片由检索页先探轨道、弹框让用户挑，挑完把选择带进来；
      单轨片不传 opts，行为跟以前一模一样。 */
function clipLine(line_id, root, opts) {
  const body = { root, line_id, pre: S.pre, post: S.post, fast: S.fast,
    subtitle: (opts && opts.subtitle) || S.subMode };
  if (opts) {
    if (opts.audio_track != null) body.audio_track = opts.audio_track;
    if (opts.sub_track != null) body.sub_track = opts.sub_track;
  }
  return api('/api/clip', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(addClips);
}

/* ==================== URL 路由：#页签?参数 ====================
   整站只有一个 hash，没有 vue-router。约定：
     #assets                 → 素材页，不带筛选
     #assets?lib=1,2&q=片名&sort=mtime&dur=s30,m30&vid=123&root=D:\片库
   参数会被各页面消费（目前只有素材页），同时素材页的筛选变化也会反写回 URL
   （replace，不塞满历史），这样链接可以直接复制分享 / 浏览器前进后退可用。 */
const TABS = ['libs', 'search', 'assets', 'box', 'clips'];

const Route = {
  tabs: TABS,
  q: {},                        // 当前 hash 上的参数（assets 页读它）
  build(tab, q) {
    const p = new URLSearchParams();
    Object.keys(q || {}).forEach(k => {
      const v = q[k];
      if (v === '' || v == null || v === false) return;
      if (Array.isArray(v) ? !v.length : false) return;
      p.set(k, Array.isArray(v) ? v.join(',') : String(v));
    });
    const s = p.toString();
    return '#' + tab + (s ? '?' + s : '');
  },
  parse() {
    const h = (location.hash || '').replace(/^#/, '');
    const i = h.indexOf('?');
    const tab = (i < 0 ? h : h.slice(0, i)) || '';
    const q = {};
    if (i >= 0) {
      try {
        new URLSearchParams(h.slice(i + 1)).forEach((v, k) => { q[k] = v; });
      } catch (e) { /* 参数畸形就当没带 */ }
    }
    return { tab, q };
  },
  /* 解析当前 hash → 同步 tab 状态 → 广播。各页面在这里读 q。 */
  dispatch(hard) {
    const r = Route.parse();
    // 下线模块 / 旧书签：不在 TABS 里的页签统一回素材库，并把 hash 改回去
    if (r.tab && Route.tabs.indexOf(r.tab) < 0) {
      r.tab = 'libs';
      try { history.replaceState(null, '', '#libs'); } catch (e) { /* 忽略 */ }
    }
    Route.q = r.q;
    if (Route.tabs.indexOf(r.tab) >= 0 && r.tab !== S.tab) S.tab = r.tab;
    BUS.emit('route:change', { tab: r.tab, q: r.q, hard: !!hard });
  },
  /* 写 URL 并立刻广播。replace 默认 true（筛选联动不塞历史）；
     push 用于「从素材库点某部影片跳到素材页」这种明确的一次跳转。 */
  set(tab, q, replace) {
    const h = Route.build(tab, q);
    try {
      if (replace === false) history.pushState(null, '', h);
      else history.replaceState(null, '', h);
    } catch (e) { location.hash = h.slice(1); return; }
    if (h === (location.hash || '')) { Route.dispatch(); return; }
    Route.dispatch();
  },
  init() {
    window.addEventListener('hashchange', () => Route.dispatch(true));
    window.addEventListener('popstate', () => Route.dispatch(true));
    Route.dispatch(true);
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

/* ==================== 对外暴露 ==================== */
// 各功能模块从 window.FS 取自己要的东西，互不耦合。
window.FS = {
  S, BUS, LIVE, PHASE, Route,
  api, post, esc, hl, mediaUrl,
  fmtDur, fmtAgo, fmtSec, toast, LS,
  connectLive, loadLibs, play, clipLine, addClips, startPoll,
};

// 给回归脚本留的口子（基础项；box 会在自己模块里追加）。
window.__FS = { S, api, post, play, toast, loadLibs, Route };

})();
