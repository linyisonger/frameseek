/* 拾帧 FrameSeek · 素材库模块（登记/索引/上传/忽略名单）
   库卡片提供「查看素材」直接跳到素材页、并把筛选设成该库；另有「编辑库」
   「打开目录」「库级实时进度/取消」 */
(function () {
'use strict';
const { reactive, computed, ref } = Vue;
const FS = window.FS;
const { S, api, post, toast, esc, fmtDur, fmtAgo, LS, loadLibs, startPoll } = FS;

const LibsView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div>
      <h1>素材库</h1>
      <p>登记本地文件夹，索引出台词与截图，跨库搜台词 / 一键导片段</p>
    </div>
    <div class="fs-tools">
      <el-button @click="askScanAll" type="primary" plain
        title="对所有已登记库重跑索引（按 md5 去重，不重复入库）">
        <el-icon><refresh-right/></el-icon><span>索引全部</span></el-button>
    </div>
  </div>

  <!-- 仪表盘 KPI 磁贴 -->
  <el-row :gutter="14" class="fs-statrow">
    <el-col :xs="24" :sm="12" :md="6">
      <el-card shadow="hover" class="fs-card fs-stat">
        <div class="ic p"><el-icon><FolderOpened/></el-icon></div>
        <div class="body">
          <b>{{ libs.length }}</b>
          <span>已登记素材库</span>
          <span class="delta">已索引 <b>{{ indexedCount }}</b> 个 · 丢失 <b>{{ missingCount }}</b> 个</span>
        </div>
      </el-card>
    </el-col>
    <el-col :xs="24" :sm="12" :md="6">
      <el-card shadow="hover" class="fs-card fs-stat">
        <div class="ic s"><el-icon><VideoCamera/></el-icon></div>
        <div class="body">
          <b>{{ S.totals.videos }}</b>
          <span>素材总数</span>
          <span class="delta up" v-if="S.totals.videos">跨 <b>{{ libs.length }}</b> 库累计</span>
        </div>
      </el-card>
    </el-col>
    <el-col :xs="24" :sm="12" :md="6">
      <el-card shadow="hover" class="fs-card fs-stat">
        <div class="ic w"><el-icon><ChatLineRound/></el-icon></div>
        <div class="body">
          <b>{{ S.totals.lines }}</b>
          <span>台词条目</span>
          <span class="delta">已抽 SRT / OCR 识别入库</span>
        </div>
      </el-card>
    </el-col>
    <el-col :xs="24" :sm="12" :md="6">
      <el-card shadow="hover" class="fs-card fs-stat">
        <div class="ic t"><el-icon><Timer/></el-icon></div>
        <div class="body">
          <b>{{ fmtDur(S.totals.duration) }}</b>
          <span>累计素材时长</span>
          <span class="delta">跨全部已索引库</span>
        </div>
      </el-card>
    </el-col>
  </el-row>

  <!-- 登记表单 -->
  <el-card shadow="never" class="fs-card">
    <el-form label-position="top" class="fs-form" @submit.prevent>
      <el-row :gutter="14">
        <el-col :xs="24" :sm="24" :md="14" :lg="15">
          <el-form-item label="素材文件夹路径">
            <el-input id="libPath" v-model="form.path" placeholder="素材文件夹绝对路径，例如 E:\\视频\\电影" clearable
              title="填一个本地文件夹的绝对路径（最后一层就是要登记的库），登记后用它自己的「索引」按钮抽台词"/>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6" :lg="5">
          <el-form-item label="备注名（可选）">
            <el-input id="libName" v-model="form.name" placeholder="可空" clearable/>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="4" :lg="4" class="fs-form-actions">
          <el-form-item label=" ">
            <el-button id="btnAddLib" type="primary" @click="addLib"
              title="把一个本地文件夹登记成素材库（登记后点「索引」才抽台词）">＋ 登记</el-button>
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>
    <div class="fs-note">
      登记信息存在项目目录 <code>{{ hubDb }}</code> · 同一个文件按 md5 去重，不会重复入库
    </div>
  </el-card>

  <el-divider />

  <div class="fs-libcards" id="libCards">
    <el-card v-for="l in libs" :key="l.id" shadow="hover" class="fs-libcard"
             :class="{ 'is-missing': !l.exists, 'is-indexed': l.exists && l.indexed,
                       'has-task': taskOf(l) }">
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
          <el-tag v-if="taskOf(l)" type="warning" size="small" effect="plain">
            {{ taskLabel(l) }}</el-tag>
        </div>
      </template>

      <div class="fs-libpath" :title="l.path">{{ l.path }}</div>

      <!-- 库级实时进度条：只在这个库的任务还在跑时显示，
           独立于右下角全局浮窗，看板更聚焦 -->
      <div class="fs-libprog" v-if="taskOf(l)">
        <el-progress :percentage="taskPct(l)" :stroke-width="6" :show-text="false"/>
        <div class="fs-libprog-meta">
          <span class="dim">{{ taskMsg(l) }}</span>
          <el-button size="small" link type="danger" @click="cancelScan(l)"
            title="中途停止。已经识别完的素材都会保留，下次索引会跳过它们、从断点接着往下跑">
            停止</el-button>
        </div>
      </div>

      <div class="fs-libstats">
        <div class="fs-lstat">
          <el-icon><VideoCamera/></el-icon>
          <div><b>{{ l.videos }}</b><span>素材</span></div>
        </div>
        <div class="fs-lstat">
          <el-icon><ChatLineRound/></el-icon>
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
        <span v-if="l.videos" class="sep">·</span>
        <span v-if="l.videos">{{ l.videos }} 部影片</span>
        <span v-if="l.ignored" class="sep">·</span>
        <span v-if="l.ignored" class="warn">忽略 {{ l.ignored }} 个</span>
      </div>

      <div class="fs-libft">
        <el-button size="small" type="primary" @click="askScan(l)"
          title="扫描这个库：抽台词、封面、动图（会先弹设置确认）">
          <el-icon><RefreshRight/></el-icon><span>索引</span></el-button>
        <el-button size="small" type="primary" plain @click="viewAssets(l)"
          title="去素材页，并把筛选设成这个库（只看这个库的素材）">
          <el-icon><VideoCamera/></el-icon><span>查看素材</span></el-button>
        <el-button size="small" @click="openDir(l)" title="在系统文件管理器里打开这个库">
          <el-icon><FolderOpened/></el-icon><span>打开目录</span></el-button>
        <el-button size="small" @click="editLib(l)" title="改备注名（库对外显示的名字）或修改路径">
          <el-icon><EditPen/></el-icon><span>编辑</span></el-button>
        <el-button size="small" @click="askUpload(l)" title="传视频进这个库，传完自动进素材页">
          <el-icon><Upload/></el-icon><span>上传</span></el-button>
        <el-button size="small" v-if="l.ignored" @click="askIgnored(l)"
          :title="'这个库里有 ' + l.ignored + ' 个文件被标为忽略（不入库）。点开可以逐个放回'">
          <el-icon><Document></Document></el-icon><span>忽略 {{ l.ignored }}</span></el-button>
        <el-button size="small" type="danger" plain @click="delLib(l)"
          title="把这个库从登记里移除。只删索引，不动磁盘上的视频文件">
          <el-icon><Delete/></el-icon><span>删除</span></el-button>
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

  <!-- 编辑库：备注名 / 路径 -->
  <el-dialog v-model="dlg.edit" :title="dlgEditTitle" width="520px" @closed="resetEditForm">
    <el-form label-position="top" class="fs-form">
      <el-form-item label="备注名">
        <el-input v-model="editForm.name" :placeholder="editForm._oldName"
          clearable/>
        <div class="fs-hint">空值 = 保留原名。要跟磁盘文件夹名不一样、用这个做对外显示的别名。</div>
      </el-form-item>
      <el-form-item label="素材文件夹路径">
        <el-input v-model="editForm.path" :placeholder="editForm._oldPath"
          clearable/>
        <div class="fs-hint">空值 = 保留原路径。新路径必须是已存在的目录，且没被别的库占用。</div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dlg.edit = false">取消</el-button>
      <el-button type="primary" :disabled="!editForm.name && !editForm.path" @click="saveEdit">保存</el-button>
    </template>
  </el-dialog>

  <!-- 上传 -->
  <el-dialog v-model="dlg.upload" :title="dlgUploadTitle" width="560px">
    <el-upload drag :auto-upload="false" :on-change="onPickFiles" multiple
               accept="video/*" :file-list="uploadFiles">
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
    const dlg = reactive({ scan: false, upload: false, ignored: false, edit: false });
    const scanOpt = reactive({
      asr: LS.get('use_asr', true),
      gif: LS.get('with_gif', true),
      retry: LS.get('retry_empty', true),
    });
    const target = ref(null);
    const uploadFiles = ref([]);
    const uploadMsg = ref('');
    const ignoredRows = ref([]);

    /* 编辑对话框表单 */
    const editForm = reactive({ name: '', path: '', _oldName: '', _oldPath: '' });

    const dlgScanTitle = computed(() =>
      target.value ? `索引「${target.value.name}」` : '索引全部素材库');
    const dlgUploadTitle = computed(() =>
      target.value ? `上传到「${target.value.name}」` : '上传');
    const dlgIgnoredTitle = computed(() =>
      target.value ? `「${target.value.name}」已忽略 ${ignoredRows.value.length} 个` : '已忽略');
    const dlgEditTitle = computed(() =>
      target.value ? `编辑「${target.value.name}」` : '编辑素材库');

    /* OCR 耗时提示 */
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

    /* 查看素材：跳到素材页，并把筛选设成这个库。
       用 URL 参数（#assets?lib=<id>）当唯一信息源：assets.js 认 lib 参数做库筛选。
       pushState 而非 replace —— 用户主动发起的导航，"后退"应该回素材库。 */
    function viewAssets(l) {
      FS.Route.set('assets', { lib: String(l.id) }, false);
    }

    /* 库级实时进度：S.tasks 是 reactive object，key=root，value 含 kind/done/total/phase/message/file */
    function taskOf(l) { return l && l.path && S.tasks && S.tasks[l.path]; }
    function taskPct(l) {
      const t = taskOf(l);
      if (!t) return 0;
      return t.total ? Math.min(100, Math.round((t.done || 0) / t.total * 100)) : 0;
    }
    function taskMsg(l) {
      const t = taskOf(l);
      if (!t) return '';
      const bits = [];
      if (t.phase === 'ocr') bits.push('OCR 识别');
      else if (t.phase === 'shots') bits.push('截图分片');
      else if (t.phase === 'calib') bits.push('字幕标定');
      if (t.total) bits.push(`第 ${Math.min((t.done || 0) + 1, t.total)} / ${t.total} 部`);
      if (t.file) bits.push('· ' + t.file);
      if (t.frames) bits.push(`已识别 ${t.frames.toLocaleString()} 帧`);
      return bits.filter(Boolean).join('  ');
    }
    function taskLabel(l) {
      const t = taskOf(l);
      if (!t) return '';
      if (t.total) return `索引中 ${taskPct(l)}%`;
      if (t.phase === 'calib') return '字幕标定中';
      if (t.phase === 'shots') return '截图中';
      if (t.phase === 'ocr') return 'OCR 识别中';
      return '处理中';
    }
    async function cancelScan(l) {
      try {
        await ElementPlus.ElMessageBox.confirm(
          '正在停止后已经识别完的素材都保留，下次点「索引」接着往下跑。'
          + '<br><br><span class="dim">' + esc(l.path) + '</span>',
          `停止「${l.name}」的索引`,
          { dangerouslyUseHTMLString: true, confirmButtonText: '停止',
            cancelButtonText: '继续跑', type: 'warning' });
      } catch (e) { return; }
      const r = await post('/api/scan/stop', { root: l.path });
      toast(r.ok ? '正在停止…' : (r.err || '停止失败'), r.ok ? 'warning' : 'error');
    }

    /* 打开目录 */
    async function openDir(l) {
      try {
        const r = await post(`/api/libraries/${l.id}/open`, {});
        if (!r.ok) {
          toast('打开失败：' + (r.err || ''), 'error');
        }
      } catch (e) {
        toast('打开失败：' + (e.message || e), 'error');
      }
    }

    /* 编辑库 */
    function editLib(l) {
      target.value = l;
      editForm.name = '';
      editForm.path = '';
      editForm._oldName = l.name;
      editForm._oldPath = l.path;
      dlg.edit = true;
    }
    function resetEditForm() {
      editForm.name = ''; editForm.path = '';
      editForm._oldName = ''; editForm._oldPath = '';
    }
    async function saveEdit() {
      if (!target.value) return;
      const payload = {};
      if (editForm.name && editForm.name.trim()) payload.name = editForm.name.trim();
      if (editForm.path && editForm.path.trim()) payload.path = editForm.path.trim();
      if (!Object.keys(payload).length) { dlg.edit = false; return; }
      const r = await post(`/api/libraries/${target.value.id}/edit`, payload);
      if (!r.ok) { toast('保存失败：' + (r.err || ''), 'error'); return; }
      dlg.edit = false;
      await loadLibs();
      toast('已保存', 'success');
    }

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

    /* 索引全部：弹索引对话框，target=null 时 doScan 会走 /api/libraries/scan_all */
    function askScanAll() { askScan(null); }

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

    /* 库列表由 S.libs 驱动：服务端在任务结束等时机调用 loadLibs() 刷新，
       S.libs 变化会自动重渲染，这里无需再挂 assets:refresh 监听。 */

    return {
      S, form, dlg, scanOpt, target, uploadFiles, uploadMsg, ignoredRows,
      editForm,
      libs: computed(() => S.libs), hubDb: computed(() => S.hubDb),
      scanTarget: computed(() => target.value),
      indexedCount: computed(() => S.libs.filter(l => l.exists && l.indexed).length),
      missingCount: computed(() => S.libs.filter(l => !l.exists).length),
      dlgScanTitle, dlgUploadTitle, dlgIgnoredTitle, dlgEditTitle, scanHint,
      addLib, askScan, doScan, askUpload, onPickFiles, doUpload,
      askScanAll,
      askIgnored, restoreOne, restoreAll, delLib,
      editLib, saveEdit, resetEditForm,
      openDir, viewAssets,
      cancelScan, taskOf, taskPct, taskMsg, taskLabel,
      fmtDur, fmtAgo,
    };
  },
};

FS.LibsView = LibsView;
})();
