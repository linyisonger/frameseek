/* 拾帧 FrameSeek - 前端 */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

let LIBS = [];        // 登记的素材库
let BUILTIN_LIBS = [];// 内置只读库（AI 生成），不进素材库卡片列表
let ACTIVE_ROOT = ''; // 当前激活库路径（/api/state 的 active）
let ALL = [];         // 跨库素材列表（全量）
let VIEW = [];        // 当前过滤后、正在显示的列表（卡片菜单按它的下标定位）
let CLIPS = [];       // 本次导出的片段
let CUR = { video_id: 0, start: 0, root: '' };
let poll = null;
let STEP = 0.2;       // OCR 采样步长（秒）。页面加载时用 /api/state 的 ocr_step 覆盖，
                     // 和服务端 core/config.py 的 OCR_STEP 保持一致。

const LS = {
  get(k, d) { try { const v = localStorage.getItem('fs_' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('fs_' + k, JSON.stringify(v)); } catch (e) {} }
};

function api(url, opt) { return fetch(url, opt).then(r => r.json()); }
function esc(s) {
  // 必须先转字符串：esc 会被喂数字（比如接口返回的 r.dur 秒数），
  // 原来直接 (s || '').replace 会抛 "replace is not a function"，
  // 整个渲染函数炸掉、面板一片空白。数字本来就没什么要转义的。
  return String(s ?? '').replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function hl(t, q) {
  t = esc(t);
  if (q) {
    const p = esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    t = t.replace(new RegExp(p, 'gi'), m => `<mark>${m}</mark>`);
  }
  return t;
}
function mediaUrl(p, root) {
  return `/media?p=${encodeURIComponent(p)}&root=${encodeURIComponent(root)}`;
}
function dur(s) {
  s = s || 0;
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}时${m}分` : `${m}分${Math.floor(s % 60)}秒`;
}
function ago(t) {
  if (!t) return '从未';
  const d = Date.now() / 1000 - t;
  if (d < 60) return '刚刚';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false });
}
function toast(msg) {
  let t = $('#toast');
  if (!t) {
    t = document.createElement('div'); t.id = 'toast'; t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg; t.classList.add('on');
  clearTimeout(t._tm); t._tm = setTimeout(() => t.classList.remove('on'), 2600);
}

/* ---------- 导航 ---------- */
function switchTab(name) {
  $$('.nav-item').forEach(a => a.classList.toggle('on', a.dataset.tab === name));
  $$('.panel').forEach(p => p.classList.toggle('on', p.dataset.panel === name));
  if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
  if (name === 'assets') loadAssets();
  if (name === 'clips') renderClips();
  if (name === 'box') loadBoxLibs();
  if (name === 'gen') loadGenInfo();
}
$$('.nav-item').forEach(a => a.onclick = e => { e.preventDefault(); switchTab(a.dataset.tab); });

/* ---------- 弹窗 ---------- */
function openModal(title, html) {
  $('#mTitle').textContent = title;
  $('#mBody').innerHTML = html;
  $('#modal').classList.remove('hidden');
}
function closeModal() { $('#modal').classList.add('hidden'); $('#mBody').innerHTML = ''; }

/* 弹窗里的输入项快照。
   坑（真实踩过）：closeModal 会把 #mBody 清空，而 askConfirm 的确认回调
   是**先关弹窗再执行**的——关完再读 $('#mForce').checked 只会拿到
   null/false。表现是"勾了强制重跑 OCR，发出去的还是 force:false"。
   所以要在关之前把表单值抓下来。 */
function formOf(scope) {
  const o = {};
  (scope || document).querySelectorAll('input,select,textarea').forEach(el => {
    if (!el.id) return;
    if (el.type === 'checkbox') o[el.id] = el.checked;
    else if (el.type === 'number' || el.type === 'range') o[el.id] = Number(el.value);
    else o[el.id] = el.value;
  });
  return o;
}
$('#mClose').onclick = closeModal;
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeModal(); $('#btnClosePlayer').click(); } });

/* 确认弹窗（不用原生 confirm：浏览器会「阻止此页面创建更多对话框」，之后点了就像没反应） */
function askConfirm(title, html, onYes, yesText = '确定', danger = false) {
  openModal(title, `${html}
    <div class="mfoot">
      <button id="mCancel">取消</button>
      <button id="mYes" class="${danger ? 'danger' : 'primary'}">${esc(yesText)}</button>
    </div>`);
  $('#mCancel').onclick = closeModal;
  $('#mYes').onclick = async () => {
    const form = formOf($('#mBody'));     // 必须在 closeModal 之前取
    closeModal();
    try { await onYes(form); } catch (e) { toast('操作失败：' + e); }
  };
}
function askAlert(title, msg) {
  openModal(title, `<div class="mrow">${esc(msg)}</div>
    <div class="mfoot"><button id="mYes" class="primary">知道了</button></div>`);
  $('#mYes').onclick = closeModal;
}

/* ---------- 素材库 ---------- */
async function loadLibs() {
  const s = await api('/api/state');
  LIBS = s.libraries || [];
  BUILTIN_LIBS = s.builtin_libs || [];
  ACTIVE_ROOT = s.active || '';
  OCR = s.ocr || { ok: true, err: '' };
  // 采样步长必须跟服务端一致：覆盖率 = hits / (span/step + 1)。
  // 曾经前端写死 0.5，服务端改成 0.2 后覆盖率虚高 2.5 倍、误报一片。
  if (s.ocr_step) STEP = +s.ocr_step;
  $('#asrDev').innerHTML = `<span class="${OCR.ok ? 'ok' : 'bad'}">`
    + `台词来源：${OCR.ok ? 'OCR 识别画面硬字幕' : 'OCR 不可用'}</span>`
    + (OCR.loaded ? '' : `<span class="dim"> (${OCR.ok ? '已装 RapidOCR' : esc(OCR.err || '')})</span>`);
  $('#hubDb').textContent = s.hub_db || '';
  $('#stats').innerHTML = LIBS.length
    ? `${LIBS.length} 个库 · ${s.total.videos} 个素材 · ${s.total.lines} 条台词`
    : '还没有登记素材库';
  $('#badgeLibs').textContent = LIBS.length;

  $('#libCards').innerHTML = LIBS.map(l => `
    <div class="libcard ${l.exists ? '' : 'missing'}">
      <div class="lc-hd">
        <div style="min-width:0;flex:1">
          <b title="${esc(l.name)}">${esc(l.name)}</b>
          ${l.note ? `<div class="note">${esc(l.note)}</div>` : ''}
        </div>
        ${l.exists ? (l.indexed ? '<span class="tag ok">已索引</span>'
          : '<span class="tag no">未索引</span>') : '<span class="tag bad">目录丢失</span>'}
      </div>
      <div class="lc-path" title="${esc(l.path)}">${esc(l.path)}</div>
      <div class="lc-stats">
        <div><b>${l.videos}</b><span>素材</span></div>
        <div><b>${l.lines}</b><span>台词</span></div>
        <div><b style="font-size:13px">${dur(l.duration)}</b><span>${ago(l.last_scan_at)}索引</span></div>
      </div>
      <div class="lc-ft">
        <button class="tiny primary" onclick="askScan(${l.id})"
          title="扫描这个库：新增的素材入库、抽台词（同名 .srt → 内嵌字幕 → OCR 画面硬字幕）、生成封面和动图。会先弹设置让你确认">索引</button>
        <button class="tiny" onclick="askUpload(${l.id})"
          title="把本地视频直接传进这个库，传完自动出现在素材页">上传</button>
        ${l.ignored ? `<button class="tiny" onclick="askIgnored(${l.id})"
          title="这个库里有 ${l.ignored} 个文件被标为忽略（不入库）。点开可以逐个放回" >忽略 ${l.ignored}</button>` : ''}
        <button class="tiny danger sp" onclick="delLib(${l.id},'${esc(l.name)}')"
          title="把这个库从登记里移除。只删索引，不动磁盘上的视频文件">删除</button>
      </div>
    </div>`).join('')
    || '<div class="dim">还没有素材库，在上面填路径登记一个</div>';
  renderLibChips();
  loadAssets();
}

async function delLib(id, name) {
  const l = LIBS.find(x => x.id === id);
  askConfirm(`移除「${name}」`, `
    <div class="mrow"><b>${esc(name)}</b></div>
    <div class="dim" style="margin-bottom:8px">${esc((l && l.path) || '')}</div>
    <div class="mrow">只删项目库 <b>hub.db</b> 里的一条登记记录。</div>
    <div class="mrow dim">磁盘上的素材文件、以及该目录下的 <code>.frameseek</code> 索引全部保留；重新登记这个文件夹，索引还在。</div>
    <div class="mrow dim">移除后，它下面的素材不再出现在「素材」和台词检索结果里。</div>`,
    async () => {
      let ok = false;
      try {
        const r = await api(`/api/libraries/${id}/delete`, { method: 'POST' });
        ok = !!(r && r.ok);
      } catch (e) { ok = false; }
      await loadLibs();
      toast(ok ? `已移除「${name}」` : '移除失败，看服务端日志');
    }, '移除登记', true);
}

/* 忽略名单：移除过但文件还在的素材，可放回来 */
async function askIgnored(id) {
  const l = LIBS.find(x => x.id === id);
  if (!l) return;
  let rows = [];
  try { rows = await api(`/api/ignored?root=${encodeURIComponent(l.path)}`); } catch (e) { rows = []; }
  openModal(`「${l.name}」已忽略 ${rows.length} 个`, `
    <div class="mrow dim">这些素材被「从库中移除」过，磁盘文件还在。索引时会跳过它们，放回来就会重新入库。</div>
    <div id="igList" class="iglist">${(rows || []).map(r => `
      <div class="igrow">
        <div class="min0"><b>${esc(r.name || r.md5)}</b>
          <div class="dim">${esc(r.rel || '')}</div></div>
        <button class="tiny" onclick="restoreOne('${encodeURIComponent(l.path)}','${r.md5}')"
          title="取消忽略，下次索引时这个文件会正常入库">放回</button>
      </div>`).join('') || '<div class="dim">（空）</div>'}</div>
    <div class="mfoot">
      <button id="mCancel">关闭</button>
      <button id="mIgAll" title="把这个库里所有被忽略的文件一次性取消忽略">全部放回（${rows.length}）</button>
    </div>`);
  $('#mCancel').onclick = closeModal;
  $('#mIgAll').onclick = async () => {
    await api('/api/ignored/restore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: l.path, all: true })
    });
    closeModal(); await loadLibs(); toast('已全部放回，下次索引会重新入库');
  };
}
async function restoreOne(rootEnc, md5) {
  await api('/api/ignored/restore', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ root: decodeURIComponent(rootEnc), md5 })
  });
  closeModal(); await loadLibs(); toast('已放回');
}

async function openPath(p) {
  api('/api/open', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: decodeURIComponent(p) })
  });
}

/* 语音转写耗时预估。
   实测（faster-whisper small / RTX 4060Ti fp16）：整部《功夫》99 分钟 -> 100 秒转完、853 条，
   即约 59 倍实时；900 秒连续音频纯模型 81 倍。短片段只有 27 倍是因为固定开销占比高。
   这里按 50 倍保守取值。CPU(small/int8) 实测约 6.6 倍，按 6 倍算。 */
let OCR = { ok: true, err: '' };
function fmtEst(h) {
  return h < 1 ? Math.round(h * 60) + ' 分钟' : h.toFixed(1) + ' 小时';
}
/* 台词来源提示。实测：OCR 8 进程 1.5~2.1 倍实时，即1 小时素材约 30~40 分钟；
   语音转写已停用（GPU 59 倍实时更快，但错字多、粤语会转成简体字）。 */
function asrHint(l) {
  const hours = (l.duration || 0) / 3600;
  if (!OCR.ok) return `<span class="bad">OCR 不可用：${esc(OCR.err || '')}</span>`
    + `没有字幕的素材将拿不到台词。`;
  if (!hours) return `开启后没有字幕的视频会用 <b>OCR 识别画面硬字幕</b>，`
    + `实测 8 进程并行约 1.5~2.1 倍实时（1 小时素材约 30~40 分钟）。`;
  const est = hours / 1.8;
  return `这个库共 ${hours.toFixed(1)} 小时素材，`
    + `OCR 识别画面硬字幕约 <b>${fmtEst(est)}</b>（按 1.8 倍实时保守算）。`
    + `<div class="dim" style="margin-top:6px">台词来源优先级：`
    + `<b>同名 .srt → 内嵌字幕 → OCR 识别画面</b>。前两种是秒级，OCR 最慢。`
    + `另外每句台词还要抽一帧截图（并发 8 路实测 0.12 秒/帧）；`
    + `关掉「生成动图预览」能再省不少。</div>`
    + `<div class="dim" style="margin-top:4px">OCR 出来的台词会写成视频同名 .srt，`
    + `你可以用记事本/剪映改完错字，在素材菜单点「重新导入字幕」，不用重跑 OCR。</div>`
    + `<div class="dim" style="margin-top:4px">开跑前会花十几秒<b>标定这部片的字幕位置</b>，`
    + `只保留像字幕的那一行——画面里的台标、片内数字、片头演员表会自动滤掉。</div>`
    + `片子多的话，优先给片子配同名 .srt（秒级，且能跳过 OCR）。`;
}

/* 索引：先弹设置 */
function askScan(id) {
  const l = LIBS.find(x => x.id === id);
  if (!l) return;
  const asr = LS.get('use_asr', true), gif = LS.get('with_gif', true),
retry = LS.get('retry_empty', true);
  openModal(`索引「${l.name}」`, `
    <div class="mrow"><label><input type="checkbox" id="mAsr" ${asr ? 'checked' : ''}>
      无字幕时用 OCR 识别画面</label></div>
    <div class="mrow dim">${asrHint(l)}</div>
    <div class="mrow"><label><input type="checkbox" id="mRetry" ${retry ? 'checked' : ''}>
      重新补一遍没有台词的素材</label></div>
    <div class="mrow dim">上次没拿到台词的，这次会再来一次。已经拿到台词的不会重跑。</div>
    <div class="mrow"><label><input type="checkbox" id="mGif" ${gif ? 'checked' : ''}>
      生成动图预览</label></div>
    <div class="mrow dim">列表缩略图悬停时播放的小动图；关掉索引会快不少。</div>
    <div class="mrow dim">目录：${esc(l.path)}</div>
    <div class="mfoot">
      <button id="mCancel">取消</button>
      <button id="mGo" class="primary">开始索引</button>
    </div>`);
  $('#mCancel').onclick = closeModal;
  $('#mGo').onclick = async () => {
    const a = $('#mAsr').checked, g = $('#mGif').checked, r = $('#mRetry').checked;
    LS.set('use_asr', a); LS.set('with_gif', g); LS.set('retry_empty', r);
    await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_asr: a, with_gif: g })
    });
    await api(`/api/libraries/${id}/scan`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_asr: a, with_gif: g, retry_empty: r })
    });
    closeModal();
    switchTab('libs');
    startPoll();
    toast(a ? '开始索引（转写较慢，看下方进度和阶段）' : '开始索引');
  };
}

/* 上传：行内弹窗 */
function askUpload(id) {
  const l = LIBS.find(x => x.id === id);
  if (!l) return;
  openModal(`上传到「${l.name}」`, `
    <div class="drop" id="mDrop">
      <div class="drop-inner">
        <strong>拖拽视频到这里</strong>
        <span>或 <label class="fakebtn">选择文件<input type="file" id="mFile" multiple accept="video/*"></label></span>
        <small>落到 ${esc(l.path)}\\.frameseek\\uploads</small>
      </div>
    </div>
    <div id="mUpList" class="dim"></div>
    <div class="mfoot"><button id="mCancel">关闭</button></div>`);
  $('#mCancel').onclick = closeModal;
  const dz = $('#mDrop');
  ['dragenter', 'dragover'].forEach(t => dz.addEventListener(t, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => dz.addEventListener(t, e => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => doUpload(l.path, e.dataTransfer.files));
  $('#mFile').onchange = e => doUpload(l.path, e.target.files);
}
async function doUpload(root, files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  fd.append('root', root);
  [...files].forEach(f => fd.append('file', f));
  $('#mUpList').textContent = '上传中…';
  const r = await api('/api/upload', { method: 'POST', body: fd });
  if (!r.ok) { $('#mUpList').textContent = '失败：' + (r.err || ''); return; }
  $('#mUpList').textContent = '已上传 ' + (r.saved || []).join('、') + '，正在入库…';
  startPoll();
  setTimeout(() => { closeModal(); switchTab('libs'); }, 900);
}

/* ---------- AI 生成（对话窗口） ---------- */
let GEN = { ready: false, missing: [], aspects: [], outDir: '', ref: '' };
let GEN_MSGS = [];   // 会话气泡：{role:'me'|'sys', ...}

async function loadGenInfo() {
  let r = {};
  try { r = await api('/api/gen/info'); } catch (e) { r = {}; }
  GEN.ready = !!r.ready;
  GEN.missing = r.missing || [];
  GEN.aspects = r.aspects || [];
  GEN.outDir = r.out_dir || '';
  const god = $('#genOutDir');
  if (god) god.textContent = GEN.outDir || '项目目录/生成';
  const srv = $('#genSrv');
  if (srv) {
    srv.className = 'gen-srv' + (GEN.ready ? '' : ' off');
    srv.innerHTML = GEN.ready
      ? 'H3 引擎就绪 <b>直调 ComfyUI 库</b>'
      : '引擎未就绪：' + esc((GEN.missing || []).join('、'));
  }
  const sel = $('#genAspect');
  if (sel && GEN.aspects.length && !sel.options.length) {
    sel.innerHTML = GEN.aspects.map(a =>
      `<option value="${esc(a)}"${a.startsWith('16:9') ? ' selected' : ''}>${esc(a)}</option>`).join('');
  }
  renderChat();
}

function renderChat() {
  const box = $('#chatLog');
  if (!box) return;
  const empty = $('#chatEmpty');
  if (empty) empty.classList.toggle('hidden', GEN_MSGS.length > 0);
  // 坑（真实踩过）：以前这里无条件 box.innerHTML = ...join('')，
  // 空数组会把静态的 #chatEmpty 节点一起清掉——空状态永远消失，
  // 且上面取到的 empty 引用打在已脱离文档的节点上，toggle 无效。
  // 现在：无消息时只移除气泡、保留空状态节点；有消息才整体重建。
  if (!GEN_MSGS.length) {
    box.querySelectorAll('.bub').forEach(n => n.remove());
    return;
  }
  box.innerHTML = GEN_MSGS.map((m, i) => {
    if (m.role === 'me') {
      const meta = [m.dur + ' 秒', m.aspect.split(' ')[0], m.mp + 'MP',
                    m.ref ? '带参考图' : ''].filter(Boolean);
      return `<div class="bub me">
        <div class="bub-txt">${esc(m.prompt)}</div>
        <div class="bub-meta">${meta.map(esc).join(' · ')}</div>
      </div>`;
    }
    let inner = '';
    if (m.status === 'error') {
      inner = `<div class="bub-err">${esc(m.msg || '生成失败')}</div>`;
    } else if (m.status === 'done') {
      inner = `
        <video class="bub-video" src="${mediaUrl(m.file, m.root)}" controls preload="metadata" playsinline></video>
        <div class="bub-meta">${esc(m.name || '')}${m.w ? ' · ' + m.w + '×' + m.h : ''}`
          + `${m.seconds ? ' · ' + m.seconds + 's' : ''}`
          + `${m.lines ? ' · ' + m.lines + ' 条台词' : ''}</div>
        <div class="bub-acts">
          <a href="javascript:;" onclick="switchTab('assets')">去素材库看</a>
          <a href="javascript:;" onclick="GEN_MSGS.length=0;renderChat()">清空会话</a>
        </div>`;
    } else {
      inner = `<div class="bub-run"><i style="width:${m.pct || 0}%"></i></div>
        <div class="bub-meta">${esc(m.msg || '排队中')}${m.pct ? ' · ' + m.pct + '%' : ''}</div>`;
    }
    return `<div class="bub sys">${inner}</div>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

/* 实时事件 → 更新正在跑的那个气泡 */
function genBubbleTick(root, pct, msg) {
  for (let i = GEN_MSGS.length - 1; i >= 0; i--) {
    const m = GEN_MSGS[i];
    if (m.role === 'sys' && m.status === 'run' && (!root || m.root === root)) {
      m.pct = Math.max(m.pct || 0, pct || 0);
      if (msg) m.msg = msg;
      renderChat();
      return;
    }
  }
}

function genBubbleDone(root, s) {
  for (let i = GEN_MSGS.length - 1; i >= 0; i--) {
    const m = GEN_MSGS[i];
    if (m.role === 'sys' && m.status === 'run' && (!root || m.root === root)) {
      if (s.error) {
        m.status = 'error';
        m.msg = s.error;
      } else {
        m.status = 'done';
        m.file = s.file; m.name = s.name || '';
        m.seconds = s.seconds; m.lines = s.lines;
        m.w = s.w; m.h = s.h;
      }
      renderChat();
      return;
    }
  }
}

async function submitGen() {
  const prompt = $('#genPrompt').value.trim();
  if (!prompt) { toast('先写提示词'); $('#genPrompt').focus(); return; }
  if (!GEN.ready) { toast('生成引擎未就绪：' + (GEN.missing || []).join('、')); return; }
  const dur = $('#genDur').value, aspect = $('#genAspect').value, mp = $('#genMp').value;
  const body = {
    prompt,
    duration: Number(dur),
    aspect,
    megapixels: Number(mp),
    seed: Number($('#genSeed').value || 0),
    name: $('#genName').value.trim(),
    ref_image: GEN.ref,
    index: 'none',
    root: ACTIVE_ROOT
  };
  GEN_MSGS.push({ role: 'me', prompt, dur, aspect, mp, ref: !!GEN.ref });
  const bub = { role: 'sys', status: 'run', pct: 0, msg: '已提交', root: body.root };
  GEN_MSGS.push(bub);
  renderChat();
  $('#genPrompt').value = '';
  $('#btnGen').disabled = true;
  try {
    const r = await api('/api/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!r.ok) {
      bub.status = 'error'; bub.msg = r.err || '生成没起来';
      renderChat();
      return;
    }
    bub.msg = '生成中…';
    renderChat();
    startPoll();
  } catch (e) {
    bub.status = 'error'; bub.msg = String(e);
    renderChat();
  } finally {
    $('#btnGen').disabled = false;
    $('#genPrompt').focus();
  }
}

function wireGen() {
  $('#btnGen').onclick = submitGen;
  // Enter 发送 / Shift+Enter 换行——聊天窗的基本礼仪
  $('#genPrompt').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitGen(); }
  });
  $('#btnGenRef').onclick = () => $('#genRefFile').click();
  $('#genRefFile').onchange = async e => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('root', ACTIVE_ROOT);
    fd.append('file', f);
    const r = await api('/api/gen/upload', { method: 'POST', body: fd });
    if (!r.ok) { toast('参考图上传失败：' + (r.err || '')); return; }
    GEN.ref = r.path;
    $('#genRefName').innerHTML = `已选 ${esc(r.name)} · prompt 里用 &lt;Picture 1&gt; 引用（点此移除）`;
    $('#genRefName').title = '点此移除参考图';
    $('#genRefName').classList.add('clr');
    $('#genRefFile').value = '';
  };
  // 点了参考图名字就清掉，避免上一张的图悄悄作用于下一次生成
  $('#genRefName').onclick = () => {
    if (!GEN.ref) return;
    GEN.ref = '';
    $('#genRefName').textContent = '';
    $('#genRefName').title = '';
    $('#genRefName').classList.remove('clr');
  };
  $('#genMore').onclick = () => $('#genAdv').classList.toggle('hidden');
  $$('.gen-ex').forEach(b => b.onclick = () => {
    $('#genPrompt').value = b.dataset.p;
    $('#genPrompt').focus();
  });
}

