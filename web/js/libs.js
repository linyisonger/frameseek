/* 拾帧 FrameSeek · 素材库模块（登记/索引/上传/忽略名单） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, BUS, api, post, toast, esc, fmtDur, fmtAgo, LS, loadLibs, startPoll } = FS;

const LibsView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div>
      <h1>素材库</h1>
      <p>登记本地文件夹，索引出台词与截图</p>
    </div>
  </div>

  <el-card shadow="never" class="fs-card">
    <div class="fs-addrow">
      <el-input id="libPath" v-model="form.path" placeholder="素材文件夹绝对路径，例如 E:\\视频\\电影" clearable
        title="填一个本地文件夹的绝对路径（最后一层就是要登记的库），登记后用它自己的「索引」按钮抽台词"/>
      <el-input id="libName" v-model="form.name" placeholder="备注名（可空）" class="w200" clearable/>
      <el-button id="btnAddLib" type="primary" @click="addLib"
        title="把一个本地文件夹登记成素材库（登记后点「索引」才抽台词）">＋ 登记</el-button>
      <el-button id="btnScanAll" @click="askScanAll"
        title="对所有已登记库重跑索引（按 md5 去重，不重复入库）">索引全部</el-button>
    </div>
    <div class="fs-note">
      登记信息存在项目目录 <code>{{ hubDb }}</code> · 同一个文件按 md5 去重，不会重复入库
    </div>
  </el-card>

  <div class="fs-libcards" id="libCards">
    <el-card v-for="l in libs" :key="l.id" shadow="hover" class="fs-libcard"
             :class="{ 'is-missing': !l.exists, 'is-indexed': l.exists && l.indexed }">
      <template #header>
        <div class="fs-libcard-hd">
          <div class="fs-libicon" :class="{ missing: !l.exists, ok: l.exists && l.indexed }">
            <el-icon><FolderOpened></FolderOpened></el-icon>
          </div>
          <div class="min0">
            <b :title="l.name">{{ l.name }}</b>
            <div class="note" v-if="l.note">{{ l.note }}</div>
          </div>
          <el-tag v-if="!l.exists" type="danger" size="small" effect="plain">目录丢失</el-tag>
          <el-tag v-else-if="l.indexed" type="success" size="small" effect="plain">已索引</el-tag>
          <el-tag v-else type="info" size="small" effect="plain">未索引</el-tag>
        </div>
      </template>
      <div class="fs-libpath" :title="l.path">{{ l.path }}</div>
      <div class="fs-libstats">
        <div class="fs-lstat">
          <el-icon><Document></Document></el-icon>
          <div><b>{{ l.videos }}</b><span>素材</span></div>
        </div>
        <div class="fs-lstat">
          <el-icon><Document></Document></el-icon>
          <div><b>{{ l.lines }}</b><span>台词</span></div>
        </div>
        <div class="fs-lstat">
          <el-icon><Timer></Timer></el-icon>
          <div><b>{{ fmtDur(l.duration) }}</b><span>总时长</span></div>
        </div>
      </div>
      <div class="fs-libmeta">
        <el-icon><Clock></Clock></el-icon>
        <span>{{ fmtAgo(l.last_scan_at) }}索引</span>
      </div>
      <div class="fs-libft">
        <el-button size="small" type="primary" @click="askScan(l)"
          title="扫描这个库：抽台词、封面、动图（会先弹设置确认）">
          <el-icon><RefreshRight></RefreshRight></el-icon><span>索引</span></el-button>
        <el-button size="small" @click="askUpload(l)" title="传视频进这个库，传完自动进素材页">
          <el-icon><Upload></Upload></el-icon><span>上传</span></el-button>
        <el-button size="small" v-if="l.ignored" @click="askIgnored(l)"
          :title="'这个库里有 ' + l.ignored + ' 个文件被标为忽略（不入库）。点开可以逐个放回'">
          <el-icon><Document></Document></el-icon><span>忽略 {{ l.ignored }}</span></el-button>
        <el-button size="small" type="danger" plain @click="delLib(l)"
          title="把这个库从登记里移除。只删索引，不动磁盘上的视频文件">
          <el-icon><Delete></Delete></el-icon><span>删除</span></el-button>
      </div>
    </el-card>
    <div v-if="!libs.length" class="dim pad">还没有素材库，在上面填路径登记一个</div>
  </div>

  <!-- 索引设置 -->
  <el-dialog v-model="dlg.scan" :title="dlgScanTitle" width="560px">
    <el-checkbox v-model="scanOpt.asr" label="无字幕时用 OCR 识别画面"/>
    <div class="fs-hint" v-html="scanHint"></div>
    <el-checkbox v-model="scanOpt.retry" label="重新补一遍没有台词的素材"/>
    <div class="fs-hint">上次没拿到台词的，这次会再来一次。已经拿到台词的不会重跑。</div>
    <el-checkbox v-model="scanOpt.gif" label="生成动图预览"/>
    <div class="fs-hint">列表缩略图悬停时播放的小动图；关掉索引会快不少。</div>
    <div class="fs-hint" v-if="scanTarget">目录：{{ scanTarget.path }}</div>
    <template #footer>
      <el-button @click="dlg.scan = false">取消</el-button>
      <el-button id="mGo" type="primary" @click="doScan">开始索引</el-button>
    </template>
  </el-dialog>

  <!-- 上传 -->
  <el-dialog v-model="dlg.upload" :title="dlgUploadTitle" width="560px">
    <el-upload drag :auto-upload="false" :on-change="onPickFiles" multiple
               accept="video/*" :file-list="uploadFiles" class="fs-upload">
      <el-icon class="fs-upload-icon"><UploadFilled></UploadFilled></el-icon>
      <div class="el-upload__text">拖拽视频到这里，或 <em>点击选择</em></div>
      <template #tip><div class="el-upload__tip">落到 {{ scanTarget ? scanTarget.path : '' }}\\.frameseek\\uploads</div></template>
    </el-upload>
    <div class="fs-hint" v-if="uploadMsg">{{ uploadMsg }}</div>
    <template #footer>
      <el-button @click="dlg.upload = false">关闭</el-button>
      <el-button type="primary" :disabled="!uploadFiles.length" @click="doUpload">上传</el-button>
    </template>
  </el-dialog>

  <!-- 忽略名单 -->
  <el-dialog v-model="dlg.ignored" :title="dlgIgnoredTitle" width="620px">
    <div class="fs-hint">这些素材被「从库中移除」过，磁盘文件还在。索引时会跳过它们，放回来就会重新入库。</div>
    <div class="fs-iglist">
      <div v-for="r in ignoredRows" :key="r.md5" class="fs-igrow">
        <div class="min0"><b>{{ r.name || r.md5 }}</b><div class="dim">{{ r.rel }}</div></div>
        <el-button size="small" @click="restoreOne(r)" title="取消忽略，下次索引时这个文件会正常入库">放回</el-button>
      </div>
      <div v-if="!ignoredRows.length" class="dim pad">（空）</div>
    </div>
    <template #footer>
      <el-button @click="dlg.ignored = false">关闭</el-button>
      <el-button :disabled="!ignoredRows.length" @click="restoreAll">全部放回（{{ ignoredRows.length }}）</el-button>
    </template>
  </el-dialog>
</div>
`,
  setup() {
    const form = reactive({ path: '', name: '' });
    const dlg = reactive({ scan: false, upload: false, ignored: false });
    const scanOpt = reactive({
      asr: LS.get('use_asr', true),
      gif: LS.get('with_gif', true),
      retry: LS.get('retry_empty', true),
    });
    const target = ref(null);            // 当前操作的库
    const uploadFiles = ref([]);
    const uploadMsg = ref('');
    const ignoredRows = ref([]);

    const dlgScanTitle = computed(() =>
      target.value ? `索引「${target.value.name}」` : '索引全部素材库');
    const dlgUploadTitle = computed(() =>
      target.value ? `上传到「${target.value.name}」` : '上传');
    const dlgIgnoredTitle = computed(() =>
      target.value ? `「${target.value.name}」已忽略 ${ignoredRows.value.length} 个` : '已忽略');

    /* OCR 耗时提示（实测：8 进程 1.5~2.1 倍实时） */
    const scanHint = computed(() => {
      const l = target.value;
      if (!S.ocr.ok) {
        return `<span class="bad">OCR 不可用：${esc(S.ocr.err || '')}</span>`
             + '没有字幕的素材将拿不到台词。';
      }
      if (!l) {
        return '开启后没有字幕的视频会用 <b>OCR 识别画面硬字幕</b>，'
             + '实测 8 进程并行约 1.5~2.1 倍实时（1 小时素材约 30~40 分钟）。';
      }
      const hours = (l.duration || 0) / 3600;
      if (!hours) return '开启后没有字幕的视频会用 OCR 识别画面硬字幕。';
      const est = hours / 1.8;
      const estTxt = est < 1 ? Math.round(est * 60) + ' 分钟' : est.toFixed(1) + ' 小时';
      return `这个库共 ${hours.toFixed(1)} 小时素材，OCR 识别画面硬字幕约 <b>${estTxt}</b>`
        + '（按 1.8 倍实时保守算）。'
        + '<div style="margin-top:5px">台词来源优先级：<b>同名 .srt → 内嵌字幕 → OCR 识别画面</b>，'
        + '前两种是秒级，OCR 最慢。</div>'
        + '<div style="margin-top:3px">OCR 出来的台词会写成视频同名 .srt，改完错字在素材页点'
        + '「重新导入同名 SRT」就行，不用重跑 OCR。</div>'
        + '<div style="margin-top:3px">开跑前会花十几秒标定这部片的字幕位置，'
        + '画面里的台标、片内数字、片头演员表会自动滤掉。</div>';
    });

    async function addLib() {
      const p = (form.path || '').trim();
      if (!p) { toast('先填素材目录路径', 'warning'); return; }
      const r = await post('/api/libraries', { path: p, name: (form.name || '').trim() });
      if (!r.ok) {
        ElementPlus.ElMessageBox.alert(r.err || '目录不存在或无法访问', '登记失败');
        return;
      }
      form.path = ''; form.name = '';
      toast(`已登记「${r.name}」`, 'success');
      loadLibs();
    }

    function askScan(l) {
      target.value = l || null;
      scanOpt.asr = LS.get('use_asr', true);
      scanOpt.gif = LS.get('with_gif', true);
      scanOpt.retry = LS.get('retry_empty', true);
      dlg.scan = true;
    }

    async function doScan() {
      LS.set('use_asr', scanOpt.asr);
      LS.set('with_gif', scanOpt.gif);
      LS.set('retry_empty', scanOpt.retry);
      await post('/api/settings', { use_asr: scanOpt.asr, with_gif: scanOpt.gif });
      const url = target.value
        ? `/api/libraries/${target.value.id}/scan` : '/api/libraries/scan_all';
      await post(url, { use_asr: scanOpt.asr, with_gif: scanOpt.gif,
                        retry_empty: scanOpt.retry });
      dlg.scan = false;
      S.tab = 'libs';
      startPoll();
      toast(scanOpt.asr ? '开始索引（转写较慢，看右下角进度）' : '开始索引');
    }

    function askUpload(l) {
      target.value = l || null;
      uploadFiles.value = []; uploadMsg.value = '';
      dlg.upload = true;
    }
    function onPickFiles(f) { uploadFiles.value.push(f); }
    async function doUpload() {
      if (!uploadFiles.value.length) return;
      const fd = new FormData();
      fd.append('root', (target.value || {}).path || '');
      uploadFiles.value.forEach(f => fd.append('file', f.raw || f));
      uploadMsg.value = '上传中…';
      const r = await api('/api/upload', { method: 'POST', body: fd });
      if (!r.ok) { uploadMsg.value = '失败：' + (r.err || ''); return; }
      uploadMsg.value = '已上传 ' + (r.saved || []).join('、') + '，正在入库…';
      startPoll();
      setTimeout(() => { dlg.upload = false; S.tab = 'libs'; }, 900);
    }

    async function askIgnored(l) {
      target.value = l;
      try { ignoredRows.value = await api(`/api/ignored?root=${encodeURIComponent(l.path)}`); }
      catch (e) { ignoredRows.value = []; }
      dlg.ignored = true;
    }
    async function restoreOne(r) {
      await post('/api/ignored/restore', { root: (target.value || {}).path, md5: r.md5 });
      dlg.ignored = false;
      await loadLibs();
      toast('已放回', 'success');
    }
    async function restoreAll() {
      await post('/api/ignored/restore', { root: (target.value || {}).path, all: true });
      dlg.ignored = false;
      await loadLibs();
      toast('已全部放回，下次索引会重新入库', 'success');
    }

    function delLib(l) {
      ElementPlus.ElMessageBox.confirm(
        `只删项目库 hub.db 里的一条登记记录。<br>`
        + `磁盘上的素材文件、以及该目录下的 .frameseek 索引全部保留；`
        + `重新登记这个文件夹，索引还在。<br><br>`
        + `<span class="dim">${esc(l.path)}</span>`,
        `移除「${l.name}」`,
        { dangerouslyUseHTMLString: true, confirmButtonText: '移除登记',
          cancelButtonText: '取消', type: 'warning' },
      ).then(async () => {
        const r = await post(`/api/libraries/${l.id}/delete`);
        await loadLibs();
        toast(r && r.ok ? `已移除「${l.name}」` : '移除失败，看服务端日志',
              r && r.ok ? 'success' : 'error');
      }).catch(() => {});
    }

    return {
      S, form, dlg, scanOpt, target, uploadFiles, uploadMsg, ignoredRows,
      libs: computed(() => S.libs), hubDb: computed(() => S.hubDb),
      scanTarget: computed(() => target.value),
      dlgScanTitle, dlgUploadTitle, dlgIgnoredTitle, scanHint,
      addLib, askScan, doScan, askUpload, onPickFiles, doUpload,
      askIgnored, restoreOne, restoreAll, delLib,
      fmtDur, fmtAgo,
    };
  },
};

FS.LibsView = LibsView;
})();
