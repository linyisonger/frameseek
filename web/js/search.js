/* 拾帧 FrameSeek · 台词检索模块（跨库搜字幕，导出片段） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, api, play, clipLine, hl, esc, mediaUrl, toast } = FS;

const SearchView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div><h1>台词检索</h1><p>跨全部素材库搜台词，回车直接搜</p></div>
  </div>

  <el-card shadow="never" class="fs-card">
    <div class="fs-bigsearch">
      <el-input id="q" v-model="q" size="large" placeholder="他在思念什么 / 回家 / 你洗不洗脸…"
                clearable @keyup.enter="doSearch" @clear="onClear">
        <template #prefix><el-icon><Search></Search></el-icon></template>
        <template #append>
          <el-button id="btnSearch" type="primary" :loading="searching" @click="doSearch"
            title="跨全部已登记库搜台词，中文子串也行（回车等价）。搜到的每句都能一键导出前后各 N 秒">搜索</el-button>
        </template>
      </el-input>
    </div>
    <el-form label-position="top" class="fs-form" @submit.prevent>
      <el-row :gutter="14">
        <el-col :xs="24" :sm="12" :md="7" :lg="7">
          <el-form-item label="导出片段时长（秒）">
            <div class="fs-cfg-inline">
              前 <el-input-number id="pre" v-model="pre" :min="0" :max="60" size="small" controls-position="right"/> 后 <el-input-number id="post" v-model="post" :min="0" :max="60" size="small" controls-position="right"/>
            </div>
          </el-form-item>
        </el-col>
        <el-col :xs="12" :sm="6" :md="5" :lg="4">
          <el-form-item label="剪辑模式">
            <el-switch id="fast" v-model="fast" size="small" active-text="秒切" inactive-text="精确"
              title="秒切＝直接拷贝原视频流（快，但只能落在关键帧上，切点可能差半秒）。不勾则重编码，切点帧精确，慢一些"/>
          </el-form-item>
        </el-col>
        <el-col :xs="12" :sm="6" :md="5" :lg="4">
          <el-form-item label="导出字幕">
            <el-select id="submode" v-model="subMode" size="small"
              title="自动＝画面本来就带字幕的素材（台词来自 OCR 识别）跳过烧录，其余照旧烧；关闭＝一律不烧；强制＝一律烧（OCR 字幕有错字、纠完想重烧一份时用）">
              <el-option label="自动" value="auto"/>
              <el-option label="关闭" value="off"/>
              <el-option label="强制" value="force"/>
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :xs="12" :sm="6" :md="4" :lg="4">
          <el-form-item label="字幕带">
            <el-switch id="subtitle" v-model="subBand" size="small" active-text="开" inactive-text="关"
              title="预览播放时在画面底部显示当前这句台词（只影响预览，不会烧进导出的片段）"/>
          </el-form-item>
        </el-col>
        <!-- 说明文字独占一列：对齐到与上方控件同一基线。
             原来用 <el-form-item label=" "> 拿空格占位，label 是个真实节点、
             高度由内容撑出来，跟旁边几列的控件对不齐。改成纯文本 + 底部对齐。 -->
        <el-col :xs="24" :sm="24" :md="3" :lg="5" class="fs-col-note">
          <div class="fs-colnote">搜到的每句都能一键导出前后各 N 秒，直接拖进剪映</div>
        </el-col>
      </el-row>
    </el-form>
  </el-card>

  <el-divider />

  <div v-if="rows.length" class="fs-results" id="results">
    <div class="fs-result-summary dim" id="resultSum">
      {{ resultSummary }}
      <el-tag v-if="capped" size="small" type="warning" effect="plain">仅显示前 {{ rows.length }} 条</el-tag>
    </div>
    <div v-for="r in rows" :key="r.id" class="fs-rrow">
      <img :src="shotUrl(r)" loading="lazy" @click="playRow(r)"/>
      <div class="fs-rbody">
        <div class="ts">
          {{ r.start_str }} → {{ r.end_str }}
          <el-tag v-if="r.lib" size="small" type="info">{{ r.lib }}</el-tag>
          <span v-html="boxMark(r)"></span>
        </div>
        <div class="txt" v-html="hl(r.text)"></div>
        <div class="from">{{ r.name }}</div>
      </div>
      <div class="ops">
        <el-button size="small" @click="playRow(r)" title="从这句台词的时间点开始播放原片">预览</el-button>
        <el-button size="small" type="primary" @click="clipRow(r)"
          :title="'导出这句台词前 ' + pre + 's、后 ' + post + 's 的 mp4，直接拖进剪映或 PR'">导出片段</el-button>
      </div>
    </div>
  </div>
  <el-empty v-else-if="searched" description="没有结果，换个词试试"/>
  <div v-else class="fs-empty-tip" id="searchEmpty">
    <b>输入关键词，搜全部素材库里的每一句台词</b>
    <span>支持按台词内容或素材文件名搜；搜到的每句能一键导出前后各 N 秒的片段，直接拖进剪映。</span>
  </div>

  <!-- 多轨片才弹：音轨≥2 或 有内嵌字幕轨。单轨片走老路径，一点就导 -->
  <el-dialog v-model="dlgOpen" title="选择音轨 / 字幕轨" width="460px"
             :close-on-click-modal="false">
    <p class="dim" style="margin:0 0 12px">这个片子有多条轨道，导出前挑一下用哪条</p>
    <el-form label-position="top" @submit.prevent>
      <el-form-item label="音轨" v-if="dlgAudio.length">
        <el-select v-model="dlgAudioSel" size="small" style="width:100%"
          title="片源里每条音频轨，按语言/声道/标题区分。选中的那条会写进导出片段">
          <el-option v-for="a in dlgAudio" :key="'a' + a.index" :label="a.label" :value="a.index"/>
        </el-select>
      </el-form-item>
      <el-form-item label="字幕">
        <el-select v-model="dlgSubSel" size="small" style="width:100%"
          title="项目台词库＝烧本项目索引出来的台词（受上面「导出字幕」三档控制）；内嵌字幕轨＝直接烧片源自带的某条字幕；不烧＝导出的片段不带字幕">
          <el-option label="项目台词库（按现有 自动/关闭/强制 判断）" value="db"/>
          <el-option v-for="s in dlgSubs" :key="'s' + s.index"
                     :label="'内嵌 ' + s.label" :value="String(s.index)"/>
          <el-option label="不烧字幕" value="none"/>
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button size="small" @click="dlgOpen = false">取消</el-button>
      <el-button size="small" type="primary" @click="confirmClip">导出</el-button>
    </template>
  </el-dialog>
</div>
`,
  setup() {
    const q = ref('');
    const rows = ref([]);
    const searched = ref(false);
    const searching = ref(false);
    const capped = ref(false);      // 命中达到后端上限，可能被截断

    const pre = computed({
      get: () => S.pre, set: v => { S.pre = v == null ? 0 : v; },
    });
    const post_ = computed({
      get: () => S.post, set: v => { S.post = v == null ? 0 : v; },
    });
    const fast = computed({ get: () => S.fast, set: v => { S.fast = !!v; } });
    const subMode = computed({
      get: () => S.subMode,
      set: v => { S.subMode = ['off', 'force'].includes(v) ? v : 'auto'; },
    });
    const subBand = computed({
      get: () => S.subBand, set: v => { S.subBand = !!v; },
    });

    async function doSearch() {
      const kw = (q.value || '').trim();
      if (!kw) { toast('先输入要搜的台词或片名', 'warning'); return; }
      searched.value = true;
      searching.value = true;
      try {
        const r = await api(
          `/api/search?q=${encodeURIComponent(kw)}&scope=all&limit=300`);
        rows.value = r || [];
        // 后端按 limit 截断：恰好或超过 300 就提示可能没列全
        capped.value = (rows.value.length >= 300);
      } finally {
        searching.value = false;
      }
    }
    function onClear() { searched.value = false; rows.value = []; capped.value = false; }

    const resultSummary = computed(() =>
      `命中 ${rows.value.length} 句`);

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

    /* 多轨选择：只有后端判定 multi（音轨≥2 或有内嵌字幕轨）才弹框。
       单轨片完全走老路径，一点就导，不多一步操作。 */
    const dlgOpen = ref(false);
    const dlgAudio = ref([]);
    const dlgSubs = ref([]);
    const dlgAudioSel = ref(null);
    const dlgSubSel = ref('db');
    let dlgRow = null;

    async function clipRow(r) {
      try {
        const t = await api(`/api/video/tracks?root=${encodeURIComponent(r.root)}`
          + `&video_id=${r.video_id}`);
        if (t && t.ok && t.multi) {
          dlgAudio.value = t.audio || [];
          dlgSubs.value = t.subtitle || [];
          const def = (t.audio || []).find(a => a.default);
          dlgAudioSel.value = def ? def.index
            : (((t.audio || [])[0] || {}).index ?? null);
          dlgSubSel.value = 'db';
          dlgRow = r;
          dlgOpen.value = true;
          return;
        }
      } catch (e) {
        /* 探测失败不挡人：按老样子直接导 */
      }
      return clipLine(r.id, r.root);
    }

    function confirmClip() {
      dlgOpen.value = false;
      const r = dlgRow;
      if (!r) return;
      const opts = {};
      if (dlgAudioSel.value != null) opts.audio_track = dlgAudioSel.value;
      if (dlgSubSel.value === 'none') opts.subtitle = 'off';
      else if (dlgSubSel.value !== 'db') opts.sub_track = Number(dlgSubSel.value);
      return clipLine(r.id, r.root, opts);
    }

    return {
      q, rows, searched, searching, capped, resultSummary, pre, post: post_, fast,
      subMode, subBand,
      dlgOpen, dlgAudio, dlgSubs, dlgAudioSel, dlgSubSel,
      doSearch, onClear, boxMark, clipRow, confirmClip,
      hl: t => hl(t, q.value),
      shotUrl: r => mediaUrl(r.shot || r.cover, r.root),
      playRow: r => play(r.video_id, r.start, r.name, r.root),
    };
  },
};

FS.SearchView = SearchView;
})();