/* ---------- 实时进度：WebSocket（退路 SSE，再退路轮询） ---------- */
let LIVE = { on: false, mode: 'none', ws: null, es: null, poll: null,
             tasks: {}, libNames: {} };
// 进度浮窗的展开/折叠。窄屏（≤820px）默认折叠，用户点开后置 true，
// 拖宽窗口会自动展开（renderTasks 里 open = DOCK_OPEN || !narrow）。
let DOCK_OPEN = false;

function startPoll() {
  connectLive();
}

/* 把 progress 事件渲染成卡片。
   done/total 是整库（第几个素材）；frames/frames_total 是 OCR / 截图分片的帧级完成度。
   卡片挂在 body 底下的固定浮窗里，任何页签都看得见。 */
function fmtSec(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  const m = Math.floor(s / 60);
  return m ? `${m}:${String(s % 60).padStart(2, '0')}` : `${s}s`;
}

function renderTasks() {
  const box = $('#tasksLibs');
  if (!box) return;
  const list = Object.values(LIVE.tasks);
  box.classList.toggle('hidden', !list.length);
  // 有浮窗时给对话输入区让位（body.tasks-on + 实测高度 --dock-h），
  // 否则 fixed 的 dock 会盖住 composer 右侧的发送按钮。
  document.body.classList.toggle('tasks-on', list.length > 0);
  // 窄屏默认折叠：全屏浮窗在 375px 下会盖住第二、三张素材卡的
  // vmore/台词数（UI 习惯审核实测 P0：点不到）。折叠成一条摘要，
  // 想细看点一下展开。桌面保持展开。
  const narrow = window.innerWidth <= 820;
  const open = DOCK_OPEN || !narrow;
  box.innerHTML = list.map(t => {
    const pct = t.total ? Math.min(100, Math.round(t.done / t.total * 100)) : 0;
    // OCR / 截图分片这两个阶段有帧级完成度
    const hasFrame = (t.phase === 'ocr' || t.phase === 'shots')
                     && (t.frames_total || 0) > 0;
    const fpct = t.frames_pct || 0;
    const PH = { ocr: 'OCR 识别', shots: '截图分片', calib: '字幕标定',
                 cover: '封面', gif: '动图', lines: '台词', clear: '清理旧台词' };
    const phName = PH[t.phase] || '';
    const eta = t.eta_min > 0
      ? ` · 还剩约 ${t.eta_min < 1 ? Math.ceil(t.eta_min * 60) + ' 秒'
                                   : Math.round(t.eta_min) + ' 分钟'}` : '';
    // 整库扫描显示"第几个素材"；单素材重识别显示素材名；
    // 生成任务的 done/total 就是百分比，头部直接显示阶段文字
    const head = t.kind === 'video'
      ? esc(t.file || t.message || '')
      : t.kind === 'gen'
        ? esc(t.message || '')
        : `${t.done}/${t.total} 个素材`;
    // 折叠态只留一行摘要：库名 / 阶段 / 百分比
    if (!open) {
      return `<div class="pcard dock-mini" data-root="${esc(t.root || '')}"
        onclick="DOCK_OPEN=true;renderTasks()">
        <div class="pname">${esc(t.lib || '')}
          <span class="dim">${head}</span>
          ${phName ? `<span class="tag">${esc(phName)}</span>` : ''}
        </div>
        <div class="ppct">${pct}%</div>
      </div>`;
    }
    return `<div class="pcard" data-root="${esc(t.root || '')}">
      <div class="pname">${esc(t.lib || '')}
        <span class="dim">${head}</span>
        ${phName ? `<span class="tag">${esc(phName)}</span>` : ''}
        ${narrow ? `<a href="javascript:;" class="dock-fold"
           onclick="event.stopPropagation();DOCK_OPEN=false;renderTasks()">收起</a>` : ''}
      </div>
      <div class="track"><i style="width:${pct}%"></i></div>
      <div class="ppct">${pct}%</div>
      <div class="pmsg">${esc(t.message || '')}${esc(eta)}</div>
      ${hasFrame ? `<div class="fbar">
        <div class="ftrack"><i style="width:${fpct}%"></i></div>
        <span class="flab">${esc(phName)} ${fpct}% · ${t.frames}/${t.frames_total}`
          + `${t.lines ? ' · 已出 ' + t.lines + ' 条' : ''}`
          + `${t.span ? ' · 已扫 ' + fmtSec(t.pos) + '/' + fmtSec(t.span) : ''}`
          + `${t.batches ? ' · ' + t.batches + ' 批' : ''}</span>
      </div>` : ''}
    </div>`;
  }).join('');
  // 实测浮窗高度 → CSS 变量，composer 据此上移让位；无任务时清掉
  if (list.length) {
    document.body.style.setProperty('--dock-h', (box.offsetHeight || 150) + 'px');
  } else {
    document.body.style.removeProperty('--dock-h');
  }
}

