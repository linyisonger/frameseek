/* 拾帧 FrameSeek · AI 绘画模块（双后端生图：Agnes / SD1.5，两种本地方案：写实 / 卡通） */
(function () {
'use strict';
const { reactive, ref, computed, onMounted, onActivated, onUnmounted } = Vue;
const FS = window.FS;
const { S, BUS, api, post, toast, startPoll, projMedia } = FS;

// 尺寸预设（宽高都必须是 8 的倍数，256~1024——SD1.5 在 8G 卡的安全区）
const SIZES = [
  { label: '方形 512×512', w: 512, h: 512 },
  { label: '横向 768×512', w: 768, h: 512 },
  { label: '竖向 512×768', w: 512, h: 768 },
  { label: '宽屏 960×544', w: 960, h: 544 },
];

const AidrawView = {
  template: `
<div class="fs-page fs-page-aidraw">
  <div class="fs-page-hd">
    <div><h1>AI 绘画</h1>
      <p>双后端生图 · 两种本地方案：写实 / 卡通·插画 · 产物落在项目「AI生成/绘画」目录</p></div>
    <div class="fs-tools">
      <el-tag v-for="b in info.backends" :key="b.id" size="small"
        :type="b.ready ? (b.id === info.default ? 'success' : 'info') : 'danger'"
        :title="b.note + ' · ' + b.eta">{{ b.name }}<template v-if="!b.ready"> · 未就绪</template></el-tag>
      <el-button size="small" @click="loadOutputs" title="重新拉取产物列表"><el-icon><Refresh/></el-icon><span>刷新</span></el-button>
    </div>
  </div>

  <!-- 输入区（仿 AI 生成：放在底部） -->
  <div class="fs-composer">
    <div class="fs-composer-main">
      <el-input id="aidrawPrompt" v-model="prompt" type="textarea" :rows="2" resize="none"
        placeholder="描述你想要的画面…（Enter 提交，Shift+Enter 换行。中文也可，场景描述越具体越好）"
        @keydown.enter.exact.prevent="onEnter"/>
      <el-button id="btnAidraw" type="primary" :disabled="running || !prompt.trim()" @click="submit"
        title="按提示词生成一张图。默认 Agnes（API 秒出），也可选本地 SD1.5（写实/卡通）"><el-icon><MagicStick/></el-icon><span>生成</span></el-button>
    </div>
    <div class="fs-composer-row">
      <el-select id="aidrawScheme" v-model="scheme" size="small" class="w140" title="出图方案（仅本地 SD1.5 生效；Agnes 云后端忽略）">
        <el-option v-for="s in info.schemes" :key="s.id" :label="s.label" :value="s.id"/>
      </el-select>
      <el-select id="aidrawBackend" v-model="backend" size="small" class="w200" title="生图后端">
        <el-option v-for="b in info.backends" :key="b.id" :label="b.name" :value="b.id"
          :disabled="!b.ready">
          <span>{{ b.name }}</span>
          <span class="dim" style="float:right;margin-left:8px">{{ b.eta }}</span>
        </el-option>
      </el-select>
      <el-select id="aidrawSize" v-model="sizeIdx" size="small" class="w140" title="尺寸（SD1.5 原生；Agnes 按宽高比适配）">
        <el-option v-for="(s, i) in sizes" :key="i" :label="s.label" :value="i"/>
      </el-select>
      <el-button id="btnAidrawRef" size="small" @click="pickRef"
        title="参考图：SD1.5 用首张算 canny 锁构图；Agnes 最多 3 张"><el-icon><Picture/></el-icon><span>参考图</span></el-button>
      <input type="file" ref="refFile" accept="image/*" multiple hidden @change="onRef"/>
      <el-tag v-for="(r, i) in refs" :key="i" size="small" type="info" effect="plain" closable
        @close="dropRef(i)" :title="'移除参考图：' + r.name">{{ r.name }}</el-tag>
      <el-button id="aidrawMore" link size="small" @click="adv = !adv"
        title="展开负向词 / 采样步数 / LoRA / 控制强度 / 种子等高级选项"><el-icon><Setting/></el-icon><span>{{ adv ? '收起 ▴' : '更多 ▾' }}</span></el-button>
    </div>
    <el-form v-show="adv" label-position="top" class="fs-form fs-composer-adv" id="aidrawAdv" @submit.prevent>
      <el-row :gutter="14">
        <el-col :xs="24" :sm="12" :md="8">
          <el-form-item label="负向词（可选）">
            <el-input id="aidrawNeg" v-model="negative" size="small" placeholder="不想出现的元素（可空，不展开则随方案默认）"/>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="8">
          <el-form-item label="采样步数">
            <el-input-number id="aidrawSteps" v-model="steps" size="small" :min="8" :max="50" controls-position="right"
              title="采样步数：SD1.5 默认随方案（写实 25 / 卡通 20）"/>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="8">
          <el-form-item label="CFG 贴合度">
            <el-input-number id="aidrawCfg" v-model="cfg" size="small" :min="1" :max="15" :step="0.5" controls-position="right"
              title="提示词贴合度：默认随方案（写实 7.5 / 卡通 6）"/>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="8">
          <el-form-item label="LoRA">
            <el-select id="aidrawLora" v-model="lora" size="small" title="SD1.5 线稿 LoRA（models/loras/ 下）">
              <el-option label="随方案（写实空 / 卡通线稿）" value=""/>
              <el-option label="simple-lineart_v6（线稿）" value="simple-lineart_v6.safetensors"/>
              <el-option label="Detailed_LineArt（细线）" value="Detailed_LineArt.safetensors"/>
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="8">
          <el-form-item label="锁构图强度">
            <el-input-number id="aidrawCtrl" v-model="controlStrength" size="small" :min="0" :max="1" :step="0.05"
              controls-position="right" title="ControlNet 强度：0 = 不锁（无参考图时自动忽略）"/>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="8">
          <el-form-item label="随机种子">
            <el-input-number id="aidrawSeed" v-model="seed" size="small" :min="0" controls-position="right"
              title="0 = 随机；固定同一个数字可复现同一张图（Agnes 无种子，仅 SD1.5 有效）"/>
          </el-form-item>
        </el-col>
      </el-row>
      <div class="fs-hint dim">未展开「更多」时，LoRA / 负向词 / 步数 / CFG / 锁构图强度都会随所选方案取默认值；展开并改了才以你填的为准。</div>
    </el-form>
  </div>

  <!-- 生成进度（生成中显示在画廊上方） -->
  <div v-if="run" class="fs-aidraw-run">
    <el-progress :percentage="run.pct || 0" :stroke-width="6"/>
    <div class="bub-meta">{{ run.msg || '排队中' }}<template v-if="run.pct"> · {{ run.pct }}%</template></div>
  </div>

  <!-- 画廊：生成结果以卡片形式呈现，各属性用图标标注 -->
  <div class="fs-aidraw-grid" id="aidrawGrid">
    <div v-if="!items.length && !run" class="fs-aidraw-empty dim">
      还没有产物。写个提示词点「生成」——默认 Agnes 秒出；选本地 SD1.5 再挑「写实 / 卡通」方案。
    </div>
    <div v-for="it in items" :key="it.file" class="fs-acard" :title="it.name">
      <div class="fs-acard-img">
        <img :src="projMedia(it.file)" loading="lazy" @click="openZoom(it)" :title="'点此放大：' + it.name"/>
        <el-button class="fs-acard-del" size="small" circle type="danger" @click="del(it)"
          title="删除源文件（不可恢复）"><el-icon><Delete/></el-icon></el-button>
      </div>
      <div class="fs-acard-body">
        <div class="fs-acard-row">
          <el-icon><Picture/></el-icon>
          <span class="fs-scheme" :class="'fs-scheme-' + (it.meta.scheme||'')">{{ schemeLabel(it.meta.scheme) }}</span>
          <span class="dim">· {{ (it.meta.backend || '?').toUpperCase() }}</span>
        </div>
        <div class="fs-acard-row">
          <el-icon><ScaleToOriginal/></el-icon><span>{{ it.meta.w || '?' }}×{{ it.meta.h || '?' }}</span>
          <span class="fs-ic2"><el-icon><Sunny/></el-icon>seed {{ it.meta.seed }}</span>
        </div>
        <div class="fs-acard-row" v-if="it.meta.lora">
          <el-icon><Collection/></el-icon><span class="fs-lora">{{ it.meta.lora }}</span>
          <span class="fs-ic2"><el-icon><Histogram/></el-icon>{{ it.meta.cfg }} cfg</span>
        </div>
        <div class="fs-acard-row">
          <el-icon><Clock/></el-icon><span>{{ fmtAgo(it.mtime) }}</span>
          <span class="fs-ic2"><el-icon><Operation/></el-icon>{{ it.meta.steps || '?' }}步/{{ it.meta.sampler || '?' }}</span>
        </div>
        <div class="fs-acard-prompt">
          <div class="fs-acard-prompt-hd"><el-icon><Document/></el-icon><b>提示词</b></div>
          <p :title="it.meta.prompt">{{ it.meta.prompt }}</p>
        </div>
      </div>
      <div class="fs-acard-acts">
        <el-button link size="small" @click="openZoom(it)" title="放大查看"><el-icon><ZoomIn/></el-icon><span>放大</span></el-button>
        <el-button link size="small" :disabled="running" @click="reroll(it)" title="用同一组参数重掷（换新种子）"><el-icon><Refresh/></el-icon><span>重掷</span></el-button>
        <el-button link size="small" tag="a" :href="projMedia(it.file)" download :title="'下载：' + it.name"><el-icon><Download/></el-icon><span>下载</span></el-button>
        <el-button link size="small" @click="goAssets" title="去「素材」页查看（归入 AI 生成素材库）"><el-icon><Picture/></el-icon><span>去素材库</span></el-button>
      </div>
    </div>
  </div>

  <el-dialog v-model="zoomOn" width="62%" top="5vh" destroy-on-close class="fs-player-dlg">
    <img v-if="zoom" :src="projMedia(zoom.file)" style="width:100%;display:block"/>
    <template #footer>
      <span class="dim" style="margin-right:12px">{{ zoom && zoom.meta.prompt }}</span>
      <el-button @click="zoomOn = false">关闭</el-button>
    </template>
  </el-dialog>
</div>
`,
  setup() {
    const info = reactive({ backends: [], default: 'agnes', outDir: '', schemes: [] });
    const prompt = ref('');
    const scheme = ref('realistic');
    const backend = ref('agnes');
    const sizeIdx = ref(0);
    const negative = ref('');
    const steps = ref(25), cfg = ref(7.5);
    const lora = ref('');
    const controlStrength = ref(0.65);
    const seed = ref(0), adv = ref(false);
    const refs = ref([]);
    const items = ref([]);
    const run = ref(null);
    const zoom = ref(null), zoomOn = ref(false);
    const refFile = ref(null);
    const sizes = SIZES;
    const running = computed(() => !!run.value);

    async function loadInfo() {
      let r = {};
      try { r = await api('/api/aidraw/info'); } catch (e) { r = {}; }
      info.backends = r.backends || [];
      info.default = r.default || 'agnes';
      info.outDir = r.out_dir || '';
      info.schemes = r.schemes || [];
      if (!info.schemes.some(s => s.id === scheme.value) && info.schemes.length) {
        scheme.value = info.schemes[0].id;
      }
      if (!info.backends.some(b => b.id === backend.value && b.ready)) {
        const ok = info.backends.find(b => b.ready);
        backend.value = ok ? ok.id : 'agnes';
      }
    }

    async function loadOutputs() {
      let r = {};
      try { r = await api('/api/aidraw/outputs'); } catch (e) { r = {}; }
      items.value = r.items || [];
    }

    function schemeLabel(sid) {
      const s = info.schemes.find(x => x.id === sid);
      return s ? s.label : (sid || '未知');
    }

    function pickRef() { if (refFile.value) refFile.value.click(); }
    async function onRef(e) {
      const files = e.target.files || [];
      for (const f of files) {
        if (refs.value.length >= 3) { toast('参考图最多 3 张', 'warning'); break; }
        const fd = new FormData();
        fd.append('root', S.activeRoot);
        fd.append('file', f);
        const r = await api('/api/aidraw/upload', { method: 'POST', body: fd });
        if (!r.ok) { toast('参考图上传失败：' + (r.err || ''), 'error'); continue; }
        refs.value.push({ path: r.path, name: r.name || f.name });
      }
      e.target.value = '';
    }
    function dropRef(i) { refs.value.splice(i, 1); }

    /* Enter 提交：中文输入法选词时 Enter 带 isComposing/keyCode 229，直接 submit 会误发 */
    function onEnter(e) {
      if (e && (e.isComposing || e.keyCode === 229)) return;
      submit();
    }

    async function submit() {
      const p = (prompt.value || '').trim();
      if (!p) { toast('先写提示词', 'warning'); return; }
      if (!S.activeRoot) { toast('先在「素材库」页选一个库（用于记录任务进度）', 'warning'); return; }
      const sz = sizes[sizeIdx.value] || sizes[0];
      // 高级参数：仅在「更多」展开时作为覆盖项发送；否则发 null，交给方案默认值
      const advOn = adv.value;
      const body = {
        backend: backend.value, prompt: p, scheme: scheme.value,
        refs: refs.value.map(r => r.path),
        seed: Number(seed.value || 0), w: sz.w, h: sz.h,
        root: S.activeRoot,
        negative: advOn ? (negative.value || '') : null,
        lora: advOn ? (lora.value || '') : null,
        control_strength: advOn ? Number(controlStrength.value || 0) : null,
        steps: advOn ? Number(steps.value || 25) : null,
        cfg: advOn ? Number(cfg.value || 7) : null,
      };
      run.value = { pct: 0, msg: '已提交' };
      try {
        const r = await post('/api/aidraw/generate', body);
        if (!r.ok) { run.value = null; toast(r.err || '生成没起来', 'error'); return; }
        run.value.msg = '生成中…';
        startPoll();
      } catch (e) {
        run.value = null; toast(String(e), 'error');
      }
      prompt.value = '';
    }

    async function reroll(it) {
      if (!S.activeRoot) { toast('先在「素材库」页选一个库', 'warning'); return; }
      run.value = { pct: 0, msg: '重掷中…' };
      try {
        const r = await post('/api/aidraw/reroll', { file: it.file, root: S.activeRoot });
        if (!r.ok) { run.value = null; toast(r.err || '重掷没起来', 'error'); return; }
        run.value.msg = '重掷生成中…';
        startPoll();
      } catch (e) {
        run.value = null; toast(String(e), 'error');
      }
    }

    async function del(it) {
      try {
        await ElementPlus.ElMessageBox.confirm(
          `将删除磁盘上的源文件（及同名参数留痕），<b>不可恢复</b>：<br><code>${esc(it.name)}</code>`,
          '删除这张绘画', { dangerouslyUseHTMLString: true, type: 'warning',
            confirmButtonText: '删除' });
      } catch (e) { return; }
      const r = await post('/api/aidraw/delete', { file: it.file });
      if (!r || !r.ok) { toast('删除失败：' + ((r && r.err) || ''), 'error'); return; }
      items.value = items.value.filter(x => x.file !== it.file);
      toast('已删除：' + it.name, 'success');
    }

    function openZoom(it) { zoom.value = it; zoomOn.value = true; }
    function goAssets() { S.tab = 'assets'; }
    function esc(s) { return String(s || '').replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

    const offTick = BUS.on('aidraw:tick', (root, pct, msg) => {
      if (run.value) {
        run.value.pct = Math.max(run.value.pct || 0, pct || 0);
        if (msg) run.value.msg = msg;
      }
    });
    const offDone = BUS.on('aidraw:done', (root, s) => {
      run.value = null;
      loadOutputs();
    });
    onUnmounted(() => { offTick(); offDone(); });

    onMounted(() => { loadInfo(); loadOutputs(); });
    onActivated(loadInfo);   // keep-alive：切回本页重新摸后端状态

    // 回归脚本留的口子
    window.__FS.aidraw = { info, run, items };

    return {
      info, prompt, scheme, backend, sizeIdx, negative, steps, cfg, lora,
      controlStrength, seed, adv, refs, items, run, running, zoom, zoomOn,
      refFile, sizes, schemeLabel, submit, onEnter, pickRef, onRef, dropRef,
      reroll, del, openZoom, goAssets, loadOutputs, projMedia, fmtAgo: FS.fmtAgo,
    };
  },
};

FS.AidrawView = AidrawView;
})();
