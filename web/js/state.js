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
    } else if (s.stopped) {
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
  S, BUS, LIVE, PHASE,
  api, post, esc, hl, mediaUrl, hdUrl,
  fmtDur, fmtAgo, fmtSec, toast, LS,
  connectLive, loadLibs, play, clipLine, addClips, startPoll,
};

// 给回归脚本留的口子（基础项；gen/hd/box 会在各自模块里追加）。
window.__FS = { S, api, post, play, toast, loadLibs };

})();