// 拖宽/拖窄窗口时重算浮窗折叠态与让位高度（renderTasks 里 narrow 判断依赖它）
let _rszT = null;
window.addEventListener('resize', () => {
  clearTimeout(_rszT);
  _rszT = setTimeout(renderTasks, 150);
});

function onLive(ev) {
  if (!ev || ev.type === 'ping' || ev.type === 'hello') return;
  if (ev.type === 'video') {
    // 索引过程中素材实时入库：直接插到列表，不用等整库跑完再刷新
    const v = ev.video || {};
    if (!v.id) return;
    if (!LIVE.known) LIVE.known = new Set();
    const key = v.root + '#' + v.id;
    if (LIVE.known.has(key)) return;
    LIVE.known.add(key);
    if (ALL.some(x => x.root === v.root && x.id === v.id)) return;
    ALL.push(v);
    if (currentTab() === 'assets') loadAssets();
    return;
  }
  if (ev.type === 'progress') {
    const k = ev.root || '';
    if (!k) return;
    const prev = LIVE.tasks[k] || {};
    const merged = Object.assign({}, prev, ev);
    // 阶段事件只带 frames_*，phase 可能为空；沿用上一阶段的 phase，
    // 否则帧进度条会在两帧之间来回闪。
    if (!ev.phase && !ev.frames_total && prev.phase) {
      merged.phase = '';
    }
    // 坑：以前写成 LIVE.tasks[k].lib，而首次事件时 LIVE.tasks[k] 还是 undefined，
    // 直接 TypeError——被 ws.onmessage 的 try/catch 吞掉，结果是**进度卡片
    // 永远渲染不出来**。改成就近取，不要把未初始化的对象当字典用。
    merged.lib = ev.lib || prev.lib || '';
    LIVE.tasks[k] = merged;
    renderTasks();
    // 生成任务的进度同时驱动对话窗口的气泡
    if (ev.kind === 'gen') genBubbleTick(k, ev.done || 0, ev.message || '');
    // 手绘渲染/混流：把进度写进气泡（渲染是本地 CPU，耗时看进度条更直观）
    if (ev.kind === 'handdraw') hdTickRun(k, ev.done || 0, ev.message || '');
    return;
  }
  if (ev.type === 'task_done') {
    const k = ev.root || '';
    // 先删卡片，再靠 loadLibs 刷新库统计——留着会一直显示 100% 不消失
    delete LIVE.tasks[k];
    renderTasks();
    const s = ev.summary || {};
    if (s.handdraw) {
      hdRunDone(s);
      if (s.error) {
        toast(`手绘任务失败：${s.error}`);
      } else {
        toast(`手绘完成：${s.name || ''}`);
        if (s.library_file) loadLibs();
      }
      loadLibs();
      clearTimeout(LIVE._t);
      LIVE._t = setTimeout(() => { renderTasks(); }, 2500);
      return;
    }
    if (s.gen) {
      // AI 生成任务的收尾：对话气泡换成成片卡片，再补一个 toast
      genBubbleDone(k, s);
      if (s.error) {
        toast(`视频生成失败：${s.error}`);
      } else {
        toast(`生成完成：${s.name || ''}（${s.seconds || '?'}s`
          + `${s.lines ? '，' + s.lines + ' 条台词' : ''}）`
          + `，已落入素材库「AI生成」`);
      }
    } else if (s.single) {
      // 单素材「重新识别台词」的收尾：清了多少旧台词 / 出了多少新台词
      if (s.error) {
        toast(`「${s.name}」识别失败：${s.error}`);
      } else {
        const SRC = { external: '外挂 SRT', embedded: '内嵌字幕',
                     ocr: 'OCR 识别', asr: '语音识别' };
        toast(`「${s.name}」识别完成：清掉旧台词 ${s.cleared || 0} 条，`
              + `新出 ${s.lines} 条（${SRC[s.source] || s.source || '未知'}）`);
      }
    } else {
      toast(`「${ev.lib}」索引完成：${s.total} 个素材，跳过 ${s.skipped} 个重复`
        + (s.failed && s.failed.length ? `，失败 ${s.failed.length}` : ''));
    }
    loadLibs();
    if (currentTab() === 'assets') loadAssets();
    clearTimeout(LIVE._t);
    LIVE._t = setTimeout(() => { renderTasks(); }, 2500);
    return;
  }
}

function connectLive() {
  if (LIVE.on) return;
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  let ws;
  try { ws = new WebSocket(url); }
  catch (e) { return connectSSE(); }

  const fail = () => { LIVE.ws = null; if (!LIVE.on) connectSSE(); };
  ws.onopen = () => { LIVE.on = true; LIVE.mode = 'ws'; stopSSE(); };
  ws.onmessage = e => {
    try { onLive(JSON.parse(e.data)); }
    catch (err) { /* 单条消息坏了不能拖垮整条流 */ }
  };
  ws.onerror = fail;
  ws.onclose = () => {
    if (LIVE.mode === 'ws') { LIVE.on = false; fail(); setTimeout(connectLive, 3000); }
  };
  LIVE.ws = ws;
}

function connectSSE() {
  if (LIVE.es || LIVE.mode === 'ws') return;
  const es = new EventSource('/api/events');
  es.onmessage = e => {
    try { onLive(JSON.parse(e.data)); } catch (err) {}
  };
  es.onerror = () => { es.close(); LIVE.es = null; startFallbackPoll(); };
  LIVE.es = es; LIVE.mode = 'sse';
}
function stopSSE() { if (LIVE.es) { LIVE.es.close(); LIVE.es = null; } }

/* 最后退路：什么都连不上就回到每秒轮询 */
function startFallbackPoll() {
  if (LIVE.poll) return;
  LIVE.mode = 'poll';
  LIVE.poll = setInterval(async () => {
    let ts = [];
    try { ts = await api('/api/tasks'); } catch (e) { return; }
    const next = {};
    (ts || []).forEach(t => {
      next[t.root || t.lib] = Object.assign({}, t, { lib: t.lib });
      // 轮询兜底也要驱动对话气泡，否则 WS/SSE 全挂时聊天窗没进度
      if (t.kind === 'gen') genBubbleTick(t.root, t.done || 0, t.message || '');
    });
    LIVE.tasks = next;
    renderTasks();
    if (!ts || !ts.length) {
      clearInterval(LIVE.poll); LIVE.poll = null;
      loadLibs();
    }
  }, 1000);
}

function currentTab() {
  const on = document.querySelector('.nav-item.on');
  return on ? on.dataset.tab : '';
}

/* ---------- 素材（跨库） ---------- */
/* ---------- 素材页筛选（2026-10-15） ----------
   库多选 + 台词状态 + 分辨率 + 时长 + 编码/帧率 + 入库时间 + 动图封面 */
const FILTER = {
  libs: new Set(),        // 空 = 全部库（含内置 AI 库）
  lines: new Set(),       // 空 = 不限台词状态
  res: new Set(),
  dur: new Set(),
  codec: '', fps: '', age: '',
  hasGif: false, hasCover: false, noGif: false,
};

function chipVals(id) {
  return [...document.querySelectorAll(`#${id} input:checked`)].map(i => i.value);
}
/* 静默批量改勾选状态：不触发 onchange。
   直接设 checked 会走 change 事件把 FILTER 重新填回去（重置时必踩）。 */
function setChipGroup(id, vals) {
  const box = document.getElementById(id);
  if (!box) return;
  box.querySelectorAll('input').forEach(i => { i.checked = vals.includes(i.value); });
}

/* 素材库多选：用户登记的库 + 内置 AI 库（只读，不能编辑） */
function renderLibChips() {
  const box = $('#fLibs');
  if (!box) return;
  const all = (LIBS || []).concat(BUILTIN_LIBS || []);
  const on = FILTER.libs;
  box.innerHTML = [
    `<label class="fchip"><input type="checkbox" value="__all"${on.size === 0 ? ' checked' : ''}><span>全部</span></label>`,
    all.map(l => `<label class="fchip" title="${esc(l.path)}">
        <input type="checkbox" value="${l.id}"${on.has(l.id) ? ' checked' : ''}>
        <span>${esc(l.name || l.path)}${l.builtin ? ' 🔒' : ''} <b class="dim">${l.videos}</b></span>
      </label>`).join(''),
  ].join('');
  box.querySelectorAll('input').forEach(i => {
    i.onchange = () => {
      if (i.value === '__all') {
        FILTER.libs.clear();
      } else {
        const id = +i.value;
        if (i.checked) FILTER.libs.add(id); else FILTER.libs.delete(id);
      }
      // 「全部」的勾选态跟随其它项
      const allBox = box.querySelector('input[value="__all"]');
      if (allBox) allBox.checked = FILTER.libs.size === 0;
      loadAssets();
    };
  });
}

/* 编码下拉：按库里实际出现过的 vcodec 生成选项 */
function renderCodecOptions(rows) {
  const sel = $('#fCodec');
  if (!sel) return;
  const seen = [...new Set(rows.map(r => (r.vcodec || '').trim()).filter(Boolean))].sort();
  const keep = FILTER.codec;
  sel.innerHTML = '<option value="">全部</option>' +
    seen.map(c => `<option value="${esc(c)}"${c === keep ? ' selected' : ''}>${esc(c)}</option>`).join('');
  if (!seen.includes(keep)) FILTER.codec = '';
}

function resBucket(v) {
  const h = +(v.height || 0), w = +(v.width || 0);
  if (!h) return '';
  if (h >= 2000) return '2160';
  if (h >= 1000) return '1080';
  if (h >= 700) return '720';
  return 'sd';                    // 标清或竖屏（竖屏 1080x1920 归 sd）
}

function fpsBucket(v) {
  const f = +(v.fps || 0);
  if (!f) return '';
  if (Math.abs(f - 23.976) < 1.2 || Math.abs(f - 24) < 1.2) return '23.976';
  if (Math.abs(f - 25) < 1.2) return '25';
  if (Math.abs(f - 29.97) < 1.2 || Math.abs(f - 30) < 1.2) return '29.97';
  if (f >= 45) return '50';
  return '';
}

function matchFilters(r, f) {
  // 库
  if (FILTER.libs.size && !FILTER.libs.has(r.lib_id)) return false;
  // 台词状态（AI 生成产物没有台词这回事，不参与这项筛选）
  if (FILTER.lines.size && !r.builtin) {
    const lc = r.line_count || 0;
    const hit = (FILTER.lines.has('has') && lc > 0)
             || (FILTER.lines.has('none') && lc === 0 && !r.err)
             || (FILTER.lines.has('err') && !!r.err);
    if (!hit) return false;
  }
  // 分辨率
  if (FILTER.res.size) {
    const b = resBucket(r);
    if (!b || !FILTER.res.has(b)) return false;
  }
  // 时长（秒）
  if (FILTER.dur.size) {
    const d = +(r.duration || 0);
    const hit = (FILTER.dur.has('s30') && d > 0 && d < 30)
             || (FILTER.dur.has('m30') && d >= 30 && d < 300)
             || (FILTER.dur.has('m5_20') && d >= 300 && d < 1200)
             || (FILTER.dur.has('l20') && d >= 1200);
    if (!hit) return false;
  }
  // 编码 / 帧率
  if (FILTER.codec && (r.vcodec || '') !== FILTER.codec) return false;
  if (FILTER.fps && fpsBucket(r) !== FILTER.fps) return false;
  // 入库时间
  if (FILTER.age) {
    const since = Date.now() / 1000 - (+FILTER.age) * 86400;
    if (!(+(r.created_at || 0) >= since)) return false;
  }
  // 动图 / 封面
  if (FILTER.hasGif && !r.gif) return false;
  if (FILTER.hasCover && !r.cover) return false;
  if (FILTER.noGif && r.gif) return false;
  // 关键字
  if (f && !(r.name || '').toLowerCase().includes(f)
         && !(r.lib || '').toLowerCase().includes(f)) return false;
  return true;
}

function filterSummary(total, shown) {
  const bits = [];
  const libs = [...FILTER.libs];
  if (libs.length) {
    const all = (LIBS || []).concat(BUILTIN_LIBS || []);
    bits.push('库：' + libs.map(id => {
      const l = all.find(x => +x.id === +id);
      return l ? l.name : id;
    }).join('、'));
  }
  if (FILTER.lines.size && FILTER.lines.size < 3)
    bits.push('台词：' + chipVals('fLines').join('/'));
  if (FILTER.res.size) bits.push('分辨率：' + chipVals('fRes').join('/'));
  if (FILTER.dur.size) bits.push('时长：' + chipVals('fDur').join('/'));
  if (FILTER.codec) bits.push('编码：' + FILTER.codec);
  if (FILTER.fps) bits.push('帧率：' + $('#fFps').selectedOptions[0].textContent);
  if (FILTER.age) bits.push('入库：' + $('#fAge').selectedOptions[0].textContent);
  if (FILTER.hasGif) bits.push('仅有动图');
  if (FILTER.hasCover) bits.push('仅有封面');
  if (FILTER.noGif) bits.push('仅无动图');
  $('#fSummary').innerHTML = `命中 <b>${shown}</b> / ${total} 部` +
    (bits.length ? ' · ' + esc(bits.join(' · ')) : ' · 未设条件');
}

