/* 拾帧 FrameSeek · 台词检索模块（跨库搜字幕，导出片段） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, api, play, clipLine, hl, esc, mediaUrl } = FS;

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
          <el-button id="btnSearch" type="primary" @click="doSearch"
            title="跨全部已登记库搜台词，中文子串也行（回车等价）。搜到的每句都能一键导出前后各 N 秒">搜索</el-button>
        </template>
      </el-input>
    </div>
    <div class="fs-searchcfg">
      <span class="fs-label">导出片段</span>
      前 <el-input-number id="pre" v-model="pre" :min="0" :max="60" size="small" controls-position="right"/> 秒
      后 <el-input-number id="post" v-model="post" :min="0" :max="60" size="small" controls-position="right"/> 秒
      <el-switch id="fast" v-model="fast" size="small" active-text="秒切"
        title="秒切＝直接拷贝原视频流（快，但只能落在关键帧上，切点可能差半秒）。不勾则重编码，切点帧精确，慢一些"/>
      <span class="dim">搜到的每句都能一键导出前后各 N 秒，直接拖进剪映</span>
    </div>
  </el-card>

  <div v-if="rows.length" class="fs-results" id="results">
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
</div>
`,
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

FS.SearchView = SearchView;
})();
