/* 拾帧 FrameSeek · 白板手绘模块（极简版：内容框 + 风格选择 → 一键生成） */
(function () {
'use strict';
const { reactive, ref, computed, onMounted, onUnmounted } = Vue;
const FS = window.FS;
const { S, BUS, api, post, toast, hdUrl, esc } = FS;

/* 讲解风格（skill）：决定每幕目标时长。白板讲解本就是空纸逐笔写，
   所以风格只调节奏，不碰线稿/标注这些被砍掉的中间步骤。 */
const SKILLS = [
  { label: '快讲 · 碎片知识点', target_sec: 15 },
  { label: '标准 · 知识点讲解', target_sec: 30 },
  { label: '系统课 · 深度讲解', target_sec: 45 },
  { label: '演讲 · 长内容梳理', target_sec: 60 },
];

const HanddrawView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div><h1>白板手绘</h1>
      <p>把要讲的内容贴进来，一键生成手绘讲解视频。本地逐笔绘制，不占显卡。</p></div>
    <div class="fs-tools">
      <el-tag v-if="hd.ready" type="success" size="small">引擎就绪 · {{ hd.engine }}</el-tag>
      <el-tag v-else type="danger" size="small">未就绪：{{ (hd.missing || []).join('；') }}</el-tag>
    </div>
  </div>

  <el-divider />

  <el-card shadow="never" class="fs-card fs-hd-simple">
    <el-form label-position="top" class="fs-form" @submit.prevent>
      <el-row :gutter="14">
        <el-col :xs="24" :sm="14" :md="10">
          <el-form-item label="讲解风格">
            <el-select id="hdSkill" v-model="skill" size="small"
              title="决定每幕目标时长：碎片知识短、系统课长">
              <el-option v-for="o in skills" :key="o.target_sec" :label="o.label" :value="o.target_sec"/>
            </el-select>
          </el-form-item>
        </el-col>
        <!-- 说明文字列：与上方控件底边对齐（原来用空格 label 占位，label 是真实节点，
             高度跟旁边几列不一致，整行看着是歪的） -->
        <el-col :xs="24" :sm="10" :md="14" class="fs-col-note">
          <div class="fs-colnote">每幕约 {{ skill }} 秒</div>
        </el-col>
      </el-row>

      <el-form-item label="讲解内容">
        <el-input id="hdText" v-model="text" type="textarea" :rows="11"
          placeholder="把要讲解的文稿贴进来（可多段，自动按句切分幕）。例如：光电效应是指光照到金属表面时，电子吸收光子能量逸出金属的现象……"/>
      </el-form-item>

      <el-row :gutter="14" class="fs-form-row">
        <el-col :xs="24" :sm="12" :md="8">
          <el-form-item label="工程名（可选）">
            <el-input id="hdName" v-model="pname" size="small" placeholder="自动取首句"/>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="16" class="fs-form-actions">
          <el-form-item label=" ">
            <el-button id="hdGen" type="primary" size="small" :disabled="busy || !canGo" @click="generate"
              title="一键：建工程 → 分幕 → 渲染各幕 → 配音合成，省掉手动步骤">生成手绘视频</el-button>
            <el-button id="hdClear" size="small" :disabled="busy" @click="clearChat">清空</el-button>
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>

    <div class="fs-hint" id="hdHint">
      生成会自动联网配音（Edge TTS 女声），无需逐幕操作。
      成片落项目「AI生成」目录，可在「素材 → AI生成素材库」查看。
    </div>
  </el-card>

  <div class="fs-chat fs-hdchat" id="hdChat">
    <div v-if="!chats.length" class="fs-chat-empty" id="hdChatEmpty">
      操作记录会出现在这里 —— 生成进度与成片（视频）实时显示
    </div>
    <div v-for="(m, i) in chats" :key="i" class="fs-bub" :class="m.who">
      <div v-if="m.run">
        <div class="bub-txt">{{ m.text }}</div>
        <el-progress :percentage="m.pct || 0" :stroke-width="6"/>
        <div class="bub-meta">{{ m.meta || (m.pct + '%') }}</div>
      </div>
      <div v-else-if="m.kind === 'media'" class="bub-media">
        <div class="bub-txt">{{ m.text }}</div>
        <img v-if="m.image" :src="hdUrl(m.file)" alt="检查图"/>
        <video v-else :src="hdUrl(m.file)" controls playsinline></video>
        <div>
          <el-button link size="small" tag="a" :href="hdUrl(m.file)" target="_blank">打开原文件</el-button>
          <span class="dim">{{ (m.file || '').split(/[\\\\/]/).pop() }}</span>
        </div>
      </div>
      <div v-else :class="m.kind === 'err' ? 'bub-err' : 'bub-txt'">{{ m.text }}</div>
      <div v-if="m.meta && !m.run" class="bub-meta">{{ m.meta }}</div>
    </div>
  </div>
</div>
`,
  setup() {
    const hd = reactive({ ready: false, missing: [], engine: '' });
    const text = ref('');
    const pname = ref('');
    const skill = ref(30);
    const skills = SKILLS;
    const busy = ref(false);
    const chats = ref([]);

    /* 能否生成：引擎就绪 + 至少登记过一个素材库（成片要落库）。
       busy 单独由 :disabled="busy || !canGo" 控制，这里只算前置条件。 */
    const canGo = computed(() => hd.ready && (S.libs || []).length > 0);

    function say(who, t, meta) { chats.value.push({ kind: 'text', who, text: t, meta }); }
    function err(t) { chats.value.push({ kind: 'err', who: 'sys', text: t }); }
    function media(file, t, isImage) {
      chats.value.push({ kind: 'media', who: 'sys', file, text: t, image: !!isImage });
    }
    function clearChat() { chats.value = []; }

    async function loadInfo() {
      try {
        const d = await api('/api/handdraw/info');
        hd.ready = !!d.ready;
        hd.missing = d.missing || [];
        hd.engine = d.engine || '';
      } catch (e) {
        hd.ready = false;
        hd.missing = [String((e && e.message) || e)];
      }
    }

    const hdRoot = () => S.activeRoot || (S.libs[0] && S.libs[0].path) || '';

    async function generate() {
      const root = hdRoot();
      if (!root) { err('先去「素材库」选一个素材库再生成'); return; }
      if (!text.value.trim()) { err('先写要讲解的内容'); return; }
      if (busy.value) { err('已经有一个手绘任务在跑了'); return; }
      busy.value = true;
      const sk = skills.find(s => s.target_sec === +skill.value);
      say('me', '一键生成手绘视频', (sk && sk.label) || ('每幕 ' + skill.value + 's'));
      const d = await post('/api/handdraw/auto', {
        root, text: text.value, name: pname.value.trim(),
        title: '', target_sec: +skill.value || 30,
      });
      if (d.ok === false) {
        busy.value = false;
        err(d.err || '启动失败');
        return;
      }
      chats.value.push({ run: true, who: 'sys', text: '手绘任务进行中…', pct: 0, meta: d.eta_hint || '' });
    }

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
      busy.value = false;
      if (s.error) { err(s.error); return; }
      const f = s.file || s.library_file || '';
      if (f) media(f, `完成：${s.name || ''}（已落项目「AI生成」目录）`, false);
      else say('sys', '手绘任务完成：' + (s.name || ''));
    });
    onUnmounted(() => { offTick(); offDone(); });

    onMounted(loadInfo);

    // 回归脚本的驱动口子（保留，避免外部引用报 undefined）
    window.__FS.hd = hd;
    window.__FS.hdApi = { generate, loadInfo };

    return {
      hd, text, pname, skill, skills, busy, canGo, chats,
      generate, clearChat, hdUrl,
    };
  },
};

FS.HanddrawView = HanddrawView;
})();