async function loadAssets() {
  // 库筛选下推到服务端；AI 内置库 id=0 也参与
  const qs = FILTER.libs.size
    ? `&libs=${[...FILTER.libs].join(',')}` : '';
  ALL = await api(`/api/videos?scope=all${qs}`);
  const f = $('#vfilter').value.trim().toLowerCase();
  renderCodecOptions(ALL);
  const list = ALL.filter(r => matchFilters(r, f));
  $('#vcount').textContent = `(${list.length})`;
  filterSummary(ALL.length, list.length);
  $('#badgeVideos').textContent = list.length;
  VIEW = list;
  $('#videos').innerHTML = list.map((v, i) => {
    // AI 生成产物：单独一档，不做字幕/语音识别，所以台词相关的入口全不出现
    const ai = !!v.builtin;
    return `
    <div class="vcard" data-i="${i}" title="${esc(v.name)}">
      <img src="${v.cover ? mediaUrl(v.cover, v.root) : ''}" loading="lazy"
           onmouseenter="if('${v.gif || ''}'&&this.dataset.gif!=='1'){this.dataset.src=this.src;this.dataset.gif='1';this.src='${v.gif ? mediaUrl(v.gif, v.root) : ''}'}"
           onmouseleave="if(this.dataset.gif==='1'){this.src=this.dataset.src;this.dataset.gif='0'}">
      <button class="vmore" title="${ai
        ? '更多操作：打开文件夹 / 从库里移除 / 删除文件（AI 生成产物不做字幕识别）'
        : '更多操作：重新识别台词 / 导入字幕 / 打开文件夹 / 移除或删除'}"
        onclick="toggleMenu(event,${i})">···</button>
      <div class="vmeta">
        <b>${esc(v.name)}</b>
        <span>${v.duration_str || ''} · ${ai ? '🔒 ' : ''}${esc(v.lib || '')}</span>
        <div class="row2">
          ${ai ? '<span class="tag" title="AI 生成产物不参与台词识别">AI 生成</span>'
               : (v.err ? `<span class="tag bad" title="${esc(v.err)}">识别失败</span>`
                        : (v.line_count ? `<span class="tag ok">台词 ${v.line_count}</span>`
                                        : '<span class="tag no">没台词</span>'))}
        </div>
      </div>
      <div class="vmenu hidden" id="menu${i}">
        <a href="javascript:;" onclick="previewVideo(${i})"
          title="在右侧播放器里预览这部片子">预览</a>
        <a href="javascript:;" onclick="openVideoDir(${i})"
          title="在资源管理器里打开这个视频所在的文件夹">打开所在文件夹</a>
        ${ai ? '' : `
        <a href="javascript:;" onclick="askReimportSrt(${i})"
          title="用记事本改完视频同名的 .srt 后点这个：直接刷新数据库和截图，不用重跑 OCR">重新导入同名 SRT</a>
        <a href="javascript:;" onclick="askRetranscribe(${i})"
          title="重跑一次画面 OCR，重新识别硬字幕台词。已有的同名 .srt 会被覆盖">重新识别台词</a>
        <a href="javascript:;" onclick="askTranscribeAsr(${i})"
          title="改用语音识别（听声音）出台词，适合没有画面字幕、只有人声的片子，耗时比 OCR 长">语音识别台词</a>`}
        <a href="javascript:;" onclick="askDelVideo(${i},false)"
          title="只把这个视频从索引里移除，磁盘文件保留">从库中移除</a>
        <a href="javascript:;" class="danger" onclick="askDelVideo(${i},true)"
          title="连磁盘文件一起删除，不可恢复（要连点两次确认）">删除素材及文件</a>
      </div>
    </div>`}).join('') || '<div class="dim">还没有素材，先去「素材库」登记并索引</div>';
  $$('.vcard').forEach(el => el.onclick = e => {
    if (e.target.closest('.vmore') || e.target.closest('.vmenu')) return;
    previewVideo(+el.dataset.i);
  });
}

/* 三点菜单 */
function toggleMenu(ev, id) {
  ev.stopPropagation();
  const m = $('#menu' + id);
  const wasOpen = !m.classList.contains('hidden');
  closeMenus();
  if (!wasOpen) {
    m.classList.remove('hidden');
    // 菜单是卡片内的绝对定位元素，不抬这张卡片的层级，
    // 后面卡片的背景就会盖住菜单底部（"删除素材及文件"曾因此点不到）。
    const card = m.closest('.vcard');
    if (card) card.classList.add('menu-open');
  }
}

function closeMenus() {
  $$('.vmenu').forEach(x => x.classList.add('hidden'));
  $$('.vcard.menu-open').forEach(x => x.classList.remove('menu-open'));
}
document.addEventListener('click', e => {
  // 菜单浮层会盖住下一行卡片的「···」，点那块地方落在菜单空白处。
  // 收起菜单之后，把这一下转给底下真正的目标——一次点击就切到目标卡片的菜单，
  // 而不是"点一下只收起、再点一下才开"。
  const onBlankMenu = e.target.classList && e.target.classList.contains('vmenu');
  if (onBlankMenu) {
    closeMenus();
    const under = document.elementFromPoint(e.clientX, e.clientY);
    const hit = under && under.closest ? under.closest('.vmore') : null;
    if (hit && hit !== e.target) hit.click();
    return;
  }
  if (!e.target.closest('.vmenu') && !e.target.closest('.vmore'))
    closeMenus();
});

function previewVideo(i) {
  const v = VIEW[i];
  if (!v) return;
  closeMenus();
  play(v.id, 0, 0, encodeURIComponent(v.root));
}

function openVideoDir(i) {
  const v = VIEW[i];
  if (!v) return;
  closeMenus();
  const rel = (v.rel || '').replace(/[\\/]+[^\\/]*$/, '');
  const dir = rel ? v.root + '\\' + rel : v.root;
  openPath(encodeURIComponent(dir));
}

/* 单个素材重新识别台词 */
function askRetranscribe(i) {
  const v = VIEW[i];
  if (!v) return;
  closeMenus();
  const prev = v.line_source || '';
  const PREV = { external: '外挂 SRT', embedded: '内嵌字幕', ocr: 'OCR 识别',
                 asr: '语音转写' };
  // 上次是 OCR 出来的话，视频旁边的同名 .srt 是我们自己的产物，
  // 不跳过它等于把旧结果读回来——所以这种情况默认勾上强制重跑。
  const needForce = prev === 'ocr';
  askConfirm('重新识别台词', `
    <div class="mrow"><b>${esc(v.name)}</b> <span class="dim">${v.duration_str || ''}</span></div>
    <div class="mrow">当前台词 ${v.line_count || 0} 条（${
      esc(PREV[prev] || '无')}）。重新识别会<b>先清空这些台词和旧截图</b>，
      再按最新逻辑重新识别。</div>
    <div class="mrow"><label><input type="checkbox" id="mForce" ${
      needForce ? 'checked' : ''}> 强制重跑 OCR（忽略已有字幕）</label></div>
    <div class="mrow dim">默认按优先级：同名 .srt → 内嵌字幕 → OCR 识别画面。
      上次是 OCR 时勾选它，才不会把上次生成的 .srt 又读一遍。
      进度看右下角浮窗。</div>`,
    async (form) => {
      const r = await api('/api/video/retranscribe', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root: v.root, video_id: v.id,
                               force: !!(form && form.mForce) })
      });
      if (!r.ok) { toast(r.err || '启动失败'); return; }
      startPoll();
      toast('已清空旧台词，开始重新识别「' + v.name + '」');
    }, '清空并重新识别');
}

/* 语音识别台词：没有硬字幕的电影走这条（直接听音轨，不碰 OCR/SRT） */
function askTranscribeAsr(i) {
  const v = VIEW[i];
  if (!v) return;
  closeMenus();
  api('/api/asr/info').then(info => {
    const gpu = info && info.cuda;
    const dev = gpu ? 'GPU（4060Ti）' : 'CPU';
    const mdl = (info && info.default_model) || 'small';
    const MODELS = ['tiny', 'base', 'small', 'medium'];
    // 时长 → 粗估耗时：GPU small 约 8~12 倍实时，CPU small 约 1.5~2 倍。
    const dur = v.duration || 0;
    const eta = gpu ? dur / 10 : dur / 1.8;
    askConfirm('语音识别台词', `
    <div class="mrow"><b>${esc(v.name)}</b> <span class="dim">${v.duration_str || ''}</span></div>
    <div class="mrow">听音轨转写对白，<b>不读</b>同名 .srt、不读内嵌字幕、也不跑 OCR。
      适合画面上没有字幕（或字幕认不出来）的电影。</div>
    <div class="mrow">当前台词 ${v.line_count || 0} 条（${
      esc(({external:'外挂 SRT',embedded:'内嵌字幕',ocr:'OCR 识别',asr:'语音转写',
            pending:'识别中'})[v.line_source] || '无')}）。
      语音识别会<b>先清空这些台词和旧截图</b>再转写。</div>
    <div class="mrow"><label>模型
      <select id="mModel">${MODELS.map(m =>
        `<option value="${m}" ${m === mdl ? 'selected' : ''}>${m}${
          m === mdl ? '（默认）' : ''}</option>`).join('')}
      </select></label>
      <span class="dim">small 准确、tiny 快 3 倍但错字多；首次用某个模型要先下载（几百 MB）</span></div>
    <div class="mrow dim">识别设备：${dev} · 预计 ${eta >= 60
      ? Math.round(eta / 60) + ' 分钟' : Math.round(eta) + ' 秒'}
      （粗估，进度看右下角浮窗）。转写完会在视频同目录生成同名 .srt，可改完再「重新导入」。</div>`,
    async (form) => {
      const model = (form && form.mModel) || '';
      const r = await api('/api/video/transcribe_asr', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root: v.root, video_id: v.id, model: model })
      });
      if (!r.ok) { toast(r.err || '启动失败'); return; }
      startPoll();
      toast('开始语音识别「' + v.name + '」');
    }, '开始语音识别');
  }).catch(() => toast('读不到语音识别设备信息，服务端异常'));
}

/* 重新导入同名 .srt：你在外面改完字幕，点这个刷新数据库 + 重抽截图 */
function askReimportSrt(i) {
  const v = VIEW[i];
  if (!v) return;
  closeMenus();
  askConfirm('重新导入字幕', `
    <div class="mrow"><b>${esc(v.name)}</b> <span class="dim">${v.duration_str || ''}</span></div>
    <div class="mrow">读取视频同目录的同名 <code>.srt</code>（必须是
      <code>${esc(v.name.replace(/\.[^.]+$/, ''))}.srt</code>），覆盖数据库里的台词并重抽截图。</div>
    <div class="mrow dim">适合你用记事本/剪映改完错字、调完时间点之后用——不用重新转写。</div>`,
    async () => {
      const r = await api('/api/video/reimport', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root: v.root, video_id: v.id })
      });
      if (!r.ok) { toast(r.err || '导入失败'); return; }
      toast(`已导入 ${r.lines} 条字幕`);
      await loadAssets();
    }, '导入');
}

/* 删除素材：弹窗确认；删文件时要点两次才算数 */
function askDelVideo(i, withFile) {
  closeMenus();
  const v = VIEW[i];
  if (!v) return;
  const full = [v.root, v.rel].filter(Boolean).join('\\');
  openModal(withFile ? '删除素材及文件' : '从库中移除', `
    <div class="mrow">
      <div><b>${esc(v.name)}</b></div>
      <div class="dim">${esc(full)}</div>
      <div class="dim">${v.duration_str || ''} · 台词 ${v.line_count || 0} · 库「${esc(v.lib || '')}」</div>
    </div>
    ${withFile ? `<div class="warnbox">
      <b>会把磁盘上的文件一起删掉，不进回收站，不可恢复。</b><br>
      同时删除：视频原文件、同名外挂字幕（.srt/.ass/.ssa/.vtt）、封面、动图、该片所有台词截图。
    </div>` : `<div class="mrow dim">只把这条记录从库里去掉，磁盘文件保留。下次索引这个库会重新入库。</div>`}
    <div class="mfoot">
      <button id="mCancel">取消</button>
      <button id="mDel" class="${withFile ? 'danger' : ''}">${withFile ? '删除' : '移除'}</button>
    </div>`);
  $('#mCancel').onclick = closeModal;
  let armed = !withFile;
  $('#mDel').onclick = async () => {
    if (!armed) {                       // 第一次点：变成确认态，不做任何事
      armed = true;
      const b = $('#mDel');
      b.textContent = '再点一次：永久删除';
      b.classList.add('danger');
      b.focus();
      return;
    }
    const btn = $('#mDel');
    btn.disabled = true; btn.textContent = '处理中…';
    const r = await api('/api/video/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: v.root, video_id: v.id, delete_file: withFile })
    });
    closeModal();
    await loadLibs();
    toast(r && r.ok
      ? (withFile ? `已删除「${v.name}」及磁盘文件（${(r.deleted || []).length} 个）`
                  : `已从库里移除「${v.name}」，文件还在，以后索引不会再收进来`)
      : '失败：' + ((r && r.err) || '看服务端日志'));
  };
}

/* ---------- 检索（默认跨全部库） ---------- */
async function search() {
  const q = $('#q').value.trim();
  if (!q) return;
  const rows = await api(`/api/search?q=${encodeURIComponent(q)}&scope=all&limit=300`);
  window.__lastRows = rows;
  // 搜过之后空状态引导就退场（有结果或"没有结果"都算搜过了）
  const tip = $('#searchEmpty');
  if (tip) tip.classList.add('hidden');
  $('#results').innerHTML = rows.map(r => `
    <div class="rrow">
      <img src="${r.shot ? mediaUrl(r.shot, r.root) : (r.cover ? mediaUrl(r.cover, r.root) : '')}"
           loading="lazy" onclick="play(${r.video_id},${r.start},${r.id},'${encodeURIComponent(r.root)}')">
      <div class="rbody">
        <div class="ts">${r.start_str} → ${r.end_str}
          ${r.lib ? `<span class="lib">${esc(r.lib)}</span>` : ''}
          ${boxMark(r)}</div>
        <div class="txt">${hl(r.text, q)}</div>
        <div class="from">${esc(r.name || '')}</div>
      </div>
      <div class="ops">
        <button onclick="play(${r.video_id},${r.start},${r.id},'${encodeURIComponent(r.root)}')"
          title="从这句台词的时间点开始播放原片">预览</button>
        <button class="primary" onclick="clip(${r.id},'${encodeURIComponent(r.root)}')"
          title="导出这句台词前 ${+$('#pre').value}s、后 ${+$('#post').value}s 的 mp4，直接拖进剪映或 PR">导出片段</button>
      </div>
    </div>`).join('') || '<div class="dim">没有结果，换个词试试</div>';
}

/* 方框凭证标记：这条台词在画面上有没有一个真实的框撑着。
   没凭证的行（外挂SRT/内嵌字幕本来就没有框）显示为中性灰，不报警。
   字体指纹偏离（font_dev）单独一个徽标——它不判伪（片头大字号、艺术字
   场景都会偏离），只是提示"这句的字和本部片主流不一样，纠错时优先怀疑"，
   与"方框异常"是两个维度的事，徽标也分开。 */
