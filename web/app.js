/* 拾帧 FrameSeek · 前端引导（只做装配与挂载，业务逻辑拆在 web/js/*.js）
   加载顺序（index.html）：vue / element-plus / zh-cn / icons →
   js/state.js → js/{libs,search,assets,gen,handdraw,box,clips}.js → 本文件。 */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, LIVE, PHASE, api, loadLibs, addClips, connectLive } = FS;

/* ==================== 主应用 ==================== */
const App = {
  components: {
    LibsView: FS.LibsView, SearchView: FS.SearchView, AssetsView: FS.AssetsView,
    GenView: FS.GenView, HanddrawView: FS.HanddrawView, BoxView: FS.BoxView,
    ClipsView: FS.ClipsView,
  },
  setup() {
    const videoEl = ref(null);

    const statsText = computed(() => (S.libs || []).length
      ? `${S.libs.length} 个库 · ${S.totals.videos} 个素材 · ${S.totals.lines} 条台词`
      : '还没有登记素材库');

    // AI 生成任务不进全局浮窗：生成页的对话气泡里已经有进度条和状态，
    // 右下角再弹一张卡是重复信息，而且生成页切走再切回来（keep-alive）气泡还在。
    const taskList = computed(() => Object.values(S.tasks || {})
      .filter(t => t.kind !== 'gen'));

    function dockPct(t) { return t.total ? Math.min(100, Math.round(t.done / t.total * 100)) : 0; }
    function dockHead(t) {
      if (t.kind === 'video') return t.file || t.message || '';
      if (t.kind === 'gen') return t.message || '';
      return `${t.done || 0}/${t.total || 0} 个素材`;
    }
    function dockPhase(t) { return PHASE[t.phase] || ''; }
    function dockEta(t) {
      if (!(t.eta_min > 0)) return '';
      return ` · 还剩约 ${t.eta_min < 1 ? Math.ceil(t.eta_min * 60) + ' 秒'
                                        : Math.round(t.eta_min) + ' 分钟'}`;
    }

    function onTab(name) {
      S.tab = name;
      if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
    }

    function onPlayerClosed() {
      const v = videoEl.value;
      if (v) { v.pause(); v.src = ''; }
    }
    async function clipHere() {
      const v = videoEl.value;
      if (!v || !S.player.cur) return;
      const c = S.player.cur;
      const r = await api('/api/clip', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root: c.root, video_id: c.video_id,
          start: v.currentTime, end: v.currentTime + 1,
          pre: S.pre, post: S.post, fast: S.fast }),
      });
      await addClips(r);
      S.player.on = false;
    }

    onMounted(async () => {
      await loadLibs();
      const h = (location.hash || '').slice(1);
      const tabs = ['libs', 'search', 'assets', 'gen', 'hd', 'box', 'clips'];
      onTab(tabs.includes(h) ? h : 'libs');
      connectLive();
      // 首屏可能正有任务在跑（服务端线程还活着）。只当临时快照——
      // WS 一旦推来真事件就会覆盖它；WS 不可用时它才长期顶替轮询。
      if (!LIVE.on) {
        let ts = [];
        try { ts = await api('/api/tasks'); } catch (e) { ts = []; }
        (ts || []).forEach(t => {
          if (!t.root || t.status !== 'running') return;
          S.tasks[t.root] = { root: t.root, lib: t.lib, done: t.done, total: t.total,
            message: t.message, phase: '', pct: t.pct, kind: t.kind || '' };
        });
      }
    });

    return {
      S, tab: computed(() => S.tab), onTab, statsText, taskList, videoEl,
      dockPct, dockHead, dockPhase, dockEta, onPlayerClosed, clipHere,
      libs: computed(() => S.libs), ocr: computed(() => S.ocr),
      viewCount: computed(() => S.viewCount),
      boxNoBox: computed(() => S.boxNoBox),
      clips: computed(() => S.clips),
      player: computed(() => S.player),
      dockOpen: computed({ get: () => S.dockOpen, set: v => { S.dockOpen = v; } }),
    };
  },
};

/* ==================== 挂载 ==================== */
const app = Vue.createApp(App);
app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
// 图标全量注册：模板里直接写 <Search/> <Film/> 等
Object.keys(ElementPlusIconsVue || {}).forEach(k => {
  const c = ElementPlusIconsVue[k];
  if (c && (c.render || c.setup || c.template)) app.component(k, c);
});
app.mount('#app');

// 回归脚本的基础口子已在 js/state.js 设好，这里无需重复。
})();
