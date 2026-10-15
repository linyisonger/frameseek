/* 拾帧 FrameSeek · AI 生成模块（对话式出片，MiniMax H3 / ComfyUI） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, BUS, api, post, toast, startPoll, mediaUrl } = FS;

const GenView = {
  template: `
<div class="fs-page fs-page-gen">
  <div class="fs-page-hd">
    <div><h1>AI 生成</h1><p>对话式出片 · 本地 MiniMax H3 · 成片落在项目「AI生成」目录</p></div>
    <div class="fs-tools">
      <el-tag v-if="gen.ready" type="success" size="small">H3 引擎就绪 · 直调 ComfyUI 库</el-tag>
      <el-tag v-else type="danger" size="small">引擎未就绪：{{ (gen.missing || []).join('、') }}</el-tag>
    </div>
  </div>

  <div class="fs-chat" id="chatLog">
    <div v-if="!msgs.length" class="fs-chat-empty" id="chatEmpty">
      说一句话就能出片：描述画面、镜头、光线（英文效果最好）。<br>回车发送 · 成片落在项目「AI生成」目录
    </div>
    <div v-for="(m, i) in msgs" :key="i" class="fs-bub" :class="m.role">
      <template v-if="m.role === 'me'">
        <div class="bub-txt">{{ m.prompt }}</div>
        <div class="bub-meta">{{ [m.dur + ' 秒', (m.aspect || '').split(' ')[0], m.mp + 'MP',
              m.ref ? '带参考图' : ''].filter(Boolean).join(' · ') }}</div>
      </template>
      <template v-else-if="m.status === 'error'">
        <div class="bub-err">{{ m.msg || '生成失败' }}</div>
      </template>
      <template v-else-if="m.status === 'done'">
        <video class="bub-video" :src="mediaUrl(m.file, m.root)" controls preload="metadata" playsinline></video>
        <div class="bub-meta">{{ m.name }}<template v-if="m.w"> · {{ m.w }}×{{ m.h }}</template>
          <template v-if="m.seconds"> · {{ m.seconds }}s</template></div>
        <div class="bub-acts">
          <el-button link size="small" @click="goAssets">去素材页看</el-button>
          <el-button link size="small" @click="clearChat">清空会话</el-button>
        </div>
      </template>
      <template v-else>
        <el-progress :percentage="m.pct || 0" :stroke-width="6"/>
        <div class="bub-meta">{{ m.msg || '排队中' }}<template v-if="m.pct"> · {{ m.pct }}%</template></div>
      </template>
    </div>
  </div>

  <div class="fs-composer">
    <div class="fs-composer-main">
      <el-input id="genPrompt" v-model="prompt" type="textarea" :rows="2" resize="none"
        placeholder="描述你想要的画面…（Enter 发送，Shift+Enter 换行）"
        @keydown.enter.exact.prevent="submit"/>
      <el-button id="btnGen" type="primary" :disabled="!gen.ready || !prompt.trim()" @click="submit"
        title="按提示词生成一段视频（英文描述效果最好）。成片落在项目「AI生成」目录，不进素材库">发送</el-button>
    </div>
    <div class="fs-composer-row">
      <el-button id="btnGenRef" size="small" @click="pickRef"
        title="参考图：上传后用 &lt;Picture 1&gt; 在提示词里引用，锁形象/风格">参考图</el-button>
      <input type="file" ref="refFile" accept="image/*" hidden @change="onRef"/>
      <span v-if="gen.ref" class="fs-refname clr" @click="dropRef"
        title="点此移除参考图">已选 {{ gen.refName }} · prompt 里用 &lt;Picture 1&gt; 引用（点此移除）</span>
      <el-select id="genDur" v-model="dur" size="small" class="w100" title="时长（5~15 秒）">
        <el-option label="5 秒" :value="5"/><el-option label="8 秒" :value="8"/>
        <el-option label="12 秒" :value="12"/><el-option label="15 秒（上限）" :value="15"/>
      </el-select>
      <el-select id="genAspect" v-model="aspect" size="small" class="w120" title="宽高比">
        <el-option v-for="a in gen.aspects" :key="a" :label="a" :value="a"/>
      </el-select>
      <el-select id="genMp" v-model="mp" size="small" class="w140" title="清晰度">
        <el-option label="标准 0.3MP（快）" :value="0.3"/>
        <el-option label="较好 0.5MP" :value="0.5"/>
        <el-option label="高清 1.0MP（慢）" :value="1"/>
      </el-select>
      <el-button id="genMore" link size="small" @click="adv = !adv"
        title="展开落点目录 / 输出文件名 / 随机种子等高级选项">{{ adv ? '收起 ▴' : '更多 ▾' }}</el-button>
    </div>
    <div v-show="adv" class="fs-composer-adv" id="genAdv">
      <span class="fs-label">落点</span>
      <span class="dim" id="genOutDir" title="生成产物不进素材库，单独放项目目录">{{ gen.outDir || '项目目录/AI生成' }}</span>
      <span class="fs-label">文件名</span>
      <el-input id="genName" v-model="fname" size="small" class="w200" placeholder="可空，默认按时间戳"/>
      <span class="fs-label">种子</span>
      <el-input-number id="genSeed" v-model="seed" size="small" :min="0" controls-position="right"
        title="0 = 随机；固定同一个数字可复现同一构图"/>
    </div>
    <div class="fs-composer-row">
      <span class="fs-label">试试</span>
      <el-button v-for="e in examples" :key="e.t" size="small" class="gen-ex" @click="prompt = e.p"
        :title="'点一下把这段英文示例提示词填进输入框，改改就能发'">{{ e.t }}</el-button>
    </div>
  </div>
</div>
`,
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

FS.GenView = GenView;
})();