function boxMark(r) {
  const b = r.box;
  if (!b) {
    return (r.src && r.src !== 'ocr')
      ? `<span class="boxinfo" title="${esc(r.src)} 来源字幕，本就没有画面框">· 无框(正常)</span>`
      : `<span class="boxwarn" title="这条台词没有画面上的方框凭证，可能不是画面字幕">· 无框凭证</span>`;
  }
  const ar = b.ar, hits = b.hits || 0, conf = b.conf;
  const span = Math.max(0, (r.end || 0) - (r.start || 0));
  const cov = span > 0.6 ? hits / (span / STEP + 1) : 1;
  const bad = ar < 0.55 || ar > 1.45 || cov < 0.34 || cov > 1.25 || conf < 0.62;
  const fnt = b.font || {};
  const tip = `框 cx=${b.cx} cy=${b.cy} 宽=${b.bw} 字高=${b.bh}\n`
            + `字宽比 ${ar} · ${hits} 帧支持(覆盖 ${Math.round(cov*100)}%) · 置信度 ${conf}`
            + (b.drift != null ? ` · 跨帧漂移 ${b.drift}` : '')
            + (fnt.ink ? `\n字体：墨密度 ${fnt.ink} · 笔画 ${fnt.stroke}px`
                       + ` · 字高占画面 ${(fnt.px_h * 100).toFixed(1)}%` : '');
  const mark = bad
    ? `<span class="boxwarn" title="${esc(tip)}">· 方框异常</span>`
    : `<span class="boxok" title="${esc(tip)}">· 方框已验</span>`;
  // 字体偏离徽标：阈值与 core/config.py 的 OCR_FONT_DEV_MAX 一致（1.5）
  if ((b.font_dev || 0) > 1.5) {
    return mark + `<span class="boxfont" title="${esc(tip + '\n\n字体偏离度 ' + b.font_dev + '：与本部片主流字幕的字体/字号不一致，纠错时优先怀疑这一条；若只是片头大字或艺术字场景，属正常。')}">· 字体异常</span>`;
  }
  return mark;
}

/* ---------- 播放器 ---------- */
function play(video_id, start, line_id, rootEnc) {
  CUR = { video_id, start, root: decodeURIComponent(rootEnc || '') };
  $('#player').classList.remove('hidden');
  const v = $('#video');
  v.src = `/stream/${video_id}?root=${encodeURIComponent(CUR.root)}`;
  v.onloadedmetadata = () => {
    v.currentTime = Math.max(0, start - Math.min(+$('#pre').value || 5, 2)); v.play();
  };
  const t = (window.__lastRows || []).find(x => x.id === line_id);
  $('#ptext').textContent = t ? `${t.start_str}  ${t.text}` : '';
}
$('#btnClosePlayer').onclick = () => { $('#player').classList.add('hidden'); $('#video').pause(); };
$('#player').onclick = e => { if (e.target.id === 'player') $('#btnClosePlayer').click(); };
$('#btnClipHere').onclick = () => {
  const v = $('#video');
  api('/api/clip', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      root: CUR.root, video_id: CUR.video_id,
      start: v.currentTime, end: v.currentTime + 1,
      pre: +$('#pre').value, post: +$('#post').value, fast: $('#fast').checked
    })
  }).then(addClips);
};

/* ---------- 方框复查（台词帧 / 台词框 / 文字）----------
   这个模块回答的是一个 verify.py 回答不了的问题：
   「框到底对不对」最终得用眼睛看。所以这里把三样东西摆在一起——
     台词帧  那一秒的真实画面（按需现抽 960 宽整幅）
     台词框  库里的方框凭证画上去（蓝=库的，绿虚线=现场重检的）
     文字    这条入库文本 vs 这一帧现场重新 OCR 出来的文本
   左边是清单（可按可疑/无框筛），右边是逐句核对视图。
--------------------------------------------------- */
let BX = { list: [], cur: null, rep: null, filter: 'all', q: '',
           busy: false, probeOut: '', key: '',
           raw: [], lib: '', vq: '' };   // raw=整份片单；lib=库筛选；vq=片名搜索

/* 清单请求的代次号。
   坑（真踩过）：进页面会触发一次 loadBoxVideo，跟着 select 的 change 又触发
   一次，两个请求同时在飞。先发的那个后回来，就拿改之前的旧快照把刚保存的
   改动抹掉——表现是"toast 说已保存，面板却又变回旧文字"。
   办法：每次请求领一个号，回来时号不对就把这份结果丢掉；保存改动时把号
   +1，让所有在途的清单请求全部作废。 */
let BX_SEQ = 0;

/* 片单两级筛选：先按素材库，再按片名关键字（多词要全部命中）。
   65 部片挤在一个下拉里找片子太难，所以库下拉 + 搜索框都在本地过滤
   ——接口只拉一次全量概览（一条 SQL 聚合，秒回），边打边筛没有网络往返。 */
function bxVideoList() {
  const terms = BX.vq.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return BX.raw.filter(x => {
    if (BX.lib && x.root !== BX.lib) return false;
    if (!terms.length) return true;
    const nm = String(x.name || '').toLowerCase();
    return terms.every(t => nm.includes(t));
  });
}

function renderBxLibPick() {
  const sel = $('#bxLibPick');
  const order = [];
  const cnt = new Map(), nm = new Map();
  BX.raw.forEach(x => {
    if (!cnt.has(x.root)) { cnt.set(x.root, 0); nm.set(x.root, x.lib); order.push(x.root); }
    cnt.set(x.root, cnt.get(x.root) + 1);
  });
  sel.innerHTML = `<option value="">全部库（${BX.raw.length}）</option>`
    + order.map(r => `<option value="${esc(r)}">`
        + `${esc(nm.get(r) || r)}（${cnt.get(r)}）</option>`).join('');
  if (BX.lib && cnt.has(BX.lib)) sel.value = BX.lib;
  else { BX.lib = ''; sel.value = ''; }
}

function renderBxPicker() {
  const list = bxVideoList();
  const sel = $('#bxPick');
  const keep = sel.value;
  // 全库模式下标出库名，否则同名片分不清是哪一库的
  const showLib = !BX.lib;
  sel.innerHTML = list.length
    ? list.map(x => `<option value="${esc(x.root)}|${x.id}">`
        + `${esc(x.name)}${showLib ? ' · ' + esc(x.lib || '') : ''}`
        + `${x.no_box ? ` · 无框 ${x.no_box}/${x.lines}` : ''}`
        + `</option>`).join('')
    : `<option value="">${BX.raw.length ? '没有匹配的片子' : '还没有已索引的素材'}</option>`;
  $('#bxHit').textContent = BX.raw.length
    ? `命中 ${list.length} / ${BX.raw.length} 部` : '';
  return keep;
}

async function loadBoxLibs(force) {
  let libs = [];
  try { libs = await api('/api/boxaudit/library'); } catch (e) { libs = []; }
  BX.raw = libs;
  renderBxLibPick();
  const sel = $('#bxPick');
  const keep = renderBxPicker();
  const noBox = libs.reduce((a, x) => a + (x.no_box || 0), 0);
  $('#badgeBox').textContent = noBox || 0;
  // innerHTML 重设后浏览器会自动选回第一条，原来那部还在结果里就选回去
  if (keep && [...sel.options].some(o => o.value === keep)) sel.value = keep;
  if (!libs.length || !sel.value) {
    $('#bxSum').innerHTML = '';
    $('#bxRows').innerHTML = '';
    $('#bxView').innerHTML = '<div class="empty dim">'
      + (libs.length ? '换个库或改个关键字试试' : '先去「素材库」索引出台词') + '</div>';
    BX.list = []; BX.cur = null; BX.key = '';
    return;
  }
  await loadBoxVideo(force);
}

/* 库下拉 / 片名搜索：改完重渲染片单，选中那部被筛掉时自动落到第一条 */
function onBxLibChange() {
  BX.lib = $('#bxLibPick').value || '';
  onBxScopeChange();
}

function onBxSearch() {
  BX.vq = $('#bxSearch').value || '';
  onBxScopeChange();
}

function onBxScopeChange() {
  const sel = $('#bxPick');
  const keep = renderBxPicker();
  const still = keep && [...sel.options].some(o => o.value === keep);
  if (still) sel.value = keep;
  if (!sel.value) {                       // 筛没了：清空右侧，别留着上一部的清单
    BX_SEQ++;
    $('#bxSum').innerHTML = '';
    $('#bxRows').innerHTML = '';
    $('#bxView').innerHTML = '<div class="empty dim">没有匹配的片子</div>';
    BX.list = []; BX.cur = null; BX.key = ''; BX.rep = null;
    return;
  }
  if (!still) loadBoxVideo();             // 换到了另一部 → 拉它的清单
}

function onBxPick() {
  const v = $('#bxPick').value;
  if (v) loadBoxVideo();
}

/* force=true 表示"别管缓存，重新拉"——点刷新和用户刚改完数据时用。
   同一部片子已经加载过就不重复拉：一部片两千条要逐条 verify，
   白跑一次好几秒，还容易两份结果互相覆盖。 */
async function loadBoxVideo(force) {
  const v = $('#bxPick').value;
  if (!v) return;
  if (!force && v === BX.key && BX.rep) return;
  const [root, id] = v.split('|');
  const my = ++BX_SEQ;
  BX.root = root; BX.vid = +id;
  $('#bxRows').innerHTML = '<div class="empty dim">读取中…</div>';
  try {
    BX.rep = await api(`/api/boxaudit/list?root=${encodeURIComponent(root)}&video_id=${id}`);
  } catch (e) {
    $('#bxRows').innerHTML = '<div class="empty bad">读不出来，看服务端日志</div>';
    return;
  }
  if (my !== BX_SEQ) return;   // 已经有更新的请求或改动了，这份作废
  BX.list = BX.rep.items || [];
  BX.key = v;
  const r = BX.rep;
  const pct = r.checked ? Math.round(r.rate * 100) : null;
  $('#bxLib').textContent = `${r.video.name} · ${r.total} 条台词`;
  $('#bxSum').innerHTML = `
    <div class="bxs"><b>${r.total}</b><span>台词</span></div>
    <div class="bxs"><b class="ok">${r.passed}</b><span>方框已验</span></div>
    <div class="bxs"><b class="${r.failed ? 'bad' : 'dim'}">${r.failed}</b><span>可疑</span></div>
    <div class="bxs"><b class="${r.no_box ? 'warn' : 'dim2'}">${r.no_box}</b><span>无框凭证</span></div>
    <div class="bxs"><b>${pct == null ? '—' : pct + '%'}</b><span>通过率</span></div>
    <div class="bxreasons">${Object.keys(r.reasons || {}).map(k =>
        `<span class="tag bad">${esc(k)} ${r.reasons[k]}</span>`).join('')
      || (r.failed ? '' : '<span class="dim">没有可疑项</span>')}</div>`;
  renderBxRows();
  if (BX.list.length) selectBx(BX.list[0].id);
  else $('#bxView').innerHTML = '<div class="empty dim">这个素材没有台词</div>';
}

function bxVisible() {
  const q = BX.q.trim().toLowerCase();
  return BX.list.filter(x => {
    if (BX.filter === 'bad' && (x.skipped || x.ok)) return false;
    if (BX.filter === 'nobox' && x.has_box) return false;
    if (q && !(x.text || '').toLowerCase().includes(q)) return false;
    return true;
  });
}

function renderBxRows() {
  const rows = bxVisible();
  $('#bxRows').innerHTML = rows.map(x => `
    <div class="bxrow ${BX.cur === x.id ? 'on' : ''}" data-id="${x.id}">
      <div class="bxts">${x.start_str}<span class="dim2">${x.end_str.slice(0, 8)}</span></div>
      <div class="bxtx">${esc(x.text || '')}</div>
      <div class="bxmark ${bxCls(x)}">${bxMarkTxt(x)}</div>
    </div>`).join('')
    || '<div class="empty dim">没有符合条件的台词</div>';
  $$('#bxRows .bxrow').forEach(el =>
    el.onclick = () => selectBx(+el.dataset.id));
}

function bxCls(x) {
  if (x.skipped) return 'skip';
  if (!x.has_box) return 'nobox';
  return x.ok ? 'ok' : 'bad';
}
function bxMarkTxt(x) {
  if (x.skipped) return '无框(正常)';
  if (!x.has_box) return '无框凭证';
  const base = x.ok ? '方框已验' : (x.reasons[0] || '方框异常');
  // 字体偏离是另一个维度：不判伪，追加一个提示徽标
  return base + (((x.metrics || {}).font_dev || 0) > 1.5 ? ' · 字体异常' : '');
}

/* 帧 URL：加一个 seq 时间戳参数，翻同一条时能强制重画（识别框变了要重出图）

   `live` 只在**这一句刚跑过识别**时才传 1。服务端 live=1 会重抽一帧跑一遍
   OCR（1~3 秒），而每次切句都要重新出图——无条件带上 live 的话，翻一句
   就等三秒。而"叠识别框"本来也只有识别过之后才有东西可叠：没识别过那一栏
   是空的，画不出任何信息，纯白烧算力。

   坑（真踩过）：单句编辑保存后走的是同句重渲染，无条件 live=1 的话，
   改一个字也要等 OCR 跑完，面板在那儿闪十几秒（实测 15.9s vs 0.4s）。 */
let BX_IMG_SEQ = 0;
function bxFrameUrl(x, opt) {
  const o = opt || {};
  const p = new URLSearchParams({
    root: BX.root, video_id: BX.vid, line_id: x.id,
    view: 'full',
    live: (o.live && BX.probeOut) ? '1' : '0',
    seq: String(++BX_IMG_SEQ),
  });
  return `/api/boxaudit/frame?${p}`;
}

function bxMetricsHtml(m) {
  if (!m) return '';
  const rows = [
    ['框心 cx/cy', `${m.cx ?? '—'} / ${m.cy ?? '—'}`],
    ['框宽 × 字高', `${m.bw ?? '—'} × ${m.bh ?? '—'}`],
    ['字宽比 ar', m.ar ?? '—', m.ar != null &&
      (m.ar < 0.55 || m.ar > 1.45) ? 'bad' : 'ok'],
    ['置信度', m.conf ?? '—', m.conf != null && m.conf < 0.62 ? 'bad' : 'ok'],
    ['支持帧数', m.hits ?? 0],
    ['覆盖率', m.cov == null ? '—' : Math.round(m.cov * 100) + '%',
      m.cov != null && (m.cov < 0.34 || m.cov > 1.25) ? 'bad' : 'ok'],
    ['跨帧漂移', m.drift ?? '—', m.drift != null && m.drift > 0.10 ? 'bad' : 'ok'],
    ['框数 / 字数', `${m.nbox ?? '—'} / ${m.nchar ?? '—'}`],
  ];
  // 字体指纹：墨密度 + 笔画宽度 + 字高占画面比例。偏离大的行不判伪，
  // 但二次复查时要意识到"这句的字和本部片主流不一样"。
  const fnt = m.font || {};
  if (fnt.ink != null) {
    rows.push(['字体 墨密度/笔画', `${fnt.ink} / ${fnt.stroke ?? '—'}px`]);
    rows.push(['字高占画面', fnt.px_h != null
      ? (fnt.px_h * 100).toFixed(1) + '%' : '—']);
    rows.push(['字体偏离度', m.font_dev ?? '—',
      m.font_dev != null && m.font_dev > 1.5 ? 'warn' : 'ok']);
  }
  return `<table class="bxmtbl">${rows.map(r =>
    `<tr><td>${r[0]}</td><td class="${r[2] || ''}">${r[1]}</td></tr>`).join('')}</table>`;
}

