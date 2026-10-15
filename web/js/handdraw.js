/* 拾帧 FrameSeek · 白板手绘模块（文稿 → 分幕 → 线稿标注 → 渲染 → 配音） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, BUS, api, post, toast, hdUrl } = FS;

const HanddrawView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div><h1>白板手绘</h1>
      <p>文稿 → 分幕 → 线稿 → 区域标注 → 逐笔渲染 → 配音合成（本地 CPU，不占显卡）</p></div>
    <div class="fs-tools">
      <el-tag v-if="hd.ready" type="success" size="small">引擎就绪 · {{ hd.engine }} · 音色 {{ hd.voice }}</el-tag>
      <el-tag v-else type="danger" size="small">未就绪：{{ (hd.missing || []).join('；') }}</el-tag>
    </div>
  </div>

  <el-steps :active="step - 1" finish-status="success" align-center class="fs-steps">
    <el-step title="建工程" description="文稿 / 字幕"/>
    <el-step title="分幕建议" description="按 25~35 秒切"/>
    <el-step title="线稿+标注" description="逐幕标注区域"/>
    <el-step title="批准时间轴" description="画面≥字幕"/>
    <el-step title="逐幕渲染" description="逐笔动画"/>
    <el-step title="配音合成" description="女声 + 伴奏"/>
  </el-steps>

  <el-card shadow="never" class="fs-card fs-hdproj" id="hdProj">
    <span v-if="!hd.project" class="dim">
      当前没有手绘工程。第 1 步：上传一个 <b>.txt 文稿</b>或 <b>.srt 字幕</b>开始。
    </span>
    <span v-else v-html="projText"></span>
    <div class="fs-hd-dirs">
      <span class="dim" id="hdRootDir" title="手绘工程目录">工程 → {{ hd.base || '项目目录/手绘工程' }}</span>
      <span class="dim" id="hdOutDir" title="手绘工程与成片都在 FrameSeek 项目目录下，不进素材库">成片 → {{ hd.outDir || '项目目录/AI生成' }}</span>
    </div>
  </el-card>

  <el-card shadow="never" class="fs-card">
    <template #header><b>1 · 建工程</b>
      <span class="dim">文稿按句拆字幕并估时间轴；已有字幕直接用</span></template>
    <div class="fs-hd-row">
      <el-button id="hdUpScript" size="small" @click="upload('script')"
        title="上传 .txt/.md 文稿或 .srt 字幕。有字幕就直接用它的时间轴，没有才按句拆文稿并估算时间">上传文稿/字幕</el-button>
      <span class="dim" id="hdUpName">{{ hd.upScriptName || '未选择' }}</span>
    </div>
    <div class="fs-hd-row">
      <el-input id="hdProjName" v-model="pname" size="small" class="w200" placeholder="工程名（如 光电效应）"/>
      <el-input id="hdTitle" v-model="ptitle" size="small" class="w200" placeholder="片名（可选）"/>
      <el-button id="hdInit" type="primary" size="small" :disabled="!hd.ready || !hd.upScript" @click="init"
        title="按工程名建工程：把文稿拆成字幕并估时间轴。工程落在项目「手绘工程」目录下，不进素材库">建工程</el-button>
    </div>
  </el-card>

  <el-card shadow="never" class="fs-card">
    <template #header><b>2 · 分幕建议</b><span class="dim">读工程字幕，按目标秒数切幕</span></template>
    <div class="fs-hd-row">
      <span class="dim">每幕目标秒</span>
      <el-input-number id="hdTargetSec" v-model="targetSec" size="small" :min="10" :max="120"
                       controls-position="right"/>
      <el-button id="hdSuggest" size="small" @click="suggest"
        title="读工程里的字幕，按左边填的每幕目标秒数切幕，给出分幕建议（还没渲染画面）">出分幕建议</el-button>
      <el-button id="hdRegions" size="small" @click="regions"
        title="渲染一张分区检查图，看这一幕的画面区域划分对不对。只出图不渲染成片，很快">渲染分区检查图</el-button>
    </div>
    <div class="fs-scenes" id="hdSuggestOut">
      <div v-for="s in scenes" :key="s.sceneIndex" class="fs-scene">
        <b>幕 {{ s.sceneIndex }}</b>
        <span class="dim">字幕 {{ (s.cueRange || [])[0] }}~{{ (s.cueRange || [])[1] }} ·
          {{ (s.sceneDurationMs / 1000).toFixed(1) }}s<br>{{ sceneText(s) }}</span>
      </div>
    </div>
  </el-card>

  <el-card shadow="never" class="fs-card">
    <template #header><b>3 · 线稿 + 区域标注</b>
      <span class="dim">每幕一张暖米黄纸底线稿 + 区域标注 JSON（决定谁先画、画多久）</span></template>
    <div class="fs-hd-row">
      <el-input id="hdSceneId" v-model="sceneId" size="small" class="w140" placeholder="scene-01"/>
      <el-button id="hdUpImage" size="small" @click="upload('image')"
        title="上传这一幕的线稿图（暖米黄纸底）。不传就用引擎默认的空白纸底">上传线稿图</el-button>
      <span class="dim" id="hdImgName">{{ hd.upImageName || '未选择' }}</span>
    </div>
    <div class="fs-hd-row">
      <span class="dim">标注 JSON</span>
      <el-button id="hdTplAnn" size="small" @click="tplAnn"
        title="往下面的标注框里填一份模板 JSON，照着改比从零写快">载入模板</el-button>
      <span class="dim">canvas 必须等于源图像素，sequence 按字幕叙事顺序</span>
    </div>
    <el-input id="hdAnn" v-model="ann" type="textarea" :rows="8"
      placeholder='{"sceneId":"scene-01","canvas":{"width":1920,"height":1080},"sceneDurationMs":24000,"elements":[...]}'/>
    <div class="fs-hd-row">
      <el-button id="hdAnnotate" type="primary" size="small" @click="annotate"
        title="把标注 JSON 写进工程。它决定这一幕谁先画、画多久；canvas 必须等于源图像素">写标注</el-button>
      <span class="dim" id="hdAnnMsg">{{ annMsg }}</span>
    </div>
  </el-card>

  <el-card shadow="never" class="fs-card">
    <template #header><b>4~6 · 时间轴 · 渲染 · 合成</b>
      <span class="dim">画面总时长必须 ≥ 字幕总时长，否则引擎拒绝正式渲染</span></template>
    <div class="fs-hd-row" id="hdTlRow">
      <span class="dim">时间轴状态：<b id="hdTlStatus">{{ tlStatus }}</b></span>
      <span class="dim" id="hdTlAdvice">{{ tlAdvice }}</span>
      <el-button id="hdApprove" size="small" @click="approve"
        title="批准时间轴。画面总时长必须 ≥ 字幕总时长，短了会被引擎拒绝正式渲染">批准时间轴</el-button>
      <el-button id="hdPreview" size="small" @click="render(true)"
        title="只渲染前 3 秒。想快速看笔迹和构图对不对先用这个，别一上来就整幕渲染">短预览 3s</el-button>
      <el-button id="hdRender" type="primary" size="small" :disabled="tlStatus !== 'approved'" @click="render(false)"
        title="渲染这一幕的完整画面。本地 CPU 逐笔绘制，不占显卡，一幕通常几分钟">渲染这一幕</el-button>
    </div>
    <div class="fs-hd-row">
      <el-input id="hdVoiceText" v-model="voiceText" size="small" class="w260"
                placeholder="试听文案（10~20 秒，含数字和问句）"/>
      <el-button id="hdAudition" size="small" @click="audition"
        title="用左边这段文案试听音色。只出一段试听音频，不写进工程">试听音色</el-button>
      <el-checkbox id="hdAllowNet" v-model="allowNet"
        title="配音要把文本送到云端 TTS。不勾选就无法合成配音">联网许可</el-checkbox>
      <el-button id="hdMix" type="primary" size="small" @click="mix"
        title="把配音和已渲染的画面合成为成片，落到项目「AI生成」目录，不进素材库">配音合成成片</el-button>
    </div>
    <div class="fs-hd-row">
      <el-button id="hdQa" size="small" @click="qa"
        title="对成片跑质检：时长够不够、音画对不对齐、字幕有没有被切掉">质检</el-button>
      <el-button id="hdClear" size="small" @click="clearChat"
        title="清空当前会话气泡和临时选择。已经落盘的工程和成片不受影响">清空会话</el-button>
    </div>
  </el-card>

  <div class="fs-chat fs-hdchat" id="hdChat">
    <div v-if="!chats.length" class="fs-chat-empty" id="hdChatEmpty">
      操作记录会出现在这里 —— 每步的实际产出（图片 / mp4 / 报错）
    </div>
    <div v-for="(m, i) in chats" :key="i" class="fs-bub" :class="m.who">
      <div v-if="m.run" class="bub-run">
        <div class="bub-txt">{{ m.text }}</div>
        <el-progress :percentage="m.pct || 0" :stroke-width="6"/>
        <div class="bub-meta">{{ m.meta || (m.pct + '%') }}</div>
      </div>
      <div v-else-if="m.kind === 'media'" class="bub-media">
        <div class="bub-txt">{{ m.text }}</div>
        <img v-if="m.image" :src="hdUrl(m.file)" alt="检查图"/>
        <video v-else :src="hdUrl(m.file)" controls playsinline></video>
        <div class="hd-acts">
          <el-button link size="small" tag="a" :href="hdUrl(m.file)" target="_blank">打开原文件</el-button>
          <span class="dim">{{ (m.file || '').split(/[\\\\/]/).pop() }}</span>
        </div>
      </div>
      <div v-else-if="m.kind === 'kv'" class="bub-kv">
        <div class="bub-txt">{{ m.text }}</div>
        <div class="hd-kv"><b v-for="(r, j) in m.rows" :key="j">{{ r[0] }}<span>{{ r[1] }}</span></b></div>
      </div>
      <div v-else :class="m.kind === 'err' ? 'bub-err' : 'bub-txt'">{{ m.text }}</div>
      <div v-if="m.meta && !m.run" class="bub-meta">{{ m.meta }}</div>
    </div>
  </div>
</div>
`,
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

FS.HanddrawView = HanddrawView;
})();
