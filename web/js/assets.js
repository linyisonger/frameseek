/* 拾帧 FrameSeek · 素材模块（跨库浏览、筛选、重识别/导入/删除） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, BUS, api, post, toast, play, mediaUrl, loadLibs, startPoll, esc } = FS;

const AssetsView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div><h1>素材 <span class="dim">({{ viewCount }})</span></h1>
      <p>点卡片预览，右上角 ··· 可重新识别 / 导入字幕 / 移除 / 删文件</p></div>
    <div class="fs-tools">
      <el-input id="vfilter" v-model="kw" class="mini" placeholder="过滤文件名或库名…" clearable
        title="按文件名或库名过滤，边打边生效"/>
      <el-button id="vMoreBtn" size="small" @click="showFilter = !showFilter"
        title="多维筛选：素材库可多选、按时长分档">
        {{ showFilter ? '筛选 ▴' : '筛选 ▾' }}</el-button>
      <el-button id="vResetBtn" size="small" @click="resetFilters" title="清空所有筛选条件，回到完整列表">重置</el-button>
    </div>
  </div>

  <el-card v-show="showFilter" shadow="never" class="fs-card fs-filtercard" id="vFilterCard">
    <div class="fs-frow">
      <span class="fs-label">素材库</span>
      <div class="fs-fchips" id="fLibs">
        <el-checkbox v-model="libsAll" label="全部" size="small"/>
        <el-checkbox v-for="l in allLibs" :key="l.id" v-model="libSel[l.id]" size="small"
                     :title="l.path">{{ l.name }}<template v-if="l.builtin"> 🔒</template><b class="dim">{{ l.videos }}</b></el-checkbox>
      </div>
    </div>
    <div class="fs-frow">
      <span class="fs-label">时长</span>
      <el-checkbox-group v-model="durSel" size="small" class="fs-fchips" id="fDur">
        <el-checkbox label="s30" value="s30">30 秒内</el-checkbox>
        <el-checkbox label="m30" value="m30">30 秒 - 5 分</el-checkbox>
        <el-checkbox label="m5_20" value="m5_20">5 - 20 分</el-checkbox>
        <el-checkbox label="l20" value="l20">20 分以上</el-checkbox>
      </el-checkbox-group>
    </div>
    <div class="fs-frow fs-fsum dim" id="fSummary">{{ summary }}</div>
  </el-card>

  <div class="fs-videos" id="videos">
    <div v-for="(v, i) in view" :key="v.root + '#' + v.id" class="fs-vcard" :title="v.name">
      <div class="fs-vthumb">
        <img :src="coverUrl(v)" loading="lazy"
             @mouseenter="hoverGif($event, v, true)" @mouseleave="hoverGif($event, v, false)"
             @click="preview(v)"/>
        <div class="fs-vgrad"></div>
        <span class="fs-vdur">{{ v.duration_str }}</span>
      </div>
      <el-dropdown trigger="click" placement="bottom-end" @command="cmd => onCmd(cmd, v)">
        <el-button class="fs-vmore" size="small" text
          :title="v.builtin ? '更多操作：打开文件夹 / 从库里移除 / 删除文件（AI 生成产物不做字幕识别）'
                           : '更多操作：重新识别台词 / 导入字幕 / 打开文件夹 / 移除或删除'">···</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="preview" title="在右侧播放器里预览这部片子">预览</el-dropdown-item>
            <el-dropdown-item command="dir" title="在资源管理器里打开这个视频所在的文件夹">打开所在文件夹</el-dropdown-item>
            <template v-if="!v.builtin">
              <el-dropdown-item command="reimport"
                title="用记事本改完视频同名的 .srt 后点这个：直接刷新数据库和截图，不用重跑 OCR">重新导入同名 SRT</el-dropdown-item>
              <el-dropdown-item command="retranscribe"
                title="重跑一次画面 OCR，重新识别硬字幕台词。已有的同名 .srt 会被覆盖">重新识别台词</el-dropdown-item>
              <el-dropdown-item command="asr"
                title="改用语音识别（听声音）出台词，适合没有画面字幕、只有人声的片子，耗时比 OCR 长">语音识别台词</el-dropdown-item>
            </template>
            <el-dropdown-item command="remove" title="只把这个视频从索引里移除，磁盘文件保留">从库中移除</el-dropdown-item>
            <el-dropdown-item command="purge" divided
              title="连磁盘文件一起删除，不可恢复（要连点两次确认）">删除素材及文件</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <div class="fs-vmeta">
        <b>{{ v.name }}</b>
        <span>{{ v.builtin ? '🔒 ' : '' }}{{ v.lib }}</span>
        <div class="row2">
          <el-tag v-if="v.builtin" size="small" title="AI 生成产物不参与台词识别">AI 生成</el-tag>
          <el-tag v-else-if="v.err" size="small" type="danger">识别失败</el-tag>
          <el-tag v-else-if="v.line_count" size="small" type="success">台词 {{ v.line_count }}</el-tag>
          <el-tag v-else size="small" type="info">没台词</el-tag>
        </div>
      </div>
    </div>
    <div v-if="!view.length" class="dim pad">还没有素材，先去「素材库」登记并索引</div>
  </div>

  <!-- 重新识别台词 -->
  <el-dialog v-model="dlg.re" title="重新识别台词" width="520px">
    <div><b>{{ pickName }}</b></div>
    <div class="fs-hint">当前台词 {{ prevCount }} 条（{{ prevSrc }}）。重新识别会
      <b>先清空这些台词和旧截图</b>，再按最新逻辑重新识别。</div>
    <el-checkbox v-model="reOpt.force" label="强制重跑 OCR（忽略已有字幕）"/>
    <div class="fs-hint">默认按优先级：同名 .srt → 内嵌字幕 → OCR 识别画面。
      上次是 OCR 时勾选它，才不会把上次生成的 .srt 又读一遍。进度看右下角浮窗。</div>
    <template #footer>
      <el-button @click="dlg.re = false">取消</el-button>
      <el-button type="primary" @click="doRetranscribe">清空并重新识别</el-button>
    </template>
  </el-dialog>

  <!-- 语音识别台词 -->
  <el-dialog v-model="dlg.asr" title="语音识别台词" width="520px">
    <div><b>{{ pickName }}</b></div>
    <div class="fs-hint">听音轨转写对白，<b>不读</b>同名 .srt、不读内嵌字幕、也不跑 OCR。
      适合画面上没有字幕（或字幕认不出来）的电影。</div>
    <div class="fs-hint">当前台词 {{ prevCount }} 条（{{ prevSrc }}）。语音识别会
      <b>先清空这些台词和旧截图</b>再转写。</div>
    <div class="fs-mgrid">
      <span>模型
        <el-select v-model="asrOpt.model" size="small" class="w120">
          <el-option v-for="m in ['tiny','base','small','medium']" :key="m"
                     :label="m + (m === (asrOpt.info || {}).default_model ? '（默认）' : '')" :value="m"/>
        </el-select>
      </span>
      <span class="dim">small 准确、tiny 快 3 倍但错字多；首次用某个模型要先下载（几百 MB）</span>
    </div>
    <div class="fs-hint">识别设备：{{ asrEta }}（粗估，进度看右下角浮窗）。
      转写完会在视频同目录生成同名 .srt，可改完再「重新导入」。</div>
    <template #footer>
      <el-button @click="dlg.asr = false">取消</el-button>
      <el-button type="primary" @click="doAsr">开始语音识别</el-button>
    </template>
  </el-dialog>
</div>
`,
  setup() {
    const kw = ref('');
    const showFilter = ref(false);
    const libSel = reactive({});          // { libId: bool }
    const durSel = ref([]);               // 时长分档

    const allLibs = computed(() => (S.libs || []).concat(S.builtinLibs || []));

    // 「全部」勾上 = 不设库条件；勾其它项时自动取消「全部」
    const libsAll = computed({
      get: () => Object.values(libSel).every(v => !v),
      set: v => { if (v) Object.keys(libSel).forEach(k => { libSel[k] = false; }); },
    });

    const libSet = computed(() => new Set(
      Object.keys(libSel).filter(k => libSel[k]).map(Number)));

    const view = computed(() => {
      const f = (kw.value || '').trim().toLowerCase();
      const libs = libSet.value, dur = new Set(durSel.value);
      return S.all.filter(r => {
        if (libs.size && !libs.has(r.lib_id)) return false;
        if (dur.size) {
          const d = +(r.duration || 0);
          const hit = (dur.has('s30') && d > 0 && d < 30)
                   || (dur.has('m30') && d >= 30 && d < 300)
                   || (dur.has('m5_20') && d >= 300 && d < 1200)
                   || (dur.has('l20') && d >= 1200);
          if (!hit) return false;
        }
        if (f && !(r.name || '').toLowerCase().includes(f)
              && !(r.lib || '').toLowerCase().includes(f)) return false;
        return true;
      });
    });

    watch(view, v => { S.viewCount = v.length; }, { immediate: true });

    const summary = computed(() => {
      const bits = [];
      if (libSet.value.size) {
        bits.push('库：' + [...libSet.value].map(id => {
          const l = allLibs.value.find(x => +x.id === +id);
          return l ? l.name : id;
        }).join('、'));
      }
      if (durSel.value.length) bits.push('时长：' + durSel.value.join('/'));
      return `命中 ${view.value.length} / ${S.all.length} 部`
           + (bits.length ? ' · ' + bits.join(' · ') : ' · 未设条件');
    });

    async function reload() {
      const qs = libSet.value.size
        ? `&libs=${[...libSet.value].join(',')}` : '';
      S.all = await api(`/api/videos?scope=all${qs}`);
    }
    // 切换页签会重新挂载，监听必须跟着解绑，否则回调会越积越多
    const offRefresh = BUS.on('assets:refresh', reload);
    onUnmounted(offRefresh);
    watch(libSet, reload);

    function resetFilters() {
      Object.keys(libSel).forEach(k => { libSel[k] = false; });
      durSel.value = [];
      kw.value = '';
      return reload();
    }

    const picking = ref(null);
    const dlg = reactive({ re: false, asr: false });
    const reOpt = reactive({ force: false });
    const asrOpt = reactive({ model: 'small', info: null });

    function onCmd(cmd, v) {
      if (cmd === 'preview') { play(v.id, 0, v.name, v.root); return; }
      if (cmd === 'dir') {
        const rel = (v.rel || '').replace(/[\\/]+[^\\/]*$/, '');
        post('/api/open', { path: rel ? v.root + '\\' + rel : v.root });
        return;
      }
      if (cmd === 'reimport') return askReimport(v);
      if (cmd === 'retranscribe') return askRetranscribe(v);
      if (cmd === 'asr') return askAsr(v);
      if (cmd === 'remove') return askDel(v, false);
      if (cmd === 'purge') return askDel(v, true);
    }

    const PREV = { external: '外挂 SRT', embedded: '内嵌字幕',
                   ocr: 'OCR 识别', asr: '语音转写', pending: '识别中' };

    function askRetranscribe(v) {
      picking.value = v;
      // 上次是 OCR 出来的话，视频旁边的同名 .srt 是我们自己的产物，
      // 不跳过它等于把旧结果读回来——所以这种情况默认强制重跑。
      reOpt.force = v.line_source === 'ocr';
      dlg.re = true;
    }
    async function doRetranscribe() {
      const v = picking.value;
      dlg.re = false;
      const r = await post('/api/video/retranscribe',
        { root: v.root, video_id: v.id, force: !!reOpt.force });
      if (!r.ok) { toast(r.err || '启动失败', 'error'); return; }
      startPoll();
      toast('已清空旧台词，开始重新识别「' + v.name + '」');
    }

    async function askAsr(v) {
      picking.value = v;
      let info = {};
      try { info = await api('/api/asr/info'); } catch (e) { info = {}; }
      asrOpt.info = info;
      asrOpt.model = info.default_model || 'small';
      dlg.asr = true;
    }
    async function doAsr() {
      const v = picking.value;
      dlg.asr = false;
      const r = await post('/api/video/transcribe_asr',
        { root: v.root, video_id: v.id, model: asrOpt.model });
      if (!r.ok) { toast(r.err || '启动失败', 'error'); return; }
      startPoll();
      toast('开始语音识别「' + v.name + '」');
    }
    const asrEta = computed(() => {
      const v = picking.value, i = asrOpt.info;
      if (!v || !i) return '';
      const eta = i.cuda ? (v.duration || 0) / 10 : (v.duration || 0) / 1.8;
      return (i.cuda ? 'GPU' : 'CPU') + ' · 预计 '
        + (eta >= 60 ? Math.round(eta / 60) + ' 分钟' : Math.round(eta) + ' 秒');
    });

    function askReimport(v) {
      ElementPlus.ElMessageBox.confirm(
        `读取视频同目录的同名 .srt（必须是 `
        + `<code>${esc((v.name || '').replace(/\.[^.]+$/, ''))}.srt</code>），`
        + `覆盖数据库里的台词并重抽截图。<br>`
        + `<span class="dim">适合用记事本/剪映改完错字、调完时间点之后用——不用重新转写。</span>`,
        '重新导入字幕',
        { dangerouslyUseHTMLString: true, confirmButtonText: '导入' },
      ).then(async () => {
        const r = await post('/api/video/reimport', { root: v.root, video_id: v.id });
        if (!r.ok) { toast(r.err || '导入失败', 'error'); return; }
        toast(`已导入 ${r.lines} 条字幕`, 'success');
        await reload();
      }).catch(() => {});
    }

    function askDel(v, withFile) {
      const full = [v.root, v.rel].filter(Boolean).join('\\');
      const body = `<div><b>${esc(v.name)}</b></div>`
        + `<div class="dim">${esc(full)}</div>`
        + `<div class="dim">${v.duration_str || ''} · 台词 ${v.line_count || 0}`
        + ` · 库「${esc(v.lib || '')}」</div>`
        + (withFile
          ? `<div style="margin-top:8px" class="bad"><b>会把磁盘上的文件一起删掉，`
            + `不进回收站，不可恢复。</b><br>同时删除：视频原文件、同名外挂字幕、`
            + `封面、动图、该片所有台词截图。</div>`
          : `<div class="dim" style="margin-top:6px">只把这条记录从库里去掉，磁盘文件保留。</div>`);
      // 删文件要点两次才算数：第一次按只改按钮文字，什么都不做
      let armed = !withFile;
      ElementPlus.ElMessageBox.confirm(body,
        withFile ? '删除素材及文件' : '从库中移除',
        { dangerouslyUseHTMLString: true,
          confirmButtonText: withFile ? '删除' : '移除',
          type: withFile ? 'error' : 'warning',
          beforeClose: (action, inst, done) => {
            if (action !== 'confirm') { done(); return; }
            if (!armed) {
              armed = true;
              inst.confirmButtonText = '再点一次：永久删除';
              return;                       // 不 done()，弹窗留着
            }
            inst.confirmButtonLoading = true;
            post('/api/video/delete',
              { root: v.root, video_id: v.id, delete_file: withFile })
              .then(async r => {
                inst.confirmButtonLoading = false;
                done();
                await loadLibs();
                toast(r && r.ok
                  ? (withFile ? `已删除「${v.name}」及磁盘文件（${(r.deleted || []).length} 个）`
                              : `已从库里移除「${v.name}」，文件还在`)
                  : '失败：' + ((r && r.err) || '看服务端日志'),
                  r && r.ok ? 'success' : 'error');
              });
          },
        }).catch(() => {});
    }

    // keep-alive 下 onActivated 首次挂载也会触发，切回素材页时刷新一遍
    // （实时事件推来的素材在别的时候也要能进来，靠 assets:refresh 兜着）
    onActivated(reload);

    return {
      kw, showFilter, libSel, durSel,
      allLibs, libsAll, view, summary, picking, dlg, reOpt, asrOpt, asrEta,
      resetFilters, onCmd, doRetranscribe, doAsr,
      viewCount: computed(() => view.value.length),
      coverUrl: v => mediaUrl(v.cover, v.root),
      preview: v => play(v.id, 0, v.name, v.root),
      hoverGif(ev, v, on) {
        if (!v.gif) return;
        const el = ev.target;
        if (on) {
          if (el.dataset.gif === '1') return;
          el.dataset.src = el.src; el.dataset.gif = '1';
          el.src = mediaUrl(v.gif, v.root);
        } else {
          if (el.dataset.gif !== '1') return;
          el.src = el.dataset.src; el.dataset.gif = '0';
        }
      },
      prevSrc: computed(() => {
        const v = picking.value;
        return v ? PREV[v.line_source || ''] || '无' : '';
      }),
      prevCount: computed(() => (picking.value || {}).line_count || 0),
      pickName: computed(() => (picking.value || {}).name || ''),
    };
  },
};

FS.AssetsView = AssetsView;
})();