function selectBx(id) {
  const x = BX.list.find(y => y.id === id);
  if (!x) return;
  // 换句就丢掉上一句的识别报告——它属于别的句子，留着只会误导。
  // 同一句内部的重渲染（保存后刷新）靠 outKey 保住报告。
  if (BX.cur !== id) BX.probeOut = '';
  BX.cur = id;
  renderBxRows();
  const reasons = (x.reasons || []).join('、');
  $('#bxView').innerHTML = `
    <div class="bxhead">
      <div class="bxhts">${x.start_str} → ${x.end_str}
        <span class="dim2">${esc(x.src || '未知来源')}</span></div>
      <div class="bxtools">
        <label class="chk dim2 ${BX.probeOut ? '' : 'hidden'}" id="bxLiveWrap"
          title="把这次识别到的框用绿虚线叠在蓝框上。两个框分开就说明这条的框不稳"
          ><input type="checkbox" id="bxLive" checked> 叠识别框</label>
        <button class="tiny" id="bxPrev" title="上一句台词（快捷键 ↑）">← 上一句</button>
        <button class="tiny" id="bxNext" title="下一句台词（快捷键 ↓）">下一句 →</button>
      </div>
    </div>

    <div class="bximg">
      <div class="spin dim" id="bxSpin">正在抽帧…</div>
      <img id="bxImg" alt="台词帧">
      <div class="bxlegend">
        <span><i style="background:#4d9dff"></i>库里的框</span>
        <span><i class="dash" style="background:#2ec27e"></i>这次识别到的框</span>
      </div>
    </div>

    <div class="bxtext">
      <div class="bxfld"><span>库里文字</span><b id="bxSavedTxt">${esc(x.text || '')}</b></div>
      <div class="bxfld"><span>重新识别结果</span><b id="bxLiveTxt" class="dim">还没识别</b></div>
    </div>

    <div class="bxfoot">
      <div class="bxverdict ${bxCls(x)}">
        <b>${bxMarkTxt(x)}</b>
        ${reasons ? `<span class="dim">${esc(reasons)}</span>` : ''}
        ${x.skipped ? `<span class="dim">${esc(x.src)} 来源本来就没有画面框</span>` : ''}
      </div>
      <div class="bxacts">
        <button class="tiny" id="bxProbe"
          title="回画面重抽首/中/尾三帧重跑 OCR，逐帧比对框和文字对不对得上。只出报告，不改库">重新 OCR 识别</button>
        <button class="tiny" id="bxAsr"
          title="只听这一句前后各 0.6 秒的音频，把听到的话显示出来。不自动覆盖库里的台词，看过后自己点「编辑台词」决定改不改">语音识别这句</button>
        <button class="tiny primary" id="bxEdit"
          title="改这句话的文字和起止时间，改完立刻生效并同步检索索引；可勾选顺带把识别到的画面方框写进库">编辑台词</button>
        <button class="tiny" id="bxPlay"
          title="从这句台词的时间点开始播放原片">播放这句</button>
      </div>
    </div>
    <div class="bxprobe" id="bxProbeOut">${BX.probeOut || ''}</div>
    <div class="bxcmp" id="bxCmp">${bxMetricsHtml(x.metrics)}</div>`;

  const img = $('#bxImg');
  img.onload = () => { $('#bxSpin').style.display = 'none'; };
  img.onerror = () => {
    $('#bxSpin').textContent = '这一帧抽不出来（文件可能不在了）';
  };
  img.src = bxFrameUrl(x, { live: true });

  const lv = $('#bxLive');
  if (lv) lv.onchange = () => { img.src = bxFrameUrl(x, { live: lv.checked }); };
  $('#bxPrev').onclick = () => bxStep(-1);
  $('#bxNext').onclick = () => bxStep(1);
  $('#bxPlay').onclick = () => play(BX.vid, x.start, x.id, encodeURIComponent(BX.root));
  $('#bxProbe').onclick = () => doBxProbe(x);
  $('#bxAsr').onclick = () => doBxAsr(x);
  $('#bxEdit').onclick = () => openBxEdit(x);
}

/* 识别跑完后调用：把「叠识别框」开关放出来，并按当前勾选状态重出图。
   开关之前是藏着的（没识别过没什么可叠），这时才有意义露出来。
   用 .hidden **类**而不是 hidden 属性：.chk 自带 display:inline-flex，
   作者样式会盖过浏览器默认的 [hidden]{display:none}，属性会失效。 */
function bxRevealLive() {
  const wrap = $('#bxLiveWrap');
  if (wrap) wrap.classList.remove('hidden');
  const lv = $('#bxLive');
  const img = $('#bxImg');
  if (lv && img) img.src = bxFrameUrl(BX.list.find(y => y.id === BX.cur) || {},
                                      { live: lv.checked });
}

function bxStep(d) {
  const rows = bxVisible();
  if (!rows.length) return;
  const i = rows.findIndex(x => x.id === BX.cur);
  const n = rows[Math.max(0, Math.min(rows.length - 1, (i < 0 ? 0 : i + d)))];
  if (n) {
    selectBx(n.id);
    const el = $(`#bxRows .bxrow[data-id="${n.id}"]`);
    if (el) el.scrollIntoView({ block: 'nearest' });
  }
}

async function doBxProbe(x) {
  if (BX.busy) return;
  BX.busy = true;
  const out = $('#bxProbeOut');
  const btn = $('#bxProbe');
  btn.disabled = true;
  out.innerHTML = '<div class="dim">正在抽 3 帧重新跑 OCR，约 1~3 秒…</div>';
  try {
    const r = await api('/api/boxaudit/probe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: BX.root, video_id: BX.vid, line_id: x.id }),
    });
    out.innerHTML = `
      <div class="bxpv ${r.verdict === 'OK' ? 'ok' : 'bad'}">
        <b>${r.verdict === 'OK' ? '识别一致' : '识别不一致'}</b>
        <span class="dim">${esc(r.detail || '')}</span>
      </div>
      <table class="bxpbtbl">${(r.frames || []).map(f => `
        <tr class="${f.match === true ? 'ok' : (f.match === false ? 'bad' : '')}">
          <td>${f.t.toFixed(2)}s</td>
          <td>${esc(f.text || '—')}</td>
          <td>${f.box ? `偏移 ${f.dx ?? '?'} / ${f.dy ?? '?'}` : '没找到字幕框'}</td>
          <td>${esc(f.why || '')}</td>
        </tr>`).join('')}</table>`;
    // 把识别文本填到"重新识别结果"那一栏，文字对不对一眼就有答案了
    const lt = (r.frames || []).filter(f => f.box).map(f => f.text).filter(Boolean);
    if (lt.length) {
      const el = $('#bxLiveTxt');
      if (el) {
        el.textContent = lt[0];
        el.className = r.verdict === 'OK' ? 'ok' : 'bad';
      }
    }
    toast(r.verdict === 'OK' ? '重新识别完成，结果一致'
                              : '重新识别完成，结果对不上：' + (r.detail || ''));
  } catch (e) {
    out.innerHTML = `<div class="bad">识别失败：${esc(String(e))}</div>`;
  } finally {
    // 报告留在 BX 里：保存后同句重渲染还会用到，别让面板自己空了。
    // 必须在 bxRevealLive() **之前**赋值——那边靠 BX.probeOut 判断
    // 要不要真的去重跑 OCR 出图，顺序反了这次就白出一次不带框的图。
    BX.probeOut = out.innerHTML;
    const lv = $('#bxLive');
    if (lv && lv.checked) bxRevealLive();
    btn.disabled = false;
    BX.busy = false;
  }
}

/* 单句语音识别：只听这一条的时间区间，1~3 秒出结果。
   识别文本**不自动写库**——出现在右侧那一栏，用户自己比对后决定
   是"照抄进编辑框"还是"忽略"。自动覆盖人工校对过的台词是不能接受的。 */
async function doBxAsr(x) {
  if (BX.busy) return;
  BX.busy = true;
  const out = $('#bxProbeOut');
  const btn = $('#bxAsr');
  btn.disabled = true;
  out.innerHTML = '<div class="dim">正在抽这段音频跑语音识别（首次要加载模型，约 10 秒）…</div>';
  try {
    const r = await api('/api/boxaudit/asr', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: BX.root, video_id: BX.vid, line_id: x.id }),
    });
    if (!r.ok) throw new Error(r.err || '识别失败');
    out.innerHTML = `
      <div class="bxpv ${r.text ? 'ok' : 'bad'}">
        <b>语音识别${r.text ? '完成' : '没听出内容'}</b>
        <span class="dim">${esc(r.dur)}s 音频 · ${esc(r.device || r.model || '')}</span>
      </div>
      <div class="bxfld" style="margin-top:8px"><span>听到的</span>
        <b>${esc(r.text || '（这一段没有人声）')}</b></div>
      ${r.text ? '<div class="dim" style="margin-top:7px">觉得对就点「编辑台词」把它抄进去，识别本身不会自动改库。</div>' : ''}`;
    const el = $('#bxLiveTxt');
    if (el) {
      el.textContent = r.text || '（没听出内容）';
      el.className = r.text ? 'warn' : 'dim';
    }
    toast(r.text ? '语音识别完成，觉得对就点「编辑台词」抄进去'
                 : '这一段没听出内容');
  } catch (e) {
    out.innerHTML = `<div class="bad">语音识别失败：${esc(String(e.message || e))}</div>`;
    toast('语音识别失败');
  } finally {
    BX.probeOut = out.innerHTML;
    btn.disabled = false;
    BX.busy = false;
  }
}

/* 编辑台词：文字 + 起止时间一次改完，可选顺带把这次识别到的框写进库。
   为什么把"回填方框"收进这个弹窗：原来那个「确认并回填方框」按钮
   单独摆在外面，说的却是"先重跑一次识别，三帧对得上才写框"——
   按钮名和行为对不上。现在改成勾选框，行为写在勾选项旁边。 */
function openBxEdit(x) {
  const live = $('#bxLiveTxt');
  const suggested = (live && live.className !== 'dim') ? live.textContent : '';
  openModal('编辑台词', `
    <div class="mrow dim">${x.start_str} → ${x.end_str} · ${esc(x.src || '未知来源')} 来源</div>
    <div class="mrow">
      <textarea id="bxEText" rows="3" class="bxed"
        placeholder="这句话的文本">${esc(x.text || '')}</textarea>
    </div>
    <div class="mgrid">
      <label>开始（秒）<input id="bxEStart" type="number" step="0.01"
        value="${x.start}"></label>
      <label>结束（秒）<input id="bxEEnd" type="number" step="0.01"
        value="${x.end}"></label>
    </div>
    <label class="chk" style="display:flex;margin-top:10px">
      <input type="checkbox" id="bxESaveBox"> 同时把这次识别到的画面方框写进库
    </label>
    <div class="dim" style="margin-top:5px">勾上后会重新抽 3 帧跑 OCR，
      三帧文字都对得上才写入；不一致会直接告诉你，什么都不会改。</div>
    ${suggested ? `<div class="mfoot" style="justify-content:flex-start">
      <button id="bxEUse" title="把识别到的这句话直接填进上面的文本框，再手动修一修"
        >用「${esc(suggested.slice(0, 14))}${suggested.length > 14 ? '…' : ''}」填入</button>
    </div>` : ''}
    <div class="mfoot">
      <button id="mCancel">取消</button>
      <button id="mYes" class="primary">保存</button>
    </div>`);
  const ta = $('#bxEText');
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  const use = $('#bxEUse');
  if (use) use.onclick = () => { ta.value = suggested; ta.focus(); };
  $('#mCancel').onclick = closeModal;
  $('#mYes').onclick = async () => {
    const payload = {
      root: BX.root, video_id: BX.vid, line_id: x.id,
      text: $('#bxEText').value,
      start: Number($('#bxEStart').value),
      end: Number($('#bxEEnd').value),
      save_box: $('#bxESaveBox').checked,
    };
    closeModal();
    try {
      const r = await api('/api/boxaudit/edit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        askAlert('没保存', r.err || '未知原因');
        return;
      }
      const v = r.verify || {};
      toast(v.ok ? '已保存，这条现在过校验'
                 : '已保存，但校验提示：' + ((v.reasons || []).join('、') || '框还没写'));
      // 只把这一条的新摘要打回列表，不重载整份清单：
      // loadBoxVideo 会把这部片的每一行都重跑一遍 verify（一部片两千条，
      // 实测要好几秒），改一句话却要等几秒、期间面板还闪回旧内容。
      // 接口已经把改完这一条的 audit 摘要带回来了，直接换掉即可。
      const patch = r.audit;
      if (patch && patch.id === x.id) {
        // 先把代次 +1：让还在飞的清单请求作废，否则它回来会把这次改动抹掉
        BX_SEQ++;
        BX.list = BX.list.map(y => (y.id === x.id ? patch : y));
        if (BX.rep) BX.rep.items = BX.list;   // 两处是同一份，别留旧引用
        // 汇总数字跟着变（可疑 / 无框 / 通过率）
        await refreshBxSummary();
        renderBxRows();
        selectBx(x.id);
      } else {
        await loadBoxVideo(true);
        selectBx(x.id);
      }
    } catch (e) {
      askAlert('保存失败', String(e.message || e));
    }
  };
}

/* 只重算顶部汇总那几个数字，不重跑整份清单的 verify。
   依赖 BX.rep 里的逐条 items——改完一条把那一条换进去，
   剩下的原样不动。 */
