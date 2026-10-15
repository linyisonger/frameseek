/* 拾帧 FrameSeek · 方框复查模块（台词帧 / 台词框 / 文字 逐句核对） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, BUS, api, post, toast, play, esc } = FS;

const BoxView = {
  template: `
<div class="fs-page fs-page-box">
  <div class="fs-page-hd">
    <div><h1>方框复查 <span class="dim" v-if="rep">{{ curFile }} · {{ rep.total }} 条台词</span></h1>
      <p>台词帧 · 台词框 · 文字 —— 把框画回画面上，逐句核对圈得对不对</p></div>
    <div class="fs-tools">
      <el-select id="bxLibPick" v-model="libKey" size="small" class="w160" clearable
        placeholder="全部库" title="只看某个素材库的片子。默认「全部库」= 列出所有库里已索引、且有台词的素材">
        <el-option v-for="o in libOpts" :key="o.value" :label="o.label" :value="o.value"/>
      </el-select>
      <el-input id="bxSearch" v-model="vq" size="small" class="w160" placeholder="搜索片名/目录…" clearable
        title="按片名或所在目录过滤右边的片单，边打边生效。多个词用空格分隔，要全部命中"/>
      <el-select id="bxPick" v-model="pick" size="small" class="w260" filterable
        placeholder="选一部片子" title="选一部片子：会列出它全部台词，逐句核对画面方框圈得对不对">
        <el-option v-for="o in pickOpts" :key="o.value" :label="o.label" :value="o.value"/>
      </el-select>
      <span class="dim" id="bxHit">{{ hitText }}</span>
      <el-button id="bxReload" size="small" @click="reload"
        title="重新拉这部片子的台词清单和校验结论。刚改过字幕或补过框就点它">刷新</el-button>
    </div>
  </div>

  <div class="fs-bxsum" id="bxSum" v-if="rep">
    <div class="bxs"><b>{{ rep.total }}</b><span>台词</span></div>
    <div class="bxs"><b class="ok">{{ rep.passed }}</b><span>方框已验</span></div>
    <div class="bxs"><b :class="rep.failed ? 'bad' : 'dim'">{{ rep.failed }}</b><span>可疑</span></div>
    <div class="bxs"><b :class="rep.no_box ? 'warn' : 'dim'">{{ rep.no_box }}</b><span>无框凭证</span></div>
    <div class="bxs"><b>{{ rep.checked ? Math.round(rep.rate * 100) + '%' : '—' }}</b><span>通过率</span></div>
    <div class="bxreasons">
      <el-tag v-for="(n, k) in (rep.reasons || {})" :key="k" size="small" type="danger">{{ k }} {{ n }}</el-tag>
      <span v-if="!rep.failed" class="dim">没有可疑项</span>
    </div>
  </div>

  <div class="fs-bxwrap">
    <div class="fs-bxlist">
      <div class="fs-bxbar">
        <el-input id="bxFilter" v-model="lq" size="small" placeholder="过滤台词…" clearable
          title="按台词文字过滤清单，边打边生效"/>
        <el-radio-group id="bxSegs" v-model="filter" size="small">
          <el-radio-button value="all" title="显示全部台词">全部</el-radio-button>
          <el-radio-button value="bad"
            title="只看可疑：方框对不上，或字体偏离度跟本部片主流字幕不一致的">可疑</el-radio-button>
          <el-radio-button value="nobox"
            title="只看没有画面方框凭证的。语音转写和外挂字幕来源本来就没有框，属正常">无框</el-radio-button>
        </el-radio-group>
      </div>
      <div class="fs-bxrows" id="bxRows">
        <div v-for="x in visible" :key="x.id" class="fs-bxrow" :class="{ on: cur === x.id }"
             @click="select(x.id)">
          <div class="bxts">{{ x.start_str }}<span class="dim2">{{ (x.end_str || '').slice(0, 8) }}</span></div>
          <div class="bxtx">{{ x.text }}</div>
          <div class="bxmark" :class="cls(x)">{{ mark(x) }}</div>
        </div>
        <div v-if="!visible.length" class="dim pad">
          {{ pick ? '没有符合条件的台词' : (raw.length ? '没有匹配的片子' : '还没有已索引的素材') }}</div>
      </div>
    </div>

    <div class="fs-bxview" id="bxView">
      <div v-if="!curRow" class="dim pad">
        {{ pick ? '左边点一条台词'
                : (raw.length ? '没有匹配的片子' : '先去「素材库」索引出台词') }}</div>
      <template v-else>
        <div class="fs-bxhead">
          <div class="bxhts">{{ curRow.start_str }} → {{ curRow.end_str }}
            <span class="dim2">{{ curRow.src || '未知来源' }}</span></div>
          <div class="bxtools">
            <el-checkbox id="bxLiveWrap" v-show="probeOut" v-model="liveBox" size="small" label="叠识别框"
              title="把这次识别到的框用绿虚线叠在蓝框上。两个框分开就说明这条的框不稳"/>
            <el-button id="bxPrev" size="small" @click="step(-1)" title="上一句台词（快捷键 ↑）">← 上一句</el-button>
            <el-button id="bxNext" size="small" @click="step(1)" title="下一句台词（快捷键 ↓）">下一句 →</el-button>
          </div>
        </div>

        <div class="fs-bximg">
          <div v-if="imgLoading" class="spin dim">正在抽帧…</div>
          <img id="bxImg" :src="frameUrl" @load="imgLoading = false" @error="onImgErr" alt="台词帧"/>
          <div class="bxlegend">
            <span><i style="background:#4d9dff"></i>库里的框</span>
            <span><i class="dash" style="background:#2ec27e"></i>这次识别到的框</span>
          </div>
        </div>

        <div class="fs-bxtext">
          <div class="bxfld"><span>库里文字</span><b id="bxSavedTxt">{{ curRow.text }}</b></div>
          <div class="bxfld"><span>重新识别结果</span>
            <b id="bxLiveTxt" :class="liveCls">{{ liveTxt || '还没识别' }}</b></div>
        </div>

        <div class="fs-bxfoot">
          <div class="bxverdict" :class="cls(curRow)">
            <b>{{ mark(curRow) }}</b>
            <span v-if="(curRow.reasons || []).length" class="dim">{{ curRow.reasons.join('、') }}</span>
            <span v-if="curRow.skipped" class="dim">{{ curRow.src }} 来源本来就没有画面框</span>
          </div>
          <div class="bxacts">
            <el-button id="bxProbe" size="small" :loading="busy" @click="doProbe"
              title="回画面重抽首/中/尾三帧重跑 OCR，逐帧比对框和文字对不对得上。只出报告，不改库">重新 OCR 识别</el-button>
            <el-button id="bxAsr" size="small" :loading="busy" @click="doAsr"
              title="只听这一句前后各 0.6 秒的音频，把听到的话显示出来。不自动覆盖库里的台词，看过后自己点「编辑台词」决定改不改">语音识别这句</el-button>
            <el-button id="bxEdit" size="small" type="primary" @click="openEdit"
              title="改这句话的文字和起止时间，改完立刻生效并同步检索索引；可勾选顺带把识别到的画面方框写进库">编辑台词</el-button>
            <el-button id="bxPlay" size="small" @click="play"
              title="从这句台词的时间点开始播放原片">播放这句</el-button>
          </div>
        </div>

        <div class="fs-bxprobe" id="bxProbeOut" v-html="probeOut"></div>
        <table v-if="metricsRows.length" class="bxmtbl">
          <tr v-for="(r, i) in metricsRows" :key="i">
            <td>{{ r[0] }}</td><td :class="r[2]">{{ r[1] }}</td>
          </tr>
        </table>
      </template>
    </div>
  </div>

  <!-- 编辑台词 -->
  <el-dialog v-model="editDlg" title="编辑台词" width="560px">
    <div class="fs-hint">{{ curRow ? curRow.start_str + ' → ' + curRow.end_str : '' }}
      · {{ curRow ? (curRow.src || '未知来源') : '' }} 来源</div>
    <el-input id="bxEText" v-model="editForm.text" type="textarea" :rows="3" placeholder="这句话的文本"/>
    <div class="fs-mgrid">
      <span>开始（秒）<el-input-number id="bxEStart" v-model="editForm.start" :step="0.01" size="small" controls-position="right"/></span>
      <span>结束（秒）<el-input-number id="bxEEnd" v-model="editForm.end" :step="0.01" size="small" controls-position="right"/></span>
    </div>
    <el-checkbox id="bxESaveBox" v-model="editForm.save_box" label="同时把这次识别到的画面方框写进库"/>
    <div class="fs-hint">勾上后会重新抽 3 帧跑 OCR，三帧文字都对得上才写入；不一致会直接告诉你，什么都不会改。</div>
    <el-button v-if="liveTxt" size="small" @click="editForm.text = liveTxt"
      :title="'把识别到的这句话直接填进上面的文本框，再手动修一修'">用「{{ liveTxt.slice(0, 14) }}{{ liveTxt.length > 14 ? '…' : '' }}」填入</el-button>
    <template #footer>
      <el-button id="mCancel" @click="editDlg = false">取消</el-button>
      <el-button id="mYes" type="primary" @click="saveEdit">保存</el-button>
    </template>
  </el-dialog>
</div>
`,
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

    /* 二级目录：片名前面带目录（亮剑/01.mp4）。否则二级目录一多，
       片单里全是 01.mp4 / 02.mp4，根本分不清是哪一部。
       目录 = 相对路径去掉文件名（兼容 / 和 \），跟素材页 dirOf 同一套口径。 */
    function dirOf(v) {
      const rel = (v && v.rel) || '';
      const i = Math.max(rel.lastIndexOf('/'), rel.lastIndexOf('\\'));
      return i > 0 ? rel.slice(0, i).replace(/[\\/]+$/, '') : '';
    }
    function shownName(v) {
      const d = dirOf(v);
      return d ? `${d}/${(v && v.name) || ''}` : ((v && v.name) || '');
    }
    const curFile = computed(() => {
      const v = (rep.value || {}).video;
      return v ? shownName(v) : '';
    });

    /* 两级筛选：先按素材库，再按片名/目录关键字（多词要全部命中）。
       全在本地做——接口只拉一次全量概览（一条 SQL 聚合，秒回）。 */
    const videoList = computed(() => {
      const terms = (vq.value || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
      return raw.value.filter(x => {
        if (libKey.value && x.root !== libKey.value) return false;
        if (!terms.length) return true;
        const nm = String(x.name || '').toLowerCase();
        const dr = dirOf(x).toLowerCase();
        // 目录和片名**分别**匹配，而不是拼成一个字符串再 includes：
        // 拼起来会让「亮剑01」这种跨界的子串搜不到（中间隔着 /），
        // 也会把原本按片名匹配的结果集改掉。
        return terms.every(t => nm.includes(t) || dr.includes(t));
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
      return videoList.value.map(x => {
        const d = dirOf(x);
        return {
          value: `${x.root}|${x.id}`,
          label: `${d ? d + '/' : ''}${x.name}`
               + `${showLib ? ' · ' + (x.lib || '') : ''}`
               + `${x.no_box ? ` · 无框 ${x.no_box}/${x.lines}` : ''}`,
        };
      });
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
      // 二级目录相关：片单标签列表 + 单条取目录，给回归断言用
      get labels() { return pickOpts.value.map(o => o.label); },
      get curFile() { return curFile.value; },
      dirOf(v) { return dirOf(v || {}); },
      get nestedCount() { return raw.value.filter(x => dirOf(x)).length; },
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
      curFile, dirOf, shownName,
      select, step, cls, mark, doProbe, doAsr, openEdit, saveEdit,
      onImgErr, reload,
      play: () => {
        const x = curRow.value;
        if (x) play(vid_.value, x.start, x.text || '', root_.value);
      },
    };
  },
};

FS.BoxView = BoxView;
})();
