/* 拾帧 FrameSeek · 片段模块（导出片段列表） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, post } = FS;

const ClipsView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div><h1>导出片段</h1><p>前后各 <b>{{ pre }}</b> / <b>{{ post }}</b> 秒（在「台词检索」里改）</p></div>
  </div>
  <div class="fs-clips" id="clips">
    <el-card v-for="(c, i) in clips" :key="i" shadow="hover" class="fs-clip">
      <div class="txt">{{ c.text || c.name }}</div>
      <div class="dim">{{ c.name }}</div>
      <video :src="clipUrl(c)" controls preload="metadata"></video>
      <div class="chipops">
        <el-button link size="small" tag="a" :href="clipUrl(c) + '&dl=1'"
          title="把这个 mp4 下载到本地">下载</el-button>
        <el-button link size="small" @click="openDir(c)"
          title="在资源管理器里打开片段输出目录">打开所在文件夹</el-button>
      </div>
    </el-card>
    <div v-if="!clips.length" class="dim pad">
      还没有导出片段。在「台词检索」里搜到台词后点「导出片段」</div>
  </div>
</div>
`,
  setup() {
    return {
      clips: computed(() => S.clips),
      pre: computed(() => S.pre), post: computed(() => S.post),
      clipUrl: c => `/clip?p=${encodeURIComponent(c.rel)}`
        + `&root=${encodeURIComponent(c.root)}`,
      openDir: c => post('/api/open',
        { path: (c.root || '') + '\\.frameseek\\clips' }),
    };
  },
};

FS.ClipsView = ClipsView;
})();