async function refreshBxSummary() {
  const r = BX.rep;
  if (!r) return;
  const items = BX.list;
  const checked = items.filter(x => !x.skipped);
  const bad = checked.filter(x => !x.ok);
  const nobox = items.filter(x => !x.has_box);
  const reason = {};
  for (const x of bad) {
    for (const rr of x.reasons || []) {
      const k = rr.split(' ')[0];
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
  const pct = r.checked ? Math.round(r.rate * 100) : null;
  $('#bxSum').innerHTML = `
    <div class="bxs"><b>${r.total}</b><span>台词</span></div>
    <div class="bxs"><b class="ok">${r.passed}</b><span>方框已验</span></div>
    <div class="bxs"><b class="${r.failed ? 'bad' : 'dim'}">${r.failed}</b><span>可疑</span></div>
    <div class="bxs"><b class="${r.no_box ? 'warn' : 'dim2'}">${r.no_box}</b><span>无框凭证</span></div>
    <div class="bxs"><b>${pct == null ? '—' : pct + '%'}</b><span>通过率</span></div>
    <div class="bxreasons">${Object.keys(r.reasons || {}).map(k =>
        `<span class="tag bad">${esc(k)} ${r.reasons[k]}</span>`).join('')
      || (r.failed ? '' : '<span class="dim">没有可疑项</span>')}</div>`;
}

/* ---------- 片段 ---------- */
async function clip(line_id, rootEnc) {
  const r = await api('/api/clip', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      root: decodeURIComponent(rootEnc || ''), line_id,
      pre: +$('#pre').value, post: +$('#post').value, fast: $('#fast').checked
    })
  });
  addClips(r);
}
function addClips(r) {
  if (!r || !r.ok) { toast((r && r.err) || '导出失败'); return; }
  CLIPS = (r.clips || []).concat(CLIPS);
  $('#badgeClips').textContent = CLIPS.length;
  renderClips();
  switchTab('clips');
}
function renderClips() {
  $('#clips').innerHTML = CLIPS.map(c => `
    <div class="cchip">
      <div class="txt">${esc(c.text || c.name)}</div>
      <div class="dim">${esc(c.name)}</div>
      <video src="/clip?p=${encodeURIComponent(c.rel)}&root=${encodeURIComponent(c.root)}" controls preload="metadata"></video>
      <div class="chipops">
        <a href="/clip?p=${encodeURIComponent(c.rel)}&root=${encodeURIComponent(c.root)}&dl=1"
          title="把这个 mp4 下载到本地">下载</a>
        <a href="javascript:;" onclick="openPath('${encodeURIComponent((c.root || '') + '\\.frameseek\\clips')}')"
          title="在资源管理器里打开片段输出目录">打开所在文件夹</a>
      </div>
    </div>`).join('') || '<div class="dim">还没有导出片段。在「台词检索」里搜到台词后点「导出片段」</div>';
}

/* ---------- 事件 ---------- */
$('#btnAddLib').onclick = async () => {
  const p = $('#libPath').value.trim();
  if (!p) { toast('先填素材目录路径'); return; }
  const r = await api('/api/libraries', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: p, name: $('#libName').value.trim() })
  });
  if (!r.ok) { askAlert('登记失败', r.err || '目录不存在或无法访问'); return; }
  $('#libPath').value = ''; $('#libName').value = '';
  toast(`已登记「${r.name}」`);
  loadLibs();
};
$('#btnScanAll').onclick = async () => {
  const asr = LS.get('use_asr', true), gif = LS.get('with_gif', true),
        retry = LS.get('retry_empty', true);
  openModal('索引全部素材库', `
    <div class="mrow"><label><input type="checkbox" id="mAsr" ${asr ? 'checked' : ''}> 无字幕时用 OCR 识别画面</label></div>
    <div class="mrow dim">台词来源：同名 .srt → 内嵌字幕 → OCR 识别画面硬字幕。
      OCR 实测 8 进程约 1.5~2.1 倍实时（1 小时素材约 30~40 分钟）。
      片子多的话先配同名 .srt 字幕更划算（秒级）。</div>
    <div class="mrow"><label><input type="checkbox" id="mRetry" ${retry ? 'checked' : ''}> 重新补一遍没有台词的素材</label></div>
    <div class="mrow"><label><input type="checkbox" id="mGif" ${gif ? 'checked' : ''}> 生成动图预览</label></div>
    <div class="mrow dim">共 ${LIBS.length} 个库，进度在下方按库分别显示。</div>
    <div class="mfoot"><button id="mCancel">取消</button>
      <button id="mGo" class="primary">开始索引</button></div>`);
  $('#mCancel').onclick = closeModal;
  $('#mGo').onclick = async () => {
    const a = $('#mAsr').checked, g = $('#mGif').checked, r = $('#mRetry').checked;
    LS.set('use_asr', a); LS.set('with_gif', g); LS.set('retry_empty', r);
    await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_asr: a, with_gif: g })
    });
    await api('/api/libraries/scan_all', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_asr: a, with_gif: g, retry_empty: r })
    });
    closeModal(); startPoll(); toast('开始索引全部库');
  };
};
$('#btnSearch').onclick = search;
$('#q').onkeydown = e => { if (e.key === 'Enter') search(); };
/* 重置所有筛选条件（按钮和自动化测试共用） */
function resetFilters() {
  FILTER.libs.clear();
  FILTER.res.clear(); FILTER.dur.clear();
  FILTER.lines.clear();               // 台词状态默认不限
  setChipGroup('fLines', []);
  setChipGroup('fRes', []);
  setChipGroup('fDur', []);
  ['fCodec', 'fFps', 'fAge'].forEach(id => { const e = $('#' + id); if (e) e.value = ''; });
  FILTER.codec = ''; FILTER.fps = ''; FILTER.age = '';
  FILTER.hasGif = FILTER.hasCover = FILTER.noGif = false;
  ['fHasGif', 'fHasCover', 'fNoGif'].forEach(id => {
    const e = $('#' + id); if (e) e.checked = false;
  });
  $('#vfilter').value = '';
  renderLibChips();
  return loadAssets();
}

$('#vfilter').oninput = loadAssets;
$('#vMoreBtn').onclick = () => {
  $('#vFilterCard').classList.toggle('hidden');
  $('#vMoreBtn').textContent =
    $('#vFilterCard').classList.contains('hidden') ? '筛选 ▾' : '筛选 ▴';
};
$('#vResetBtn').onclick = resetFilters;
['fLines', 'fRes', 'fDur'].forEach(id => {
  document.querySelectorAll(`#${id} input`).forEach(i => {
    i.onchange = () => {
      const set = id === 'fLines' ? FILTER.lines
                : id === 'fRes' ? FILTER.res : FILTER.dur;
      set.clear();
      document.querySelectorAll(`#${id} input:checked`).forEach(x => set.add(x.value));
      loadAssets();
    };
  });
});
[['#fCodec', 'codec'], ['#fFps', 'fps'], ['#fAge', 'age']].forEach(([sel, key]) => {
  const el = $(sel);
  if (el) el.onchange = e => { FILTER[key] = e.target.value; loadAssets(); };
});
['fHasGif', 'fHasCover', 'fNoGif'].forEach(id => {
  $('#' + id).onchange = () => {
    FILTER.hasGif = $('#fHasGif').checked;
    FILTER.hasCover = $('#fHasCover').checked;
    FILTER.noGif = $('#fNoGif').checked;
    loadAssets();
  };
});
['pre', 'post'].forEach(id => $('#' + id).oninput = () => {
  $('#preEcho').textContent = $('#pre').value;
  $('#postEcho').textContent = $('#post').value;
});

/* 方框复查：素材选择 / 过滤 / 快捷键 */
if ($('#bxPick')) {
  $('#bxPick').onchange = onBxPick;
  $('#bxLibPick').onchange = onBxLibChange;
  // 边打边筛：片单已在本地，等用户停手 120ms 再渲染，省掉每键一次重排
  let _bxT = 0;
  $('#bxSearch').oninput = () => {
    clearTimeout(_bxT);
    _bxT = setTimeout(onBxSearch, 120);
  };
  $('#bxReload').onclick = () => loadBoxLibs(true);   // 强制重拉：忽略已缓存的清单
  $('#bxFilter').oninput = e => { BX.q = e.target.value; renderBxRows(); };
  $$('#bxSegs button').forEach(b => b.onclick = () => {
    BX.filter = b.dataset.f;
    $$('#bxSegs button').forEach(y => y.classList.toggle('on', y === b));
    renderBxRows();
  });
  // ↑/↓ 换句。输入框里打字时不抢按键。
  document.addEventListener('keydown', e => {
    if (currentTab() !== 'box') return;
    const t = e.target.tagName;
    if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return;
    if ($('#modal').classList.contains('hidden') === false) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); bxStep(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); bxStep(-1); }
  });
}

/* 启动 */
(async function boot() {
  await loadLibs();
  wireGen();
  const h = (location.hash || '').slice(1);
  switchTab($$('.nav-item').some(a => a.dataset.tab === h) ? h : 'libs');
  // 页面一打开就连实时通道，索引中的进度不用等用户点任何按钮
  connectLive();
  // 首屏可能正有任务在跑（服务端线程还活着）。只当成"临时快照"——
  // WS 一旦推来真事件就会覆盖它；WS 不可用时它才长期顶替轮询。
  if (!LIVE.on) {
    let ts = [];
    try { ts = await api('/api/tasks'); } catch (e) { ts = []; }
    (ts || []).forEach(t => {
      if (!t.root) return;
      // 服务端启动时会把上次残留的 running 标成 interrupted，
      // 这里只收真在跑的（后端 latest_task 只返回 running，这里再兜一层时间）
      if (t.status !== 'running') return;
      LIVE.tasks[t.root] = { root: t.root, lib: t.lib, done: t.done,
        total: t.total, message: t.message, phase: '', pct: t.pct,
        kind: t.kind || '' };
    });
    renderTasks();
  }
})();

/* ================= 白板手绘面板 ================= */
const HD = {
  ready: false, missing: [], voice: '', subdir: '', base: '', outDir: '',
  project: '', upScript: '', upImage: '',
};

function hdRoot() { return ACTIVE_ROOT || (LIBS[0] && LIBS[0].path) || ''; }

function hdSetStep(n) {
  $$('.hd-step').forEach(el => {
    const s = +el.dataset.s;
    el.classList.toggle('on', s === n);
    el.classList.toggle('done', s < n);
  });
}

/* ---- 气泡会话 ---- */
function hdSyncChat() {
  const box = $('#hdChat');
  if (!box) return;
  const empty = $('#hdChatEmpty');
  if (empty) empty.classList.toggle('hidden', HD.msgs.length > 0);
  box.innerHTML = HD.msgs.map(m => {
    if (m.run) {
      const pct = Math.min(100, Math.max(0, m.pct || 0));
      return `<div class="bub sys"><div class="bub-txt">${esc(m.text || '进行中…')}</div>
        <div class="bub-run"><i style="width:${pct}%"></i></div>
        <div class="bub-meta">${esc(m.meta || (pct + '%'))}</div></div>`;
    }
    if (m.kind === 'text') {
      return `<div class="bub ${m.who === 'me' ? 'me' : 'sys'}">
        <div class="bub-txt">${esc(m.text)}</div>
        ${m.meta ? `<div class="bub-meta">${esc(m.meta)}</div>` : ''}</div>`;
    }
    if (m.kind === 'err') {
      return `<div class="bub sys"><div class="bub-txt bub-err">${esc(m.text)}</div></div>`;
    }
    if (m.kind === 'media') {
      // 手绘产物在 <项目根>/手绘工程 与 <项目根>/生成 下，不在 .frameseek/cache，
      // 所以走 /hd/media（限定项目目录内），不能用 mediaUrl()
      const url = `/hd/media?p=${encodeURIComponent(m.file)}`;
      const inner = m.image
        ? `<img src="${url}" alt="检查图">`
        : `<video src="${url}" controls playsinline></video>`;
      return `<div class="bub sys"><div class="bub-txt">${esc(m.text || '')}</div>
        <div class="hd-media">${inner}</div>
        <div class="hd-acts">
          <a class="tiny" href="${url}" target="_blank">打开原文件</a>
          <span class="dim">${esc(m.file.split(/[\\/]/).pop())}</span>
        </div></div>`;
    }
    if (m.kind === 'kv') {
      const rows = (m.rows || []).map(([k, v]) =>
        `<b>${esc(k)}</b><span>${esc(v)}</span>`).join('');
      return `<div class="bub sys"><div class="bub-txt">${esc(m.text || '')}</div>
        <div class="hd-kv">${rows}</div></div>`;
    }
    return '';
  }).join('');
  box.scrollTop = box.scrollHeight;
}

function hdSay(who, text, meta) { HD.msgs.push({ kind: 'text', who, text, meta }); hdSyncChat(); }
function hdErr(text) { HD.msgs.push({ kind: 'err', text }); hdSyncChat(); }
function hdNote(text, meta) { hdSay('sys', text, meta); }
function hdMedia(file, text, isImage) {
  HD.msgs.push({ kind: 'media', file, text, image: !!isImage }); hdSyncChat();
}
function hdKV(text, rows) { HD.msgs.push({ kind: 'kv', text, rows }); hdSyncChat(); }
function hdClear() { HD.msgs = []; hdSyncChat(); }

/* ---- 渲染/混流的实时进度 → 最后一条系统气泡 ---- */
function hdLastRun() {
  for (let i = HD.msgs.length - 1; i >= 0; i--) {
    if (HD.msgs[i].run) return HD.msgs[i];
  }
  return null;
}

function hdTickRun(_root, pct, msg) {
  const m = hdLastRun();
  if (!m) {
    HD.msgs.push({ run: true, text: '手绘任务进行中…', pct: 0, meta: '' });
    hdSyncChat();
    return hdTickRun(_root, pct, msg);
  }
  m.pct = Math.max(m.pct || 0, pct || 0);
  m.text = msg || m.text;
  m.meta = `${m.pct}%`;
  hdSyncChat();
}

function hdRunDone(s) {
  const m = hdLastRun();
  if (s.error) {
    if (m) { HD.msgs.splice(HD.msgs.indexOf(m), 1); }
    hdErr(s.error);
    return;
  }
  if (m) { HD.msgs.splice(HD.msgs.indexOf(m), 1); }
  const f = s.file || s.library_file || '';
  if (f) {
    hdMedia(f, `⑥ 完成：${s.name || ''}`
      + (s.library_file ? '（已拷进素材库 AI生成）' : ''), false);
  } else {
    hdNote('手绘任务完成：' + (s.name || ''));
  }
  hdRefreshStatus();
}

