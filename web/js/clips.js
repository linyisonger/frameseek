/* 拾帧 FrameSeek · 片段模块（已导出片段列表：含历史导出） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, api, post, fmtAgo, fmtSec, toast, esc } = FS;

const ClipsView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div>
      <h1>导出片段</h1>
      <p>每个素材库自己的 <code>.frameseek\\clips</code> 目录；以前导出过的也在列表里</p>
    </div>
    <div class="fs-tools">
      <el-button @click="load()" :loading="loading" title="重新扫描各素材库的片段目录">
        <el-icon><refresh/></el-icon><span>刷新</span></el-button>
      <el-button @click="openDir()" title="在资源管理器里打开当前库的片段输出目录">
        <el-icon><folder-opened/></el-icon><span>打开片段目录</span></el-button>
    </div>
  </div>

  <el-card shadow="never" class="fs-card">
    <el-form label-position="top" class="fs-form fs-filterform" @submit.prevent>
      <el-row :gutter="12">
        <el-col :xs="24" :sm="12" :md="6">
          <el-form-item label="关键词">
            <el-input v-model="kw" class="w220" clearable placeholder="台词 / 文件名 / 影片名">
              <template #prefix><el-icon><search/></el-icon></template>
            </el-input>
          </el-form-item>
        </el-col>
        <el-col :xs="12" :sm="6" :md="4">
          <el-form-item label="素材库">
            <el-select v-model="lib" class="w160" clearable placeholder="全部库">
              <el-option v-for="L in libs" :key="L" :label="L" :value="L"/>
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :xs="12" :sm="6" :md="4">
          <el-form-item label="排序">
            <el-select v-model="sort" class="w160">
              <el-option label="最近导出" value="time"/>
              <el-option label="按影片名" value="video"/>
              <el-option label="按体积" value="size"/>
              <el-option label="按时长" value="dur"/>
            </el-select>
          </el-form-item>
        </el-col>
        <!-- 统计说明列：与上方三个筛选控件底边对齐（原来用空格 label 占位会对不齐） -->
        <el-col :xs="24" :md="10" class="fs-col-note">
          <div class="fs-colnote">{{ view.length }} 个 · {{ totalSize }} · {{ libCount }} 个库</div>
        </el-col>
      </el-row>
    </el-form>
  </el-card>

  <el-row v-if="view.length" :gutter="14" class="fs-clips">
    <el-col v-for="c in view" :key="c.key" :xs="24" :sm="12" :md="8" :xl="6">
      <el-card shadow="hover" class="fs-clip">
        <video :src="clipUrl(c)" controls preload="metadata" playsinline></video>
        <div class="txt" :title="c.text || c.name">{{ c.text || c.name }}</div>
        <div class="dim ellip" :title="c.video">{{ c.video || c.name }}</div>
        <div class="metas">
          <el-tag size="small" type="info" v-if="c.lib">{{ c.lib }}</el-tag>
          <el-tag size="small" v-if="c.duration">{{ fmtSec(c.duration) }}</el-tag>
          <el-tag size="small" type="success" v-if="c.sub">字幕</el-tag>
          <el-tag size="small" type="warning" v-if="c.sub_skipped"
            title="这条素材画面本来就有字幕（OCR 源），自动模式跳过了烧录；要烧请在检索页把「导出字幕」改成「强制」再导一次">未烧字幕</el-tag>
          <span class="dim">{{ fmtSize(c.size) }} · {{ fmtAgo(c.mtime) }}</span>
        </div>
        <div class="chipops">
          <el-button link size="small" tag="a" :href="dlUrl(c)"
            title="把这个 mp4 下载到本地"><el-icon><download/></el-icon><span>下载</span></el-button>
          <el-button link size="small" @click="openFolder(c)"
            title="在资源管理器里打开这个片段所在的文件夹"><el-icon><folder-opened/></el-icon><span>文件夹</span></el-button>
          <el-button link size="small" type="danger" @click="del(c)"
            title="删除这个片段（连同它的 .srt）"><el-icon><delete/></el-icon><span>删除</span></el-button>
        </div>
      </el-card>
    </el-col>
  </el-row>
  <div v-else-if="loading" class="fs-bxloading dim">
    <el-icon class="is-loading"><Loading/></el-icon><span>正在加载片段…</span>
  </div>
  <div v-else class="fs-empty-tip">
    <el-icon class="fs-empty-ic"><Scissor/></el-icon>
    <b>还没有导出片段</b>
    <span>在「台词检索」里搜到台词后点「导出片段」，片段会出现在这里</span>
  </div>
</div>
`,
  setup() {
    const items = ref([]);          // 后端扫出来的（各库 clips 目录，含历史）
    const loading = ref(false);
    const kw = ref('');
    const lib = ref('');
    const sort = ref('time');

    async function load() {
      loading.value = true;
      try {
        const r = await api('/api/clips');
        items.value = (r && r.clips) || [];
      } catch (e) {
        items.value = [];
      }
      loading.value = false;
    }

    /* 本次会话刚导出的（S.clips）可能还没被后端扫到/没刷新，先并进来去重显示 */
    const merged = computed(() => {
      const map = new Map();
      const push = c => {
        const root = c.root || '';
        const rel = c.rel || '';
        const k = root + '|' + rel;
        if (!root && !rel) return;
        const old = map.get(k);
        map.set(k, Object.assign({}, old || {}, c, { key: k }));
      };
      (items.value || []).forEach(push);
      (S.clips || []).forEach(push);
      return Array.from(map.values());
    });

    const libs = computed(() => Array.from(
      new Set((merged.value || []).map(c => c.lib || shortRoot(c.root)).filter(Boolean))));

    const view = computed(() => {
      const k = (kw.value || '').trim().toLowerCase();
      let out = merged.value.filter(c => {
        if (lib.value && (c.lib || shortRoot(c.root)) !== lib.value) return false;
        if (!k) return true;
        return ((c.text || '') + (c.name || '') + (c.video || '')).toLowerCase().includes(k);
      });
      const by = {
        time: (a, b) => (b.mtime || 0) - (a.mtime || 0),
        video: (a, b) => String(a.video || a.name).localeCompare(String(b.video || b.name), 'zh'),
        size: (a, b) => (b.size || 0) - (a.size || 0),
        dur: (a, b) => (b.duration || 0) - (a.duration || 0),
      }[sort.value] || (() => 0);
      return out.slice().sort(by);
    });

    const totalSize = computed(() => {
      const b = (view.value || []).reduce((n, c) => n + (c.size || 0), 0);
      return b > 1024 * 1024 ? (b / 1024 / 1024).toFixed(1) + ' MB'
                             : Math.max(0, Math.round(b / 1024)) + ' KB';
    });
    const libCount = computed(() => new Set(
      (view.value || []).map(c => c.lib || shortRoot(c.root))).size);

    function shortRoot(root) {
      return String(root || '').split(/[\\/]/).filter(Boolean).pop() || '';
    }
    function fmtSize(b) {
      b = Number(b) || 0;
      return b > 1024 * 1024 ? (b / 1024 / 1024).toFixed(1) + ' MB'
                             : Math.max(0, Math.round(b / 1024)) + ' KB';
    }
    function clipUrl(c) {
      return `/clip?p=${encodeURIComponent(c.rel)}&root=${encodeURIComponent(c.root || '')}`;
    }
    const dlUrl = c => clipUrl(c) + '&dl=1';

    function openDir() {
      const root = S.activeRoot;
      if (!root) { toast('先在「素材库」页选一个库', 'warning'); return; }
      post('/api/open', { path: root + '\\.frameseek\\clips' });
    }
    function openFolder(c) {
      const root = c.root || S.activeRoot;
      if (!root) { toast('这个片段的来源库不明确，先刷新重扫', 'warning'); return; }
      post('/api/open', { path: root + '\\.frameseek\\clips' });
    }
    async function del(c) {
      try {
        await ElementPlus.ElMessageBox.confirm(
          `确定删除「${esc(c.text || c.name)}」？<br><span class="dim">所在目录：`
          + `${esc((c.root || '') + '\\.frameseek\\clips')}</span>`,
          '删除片段',
          { dangerouslyUseHTMLString: true, confirmButtonText: '删除',
            cancelButtonText: '取消', type: 'warning' },
        );
      } catch (e) { return; }
      const r = await post('/api/clips/delete', { root: c.root, rel: c.rel });
      if (r && r.ok) {
        toast('已删除', 'success');
        const k = c.key;
        const i = (S.clips || []).findIndex(x => (x.root || '') + '|' + (x.rel || '') === k);
        if (i >= 0) S.clips.splice(i, 1);
        load();
      } else {
        toast((r && r.err) || '删除失败', 'error');
      }
    }

    onMounted(load);
    onActivated(load);

    return {
      view, libs, kw, lib, sort, loading, totalSize, libCount,
      pre: computed(() => S.pre), post: computed(() => S.post),
      load, clipUrl, dlUrl, openDir, openFolder, del,
      fmtSize, fmtSec, fmtAgo,
    };
  },
};

FS.ClipsView = ClipsView;
})();
