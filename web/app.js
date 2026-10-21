/* 拾帧 FrameSeek · 前端引导（只做装配与挂载，业务逻辑拆在 web/js/*.js）
   加载顺序（index.html）：vue / element-plus / zh-cn / icons →
   js/state.js → js/{libs,search,assets,box,clips}.js → 本文件。 */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, LIVE, PHASE, Route, api, post, esc, toast, loadLibs, addClips,
        connectLive, fmtDur } = FS;

/* ==================== 主应用 ==================== */
const App = {
  components: {
    LibsView: FS.LibsView, SearchView: FS.SearchView, AssetsView: FS.AssetsView,
    BoxView: FS.BoxView, ClipsView: FS.ClipsView,
  },
  setup() {
    const videoEl = ref(null);

    const statsText = computed(() => (S.libs || []).length
      ? `${S.libs.length} 个库 · ${S.totals.videos} 个素材 · ${S.totals.lines} 条台词`
      : '还没有登记素材库');

    // 全局浮窗显示素材索引任务。
    const taskList = computed(() => Object.values(S.tasks || {}));

    function dockPct(t) { return t.total ? Math.min(100, Math.round(t.done / t.total * 100)) : 0; }
    function dockPhase(t) { return PHASE[t.phase] || ''; }
    function dockEta(t) {
      if (!(t.eta_min > 0)) return '';
      return ` · 还剩约 ${t.eta_min < 1 ? Math.ceil(t.eta_min * 60) + ' 秒'
                                        : Math.round(t.eta_min) + ' 分钟'}`;
    }

    /* 收起状态下也常显的一行摘要：跑到第几个、当前是哪部、出了多少条台词。
       二级目录下的片子后端给的就是相对路径（亮剑/01.mp4），这里照搬。 */
    function dockLive(t) {
      const parts = [];
      if (t.kind !== 'video' && t.total) {
        parts.push(`第 ${Math.min((t.done || 0) + 1, t.total)}/${t.total} 个`);
      }
      if (t.lines) parts.push(`已出 ${t.lines} 条台词`);
      return parts.join(' · ') || (t.file || '');
    }

    /* 已识别的画面帧数——用户要的「目前视频的识别画面数量」。
       frames 是已扫过的采样帧数；frames_total 是外推的总帧数（跑一会才准）。 */
    function docFrames(t) {
      const n = t.frames || 0;
      if (!n) return '';
      const tot = t.frames_total || 0;
      const p = t.frames_pct ? ` · ${t.frames_pct}%` : '';
      return tot
        ? `已识别画面 ${n.toLocaleString()} 帧 / 约 ${tot.toLocaleString()} 帧${p}`
        : `已识别画面 ${n.toLocaleString()} 帧${p}`;
    }

    function dockMsg(t) { return (t.message || '') + dockEta(t); }

    /* 中途终止：确认后打停止接口。后端在 OCR/ASR 的下一个检查点收工，
       已经跑完的素材都留在库里，下次索引按 md5 跳过它们接着跑。 */
    async function onStop(t) {
      const cur = t.file ? `<br>正在识别的 <b>${esc(t.file)}</b> 会留成「还没识别」，下次重跑。` : '';
      try {
        await ElementPlus.ElMessageBox.confirm(
          '已经识别完的素材都保留在库里，下次点「索引」会跳过它们、' + '从断点接着往下跑。' + cur
          + '<br><br><span class="dim">停止信号在 1~2 秒内生效（OCR 每两秒看一次）。</span>',
          `停止「${t.lib || '索引'}」`,
          { dangerouslyUseHTMLString: true, confirmButtonText: '停止索引',
            cancelButtonText: '继续跑', type: 'warning' },
        );
      } catch (e) { return; }           // 用户点了「继续跑」
      const r = await post('/api/scan/stop', { root: t.root });
      toast(r.ok ? '正在停止…' : (r.err || '停止失败'), r.ok ? 'warning' : 'error');
    }

    function onTab(name) {
      S.tab = name;
      // 切页签 = 丢弃该页的 URL 参数（筛选是页面级状态，不是全局的）。
      // 但用 Route.set 而不是裸 replaceState：让 hash 与 S.tab 始终一致，
      // 顺带把广播打出去，子页签能知道自己是被"切回来"还是"刷新进来的"。
      if (Route.parse().tab !== name) Route.set(name, {}, true);
    }

    function onPlayerClosed() {
      const v = videoEl.value;
      if (v) { v.pause(); v.src = ''; }
      S.player.sub = '';
    }
    async function clipHere() {
      const v = videoEl.value;
      if (!v || !S.player.cur) return;
      const c = S.player.cur;
      const r = await api('/api/clip', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root: c.root, video_id: c.video_id,
          start: v.currentTime, end: v.currentTime + 1,
          pre: S.pre, post: S.post, fast: S.fast, subtitle: S.subMode }),
      });
      await addClips(r);
      S.player.on = false;
    }

    onMounted(async () => {
      await loadLibs();
      // 先按 hash 定位页签（无 hash / 非法值回素材库），再让 Route 接管后续变更。
      const r = Route.parse();
      let want = Route.tabs.indexOf(r.tab) >= 0 ? r.tab : 'libs';
      S.tab = want;
      Route.init();
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
      S, tab: computed(() => S.tab), onTab, statsText,
      taskList, videoEl,
      dockPct, dockPhase, dockEta, dockLive, docFrames, dockMsg, onStop,
      onPlayerClosed, clipHere,
      libs: computed(() => S.libs), ocr: computed(() => S.ocr),
      viewCount: computed(() => S.viewCount),
      boxNoBox: computed(() => S.boxNoBox),
      clips: computed(() => S.clips),
      player: computed(() => S.player),
      dockOpen: computed({ get: () => S.dockOpen, set: v => { S.dockOpen = v; } }),
      fmtDur,
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