/* ---- 引擎摸底 ---- */
async function hdLoadInfo() {
  const s = $('#hdSrv');
  if (!s) return;
  try {
    const d = await api('/api/handdraw/info');
    if (d.ok === false) throw new Error(d.err || '引擎异常');
    HD.ready = !!d.ready;
    HD.missing = d.missing || [];
    HD.voice = d.default_voice || '';
    HD.subdir = d.project_root_name || '手绘工程';
    HD.base = d.project_root_dir || '';
    HD.outDir = d.out_dir || '';
    const hod = $('#hdOutDir');
    if (hod) hod.textContent = '成片 → ' + (HD.outDir || '项目目录/生成');
    const hrd = $('#hdRootDir');
    if (hrd) hrd.textContent = '工程 → ' + (HD.base || '项目目录/手绘工程');
    s.innerHTML = HD.ready
      ? `引擎就绪 · <b>${esc(d.engine)}</b> · 音色 ${esc(HD.voice)}`
      : `<span class="bub-err">未就绪：${esc(HD.missing.join('；'))}</span>`;
    const btn = $('#hdInit');
    if (btn) btn.disabled = !HD.ready;
  } catch (e) {
    HD.ready = false;
    s.innerHTML = `<span class="bub-err">摸底失败：${esc(String(e.message || e))}</span>`;
  }
}

async function hdLoadLibs() {
  // 工程不再依赖素材库（2026-10-15），选择器已移除；保留函数只为兼容调用。
  const sel = $('#hdLib');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = LIBS.length
    ? LIBS.map(l => `<option value="${esc(l.path)}">${esc(l.name || l.path)}</option>`).join('')
    : '<option value="">先登记一个素材库</option>';
  const want = keep || hdRoot();
  if (want && LIBS.some(l => l.path === want)) sel.value = want;
}

async function hdRefreshStatus() {
  if (!HD.project) return;
  try {
    const d = await api(`/api/handdraw/status?root=${encodeURIComponent(hdRoot())}` +
      `&project=${encodeURIComponent(HD.project)}`);
    if (d.ok === false) { HD.project = ''; return; }
    const st = $('#hdTlStatus'), adv = $('#hdTlAdvice');
    if (st) st.textContent = d.timelineStatus || '-';
    if (adv) adv.textContent = d.advice || '';
    const approved = d.timelineStatus === 'approved';
    const rbtn = $('#hdRender');
    if (rbtn) rbtn.disabled = !approved;
    const nScenes = Array.isArray(d.scenes) ? d.scenes.length : (d.scenes || 0);
    $('#hdProj').innerHTML =
      `工程 <b>${esc(HD.project)}</b> · ${nScenes} 幕 · 画面 ${((d.picture_total_ms || 0) / 1000).toFixed(1)}s` +
      ` / 字幕 ${((d.cue_total_ms || 0) / 1000).toFixed(1)}s · 时间轴 <b>${esc(d.timelineStatus)}</b>`;
  } catch (e) { /* 工程可能还没建 */ }
}

/* ---- 1 建工程 ---- */
async function hdUpload(kind) {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = kind === 'script' ? '.txt,.srt' : '.png,.jpg,.jpeg,.webp,.bmp';
  inp.onchange = async () => {
    const f = inp.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f);
    const d = await api('/api/handdraw/upload', { method: 'POST', body: fd });
    if (d.ok === false) { hdErr(d.err || '上传失败'); return; }
    if (kind === 'script') {
      HD.upScript = d.path;
      const n = $('#hdUpName');
      if (n) n.textContent = `${d.name}（${d.kind === 'srt' ? '字幕' : '文稿'}）`;
      if (!$('#hdProjName').value) $('#hdProjName').value = d.name.replace(/\.(txt|srt)$/i, '');
    } else {
      HD.upImage = d.path;
      const n = $('#hdImgName');
      if (n) n.textContent = d.name;
    }
    toast('已上传 ' + d.name);
  };
  inp.click();
}

async function hdInit() {
  if (!HD.upScript) { hdErr('先上传一个 .txt 文稿或 .srt 字幕'); return; }
  const p = $('#hdProjName').value.trim();
  const t = $('#hdTitle').value.trim();
  hdSay('me', '① 建工程', p ? p : '(自动命名)');
  const d = await api('/api/handdraw/init', {
    method: 'POST',
    body: JSON.stringify({
      root: hdRoot(),
      script: /\.srt$/i.test(HD.upScript) ? '' : HD.upScript,
      srt: /\.srt$/i.test(HD.upScript) ? HD.upScript : '',
      name: p, title: t,
    }),
  });
  if (d.ok === false) { hdErr(d.err || '建工程失败'); return; }
  HD.project = p || (d.project || '').replace(/^.*[\\/]/, '');
  HD.upImage = '';
  $('#hdImgName').textContent = '未选择';
  hdNote(`工程就绪：${d.project} · ${d.cues} 条字幕 · 时间轴「${d.timelineStatus}」（预估，非配音对齐结果）`);
  hdSetStep(2);
  hdRefreshStatus();
  hdSuggest();
}

/* ---- 2 分幕建议 ---- */
async function hdSuggest() {
  if (!HD.project) { hdErr('还没有工程，先做第 1 步'); return; }
  const ts = +$('#hdTargetSec').value || 30;
  const d = await api('/api/handdraw/scenes', {
    method: 'POST',
    body: JSON.stringify({ root: hdRoot(), project: HD.project, target_sec: ts }),
  });
  if (d.ok === false) { hdErr(d.err || '出分幕建议失败'); return; }
  const cues = d.cues || [], scenes = d.scenes || [];
  const total = scenes.reduce((a, s) => a + (s.sceneDurationMs || 0), 0);
  const cueMs = cues.length ? cues[cues.length - 1].endMs : 0;
  HD.msgs.push({
    kind: 'text', who: 'sys',
    text: `② 分幕建议：${scenes.length} 幕`,
    meta: `字幕 ${(cueMs / 1000).toFixed(1)}s / 建议画面 ${(total / 1000).toFixed(1)}s` +
      `（画面总长要 ≥ 字幕总长，否则引擎拒绝正式渲染）`,
  });
  const out = $('#hdSuggestOut');
  if (out) {
    out.innerHTML = scenes.map(s => {
      const [a, b] = s.cueRange || [];
      const txt = cues.filter(c => c.index >= a && c.index <= b)
        .map(c => c.text).join(' ');
      return `<div class="hd-scene"><b>幕 ${s.sceneIndex}</b>
        <span class="hd-sc-meta">字幕 ${a}~${b} · ${(s.sceneDurationMs / 1000).toFixed(1)}s
        <br><span class="dim">${esc(txt.slice(0, 80))}${txt.length > 80 ? '…' : ''}</span></span></div>`;
    }).join('');
  }
  hdSyncChat();
  hdSetStep(3);
  hdRefreshStatus();
}

async function hdRegions() {
  if (!HD.project) { hdErr('还没有工程'); return; }
  const sid = $('#hdSceneId').value.trim();
  const d = await api('/api/handdraw/preview', {
    method: 'POST',
    body: JSON.stringify({ root: hdRoot(), project: HD.project, scene_id: sid }),
  });
  if (d.ok === false) { hdErr(d.err || '出检查图失败'); return; }
  const f = d.result || d.output;
  if (f) hdMedia(f, `④ 分区检查图 · ${sid}（核对编号顺序与区域边界）`, true);
}

/* ---- 3 标注 ---- */
function hdAnnTemplate() {
  const tpl = {
    sceneId: $('#hdSceneId').value.trim() || 'scene-01',
    canvas: { width: 1920, height: 1080 },
    storyBasis: '这一幕在讲什么',
    sceneDurationMs: 24000,
    elements: [
      {
        id: 'main', label: '主体', sequence: 1,
        narrativeRole: '场景铺垫：先画出主体',
        subtitle: '对应的字幕原文', type: 'structure',
        region: { x: 300, y: 300, width: 500, height: 400 },
        reveal: {
          direction: 'left_to_right', startMs: 300, durationMs: 2600,
          maskPaddingPx: 20, protectedRegions: [],
        },
        handPath: { start: [300, 500], end: [800, 500], easing: 'easeInOut' },
      },
      {
        id: 'sub', label: '第二主体', sequence: 2,
        narrativeRole: '关键变化：讲清结论',
        subtitle: '第二条字幕', type: 'structure',
        region: { x: 1000, y: 300, width: 500, height: 400 },
        reveal: {
          direction: 'left_to_right', startMs: 3100, durationMs: 2600,
          maskPaddingPx: 20, protectedRegions: [],
        },
        handPath: { start: [1000, 500], end: [1500, 500], easing: 'easeInOut' },
      },
    ],
  };
  $('#hdAnn').value = JSON.stringify(tpl, null, 2);
  toast('模板已载入（记得改 canvas 成源图真实像素）');
}

async function hdAnnotate() {
  if (!HD.project) { hdErr('还没有工程'); return; }
  if (!HD.upImage) { hdErr('先上传这一幕的线稿图'); return; }
  let ann;
  try { ann = JSON.parse($('#hdAnn').value); }
  catch (e) { hdErr('标注不是合法 JSON：' + e.message); return; }
  const sid = $('#hdSceneId').value.trim();
  hdSay('me', `③ 写标注 · ${sid}`, `${ann.elements ? ann.elements.length : 0} 个区域`);
  const d = await api('/api/handdraw/annotate', {
    method: 'POST',
    body: JSON.stringify({
      root: hdRoot(), project: HD.project, scene_id: sid,
      image: HD.upImage, annotation: ann,
    }),
  });
  if (d.ok === false) { hdErr(d.err || '写标注失败'); return; }
  hdNote(`标注已登记：${d.scene} · ${d.elements} 个区域 · 幕时长 ${(d.sceneDurationMs / 1000).toFixed(1)}s · 全片位置 ${(d.sourceStartMs / 1000).toFixed(1)}s`);
  $('#hdAnnMsg').textContent = '已登记';
  hdSetStep(4);
  hdRefreshStatus();
}

/* ---- 4 时间轴 ---- */
async function hdApprove() {
  if (!HD.project) { hdErr('还没有工程'); return; }
  const d = await api('/api/handdraw/approve', {
    method: 'POST',
    body: JSON.stringify({ root: hdRoot(), project: HD.project }),
  });
  if (d.ok === false) { hdErr(d.err || '批准失败'); return; }
  hdNote(`时间轴已批准（画面 ${(d.picture_total_ms / 1000).toFixed(1)}s ≥ 字幕 ${(d.cue_total_ms / 1000).toFixed(1)}s），现在可以正式渲染`);
  hdSetStep(5);
  hdRefreshStatus();
}

/* ---- 5 渲染 ---- */
async function hdRender(preview) {
  if (!HD.project) { hdErr('还没有工程'); return; }
  const sid = $('#hdSceneId').value.trim();
  hdSay('me', preview ? `⑤ 短预览 · ${sid}` : `⑤ 渲染这一幕 · ${sid}`);
  const d = await api('/api/handdraw/render', {
    method: 'POST',
    body: JSON.stringify({
      root: hdRoot(), project: HD.project, scene_id: sid,
      preview_ms: preview ? 3000 : 0,
    }),
  });
  if (d.ok === false) { hdErr(d.err || '渲染未启动'); return; }
  HD.msgs.push({
    run: true, text: preview ? `短预览渲染中 · ${sid}…` : `渲染中 · ${sid}…`,
    pct: 0, meta: d.eta_hint || '',
  });
  hdSyncChat();
}

/* ---- 6 合成 ---- */
async function hdAudition() {
  const text = $('#hdVoiceText').value.trim();
  if (!text) { hdErr('先填试听文案'); return; }
  if (!$('#hdAllowNet').checked) { hdErr('试听是联网合成（Edge TTS），请先勾「联网许可」'); return; }
  const d = await api('/api/handdraw/voice', {
    method: 'POST',
    body: JSON.stringify({
      root: hdRoot(), text, allow_network: true,
      project: HD.project || '',
    }),
  });
  if (d.ok === false) { hdErr(d.err || '试听失败'); return; }
  hdKV('音色试听', Object.entries(d).filter(([k]) => k !== 'ok')
    .map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v).slice(0, 120)]));
}

async function hdMix() {
  if (!HD.project) { hdErr('还没有工程'); return; }
  if (!$('#hdAllowNet').checked) { hdErr('配音是联网合成，请先勾「联网许可」'); return; }
  hdSay('me', '⑥ 配音合成', '成片落项目目录/生成');
  const d = await api('/api/handdraw/mix', {
    method: 'POST',
    body: JSON.stringify({
      root: hdRoot(), project: HD.project, index: 'none',
    }),
  });
  if (d.ok === false) { hdErr(d.err || '合成未启动'); return; }
  HD.msgs.push({
    run: true, text: '配音合成中…', pct: 0, meta: d.eta_hint || '',
  });
  hdSyncChat();
}

async function hdQa() {
  if (!HD.project) { hdErr('还没有工程'); return; }
  const d = await api('/api/handdraw/status?root=' + encodeURIComponent(hdRoot()) +
    '&project=' + encodeURIComponent(HD.project));
  if (d.ok === false) { hdErr(d.err || '质检失败'); return; }
  hdKV('质检', [
    ['时间轴状态', d.timelineStatus],
    ['幕数', Array.isArray(d.scenes) ? d.scenes.length : d.scenes],
    ['画面总长', (d.picture_total_ms / 1000).toFixed(1) + 's'],
    ['字幕总长', (d.cue_total_ms / 1000).toFixed(1) + 's'],
    ['结论', d.ok ? '时长足够' : ('画面短 ' + (d.shortfall_ms / 1000).toFixed(1) + 's')],
  ]);
}

/* ---- 事件接线 ---- */
function hdWire() {
  const on = (sel, fn) => { const el = $(sel); if (el) el.onclick = fn; };
  on('#hdUpScript', () => hdUpload('script'));
  on('#hdUpImage', () => hdUpload('image'));
  on('#hdInit', hdInit);
  on('#hdSuggest', hdSuggest);
  on('#hdRegions', hdRegions);
  on('#hdTplAnn', hdAnnTemplate);
  on('#hdAnnotate', hdAnnotate);
  on('#hdApprove', hdApprove);
  on('#hdPreview', () => hdRender(true));
  on('#hdRender', () => hdRender(false));
  on('#hdAudition', hdAudition);
  on('#hdMix', hdMix);
  on('#hdQa', hdQa);
  on('#hdClear', hdClear);
}

HD.msgs = [];
hdWire();
hdLoadInfo();
hdLoadLibs();
