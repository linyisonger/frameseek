/* 拾帧 FrameSeek · 素材模块（跨库浏览、筛选、重识别/导入/删除） */
(function () {
'use strict';
const { reactive, computed, ref, watch, onMounted, onUnmounted, onActivated, nextTick } = Vue;
const FS = window.FS;
const { S, BUS, api, post, toast, play, mediaUrl, fmtDur, loadLibs, startPoll, esc, hdUrl } = FS;
const Route = FS.Route;

const PAGE = 48;   // 滚动分页每页条数

/* URL 参数 ⇄ 筛选条件 的唯一转换口。别的文件只管调 Route.set('assets', 参数)，
   这里负责把参数翻译成 kw/libSel/durSel/sort，反过来也从当前筛选拼回参数。
   参数集（全部可选，缺省即不筛）：
     lib  逗号分隔的库 id，可多选（0 = AI 生成）
     q    文件名 / 库名关键词
     dur  逗号分隔的时长档 s30,m30,m5_20,l20
     sort time | mtime | name | lib
     vid  + root：精确锁定某一部影片（带 vid 的分享链接直达时用） */
const URL_DUR = ['s30', 'm30', 'm5_20', 'l20'];
const URL_SORT = ['time', 'mtime', 'name', 'lib'];

function paramsFromUrl(q) {
  const libs = String(q.lib || '').split(',')
    .map(x => x.trim()).filter(x => x !== '' && /^\d+$/.test(x));
  const dur = String(q.dur || '').split(',')
    .map(x => x.trim()).filter(x => URL_DUR.indexOf(x) >= 0);
  const sort = URL_SORT.indexOf(String(q.sort || '')) >= 0 ? q.sort : 'time';
  const vid = /^\d+$/.test(String(q.vid || '')) ? +q.vid : null;
  return {
    libs, dur, sort, kw: String(q.q || '').trim(),
    vid, root: String(q.root || ''),
  };
}
function urlFromState(st) {
  const q = {};
  const libs = st.libs && st.libs.length ? st.libs.map(String).join(',') : '';
  if (libs) q.lib = libs;
  if (st.kw) q.q = st.kw;
  if (st.dur && st.dur.length) q.dur = st.dur.join(',');
  if (st.sort && st.sort !== 'time') q.sort = st.sort;
  if (st.vid) { q.vid = st.vid; if (st.root) q.root = st.root; }
  return q;
}

/* 时长分档命中（纯函数，供 rowHit 用） */
function durHit(d, dur) {
  return (dur.has('s30') && d > 0 && d < 30)
      || (dur.has('m30') && d >= 30 && d < 300)
      || (dur.has('m5_20') && d >= 300 && d < 1200)
      || (dur.has('l20') && d >= 1200);
}

/* ---------- 素材命中判定（纯函数，可独立测试） ---------- */
/* 精确锁定：命中同 id，且（没带 root，或 root 相同）。
   必须成对使用——实测库里 60 个 video id 跨库撞号（id=1 同时存在于
   电影 / 电视剧 / AI生成三个库），只比 id 会一次放出好几部不相干的片子。 */
function pinHit(r, pin) {
  if (!pin || !pin.vid) return true;
  if (+r.id !== +pin.vid) return false;
  return !pin.root || r.root === pin.root;
}

/* 主过滤：锁定 → 库 → 时长 → 关键词，任一不中就淘汰。
   opts = { pin, libs:Set<number>, dur:Set<string>, kw:string(已小写) } */
function rowHit(r, o) {
  if (!pinHit(r, o.pin)) return false;
  if (o.libs && o.libs.size && !o.libs.has(+(r.lib_id || 0))) return false;
  if (o.dur && o.dur.size && !durHit(+(r.duration || 0), o.dur)) return false;
  if (o.kw && !((r.name || '') + ' ' + (r.rel || '') + ' ' + (r.lib || ''))
                  .toLowerCase().includes(o.kw)) return false;
  return true;
}

const AssetsView = {
  template: `
<div class="fs-page">
  <div class="fs-page-hd">
    <div><h1>素材 <span class="dim">({{ viewCount }})</span></h1>
      <p>点卡片预览，右上角 ··· 可重新识别 / 导入字幕 / 移除 / 删文件</p></div>
    <div class="fs-tools">
      <el-input id="vfilter" v-model="kw" size="small" class="mini" placeholder="过滤文件名或库名…" clearable
        title="按文件名或库名过滤，边打边生效（同时作用于 AI 生成图片）"/>
      <el-select id="vSort" v-model="sort" size="small" class="w140" title="排序方式">
        <el-option label="最新录入" value="time"/>
        <el-option label="文件时间" value="mtime"/>
        <el-option label="按名称" value="name"/>
        <el-option label="按库" value="lib"/>
      </el-select>
      <el-button id="vMoreBtn" size="small" @click="showFilter = !showFilter"
        title="多维筛选：素材库可多选、按时长分档">
        {{ showFilter ? '筛选 ▴' : '筛选 ▾' }}</el-button>
      <el-button id="vResetBtn" size="small" @click="resetFilters" title="清空所有筛选条件，回到完整列表">重置</el-button>
      <el-button id="vCopyBtn" size="small" @click="copyLink"
        title="把当前这套筛选（素材库 / 关键词 / 时长档 / 锁定影片）复制成链接，发给别人或存书签，打开就是同一屏结果">
        <el-icon><Link/></el-icon><span>复制筛选链接</span></el-button>
    </div>
  </div>

  <!-- URL 带 vid 时的锁定提示条 -->
  <el-alert v-if="pin.vid" type="primary" :closable="false" show-icon class="fs-pinalert">
    <template #title>
      <span class="fs-pinbar">
        <el-icon><Aim/></el-icon>
        只看「{{ pin.name || '这一部影片' }}」· 地址栏已带上 vid={{ pin.vid }}，分享出去打开就是这一屏
        <el-button size="small" @click="unpin" title="解除锁定，回到全部素材">看全部</el-button>
      </span>
    </template>
  </el-alert>

  <el-divider />

  <el-card v-show="showFilter" shadow="never" class="fs-card fs-filtercard" id="vFilterCard">
    <el-form label-position="top" class="fs-form" @submit.prevent>
      <el-form-item label="素材库">
        <div class="fs-fchips" id="fLibs">
          <el-checkbox v-model="libsAll" label="全部" size="small"/>
          <el-checkbox v-for="l in allLibs" :key="l.id" v-model="libSel[l.id]" size="small"
                       :title="l.path">{{ l.name }}<template v-if="l.builtin"> 🔒</template><b class="dim">{{ l.videos }}</b></el-checkbox>
        </div>
      </el-form-item>
      <el-form-item label="时长分档">
        <el-checkbox-group v-model="durSel" size="small" class="fs-fchips" id="fDur">
          <el-checkbox label="s30" value="s30">30 秒内</el-checkbox>
          <el-checkbox label="m30" value="m30">30 秒 - 5 分</el-checkbox>
          <el-checkbox label="m5_20" value="m5_20">5 - 20 分</el-checkbox>
          <el-checkbox label="l20" value="l20">20 分以上</el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <div class="fs-frow fs-fsum dim" id="fSummary">{{ summary }}</div>
      <div class="fs-frow fs-fsum dim" id="fUrlRow" v-if="curQuery">
        <span>当前链接参数</span>
        <code class="fs-urlcode" :title="'可直接复制：' + shareUrl">{{ curQuery }}</code>
      </div>
    </el-form>
  </el-card>

  <!-- 统一网格：已索引视频 + AI 生成产物（图片/未索引视频）混排，不再单独开分区 -->
  <div class="fs-videos" id="videos">
    <template v-for="row in view" :key="rowKey(row)">
      <!-- 已索引视频（含内置 AI 生成视频）：完整卡片 + ··· 菜单 -->
      <div v-if="row.kind === 'video'" class="fs-vcard" :title="row.v.name">
        <div class="fs-vthumb">
          <img :src="coverUrl(row.v)" loading="lazy"
               @mouseenter="hoverGif($event, row.v, true)" @mouseleave="hoverGif($event, row.v, false)"
               @click="preview(row.v)"/>
          <div class="fs-vgrad"></div>
          <span class="fs-vdur">{{ fmtDur(row.v.duration) }}</span>
          <div class="fs-vplay" @click="preview(row.v)">
            <el-icon><VideoPlay></VideoPlay></el-icon>
            <span>预览</span>
          </div>
        </div>
        <el-dropdown trigger="click" placement="bottom-end" @command="cmd => onCmd(cmd, row.v)">
          <el-button class="fs-vmore" size="small" text
            :title="row.v.builtin ? '更多操作：打开文件夹 / 从库里移除 / 删除文件（AI 生成产物不做字幕识别）'
                             : '更多操作：重新识别台词 / 导入字幕 / 打开文件夹 / 移除或删除'">
            <el-icon><MoreFilled></MoreFilled></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="preview" title="在右侧播放器里预览这部片子">预览</el-dropdown-item>
              <el-dropdown-item command="dir" title="在资源管理器里打开这个视频所在的文件夹">打开所在文件夹</el-dropdown-item>
              <template v-if="!row.v.builtin">
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
          <b :title="row.v.name">
            <span class="fs-vdir" v-if="dirOf(row.v)">{{ dirOf(row.v) }}/</span>{{ row.v.name }}
          </b>
          <div class="fs-vsub">
            <i class="fs-dot" :class="{ ai: row.v.builtin, err: row.v.err, ok: !row.v.builtin && row.v.line_count }"></i>
            <span class="fs-vlib">{{ row.v.lib }}</span>
            <span class="fs-vai" v-if="row.v.builtin">· AI 生成</span>
            <span class="fs-vline" v-else-if="row.v.line_count">· {{ row.v.line_count.toLocaleString() }} 条台词</span>
            <span class="fs-vline" v-else-if="row.v.err">· 识别失败</span>
            <span class="fs-vline" v-else-if="row.v.line_source === 'pending'">· 待识别</span>
            <span class="fs-vline" v-else>· 无台词</span>
          </div>
        </div>
      </div>

      <!-- AI 生成产物（图片 / 未进索引的视频）：简化卡片，点开看大图或播放 -->
      <div v-else class="fs-vcard fs-aicard" :title="row.it.name">
        <div class="fs-vthumb">
          <img v-if="row.it.type === 'image'" class="fs-vimg" :src="hdUrl(row.it.file)" loading="lazy" @click="openPic(row.it)"/>
          <video v-else class="fs-vvid" :src="hdUrl(row.it.file)" preload="metadata" muted playsinline
                 @click="openVid(row.it)"></video>
          <span class="fs-vtype" :class="row.it.type">{{ row.it.type === 'image' ? '图' : '▷' }}</span>
          <el-button class="fs-aicard-del" size="small" circle type="danger"
            @click="delAi(row.it)" title="删除源文件（不可恢复）"><el-icon><Delete/></el-icon></el-button>
        </div>
        <div class="fs-vmeta">
          <b :title="row.it.name">{{ row.it.name }}</b>
          <div class="fs-vsub">
            <span class="fs-vlib">AI 生成</span>
            <span class="fs-vline">{{ fmtAgo(row.it.mtime) }}</span>
            <span class="fs-vline" v-if="row.it.rel && row.it.rel !== row.it.name">· {{ row.it.rel }}</span>
          </div>
        </div>
      </div>
    </template>

    <div v-if="!view.length" class="dim pad">
      {{ S.all.length ? '没有符合条件的素材，试试调整筛选或清空关键词'
                      : '还没有素材，先去「素材库」登记并索引' }}
    </div>

    <!-- 滚动分页：接近底部自动加载下一页 -->
    <div ref="sentinel" v-show="!allDone" class="fs-loadmore dim">
      <el-icon class="is-loading" v-if="loadingMore"><Loading></Loading></el-icon>
      {{ loadingMore ? '加载中…' : '滚动加载更多' }}
    </div>
    <div v-if="allDone && view.length" class="fs-loadmore dim">—— 已经到底了 ——</div>
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
    <el-form label-position="top" class="fs-form" @submit.prevent>
      <el-form-item label="识别模型">
        <el-select id="asrModel" v-model="asrOpt.model" size="small" class="w160">
          <el-option v-for="m in ['tiny','base','small','medium']" :key="m"
                     :label="m + (m === (asrOpt.info || {}).default_model ? '（默认）' : '')" :value="m"/>
        </el-select>
      </el-form-item>
      <div class="fs-hint">small 准确、tiny 快 3 倍但错字多；首次用某个模型要先下载（几百 MB）</div>
    </el-form>
    <div class="fs-hint">识别设备：{{ asrEta }}（粗估，进度看右下角浮窗）。
      转写完会在视频同目录生成同名 .srt，可改完再「重新导入」。</div>
    <template #footer>
      <el-button @click="dlg.asr = false">取消</el-button>
      <el-button type="primary" @click="doAsr">开始语音识别</el-button>
    </template>
  </el-dialog>

  <!-- AI 生成素材：图片大图 / 视频播放 -->
  <el-dialog v-model="picOn" width="62%" top="5vh" destroy-on-close class="fs-player-dlg">
    <img v-if="pic" :src="hdUrl(pic.file)" style="width:100%;display:block"/>
    <template #footer><span class="dim">{{ pic && pic.name }}</span>
      <el-button @click="picOn = false">关闭</el-button></template>
  </el-dialog>
  <el-dialog v-model="vidOn" width="62%" top="5vh" destroy-on-close class="fs-player-dlg">
    <video v-if="vid" :src="hdUrl(vid.file)" style="width:100%;display:block" controls autoplay></video>
    <template #footer><span class="dim">{{ vid && vid.name }}</span>
      <el-button @click="vidOn = false">关闭</el-button></template>
  </el-dialog>
</div>
`,
  setup() {
    const kw = ref('');
    const sort = ref('time');                // 排序：time=最新录入(created_at) / mtime=文件修改时间 / name / lib
    const aiItems = ref([]);                 // AI 生成产物（图片 + 未在索引里的视频），统一并入主网格
    // 纯前端分页：一次拉全量，在客户端合并 + 筛选 + 排序后再按 limit 切片。
    // 只有这样才能让 AI 产物按时间真正插进视频流，而不是永远吊在最后面。
    const limit = ref(PAGE);
    const loadingMore = ref(false);
    const sentinel = ref(null);
    let io = null;
    // primed：首屏 reload 是否已完成。未完成前哨兵若已进视口也别加载，
    // 否则哨兵会在数据还没到位时提前触发一次（列表出现重复项、key 重复报 Vue 警告）。
    let primed = false;
    // 追加后若哨兵仍在视口内（屏幕很高 / 一页撑不满），要继续补，否则分页会卡住不动
    function sentinelNear() {
      const el = sentinel.value;
      if (!el) return false;
      const r = el.getBoundingClientRect();
      const vh = (window.innerHeight || document.documentElement.clientHeight || 0);
      return r.top < vh + 300;
    }

    function openPic(it) { pic.value = it; picOn.value = true; }
    function openVid(it) { vid.value = it; vidOn.value = true; }
    async function delAi(it) {
      try {
        await ElementPlus.ElMessageBox.confirm(
          `将删除磁盘上的源文件（及同名参数留痕），<b>不可恢复</b>：<br>`
          + `<code>${esc(it.name)}</code>`,
          '删除 AI 生成素材', { dangerouslyUseHTMLString: true, type: 'warning',
            confirmButtonText: '删除' });
      } catch (e) { return; }
      const r = await post('/api/ai-gallery/delete', { file: it.file });
      if (!r || !r.ok) { toast('删除失败：' + ((r && r.err) || ''), 'error'); return; }
      await loadAi();
      toast('已删除：' + it.name, 'success');
    }
    async function loadAi() {
      try {
        const arr = await api('/api/ai-gallery');
        aiItems.value = Array.isArray(arr) ? arr : [];
      } catch (e) { aiItems.value = []; }
    }
    const pic = ref(null), picOn = ref(false);
    const vid = ref(null), vidOn = ref(false);
    const showFilter = ref(false);
    const libSel = reactive({});          // { libId: bool }
    const durSel = ref([]);               // 时长分档
    // 精确锁定某一部影片（URL 带 vid 时）。
    // 比"按文件名过滤"可靠得多：多库里同名片子（01.mp4）也能只命中一部。
    // name 记一份是为了判断"用户是不是自己改了关键词"——改了就不该继续锁着。
    const pin = reactive({ vid: null, root: '', name: '' });

    const allLibs = computed(() => (S.libs || []).concat(S.builtinLibs || []));

    // 「全部」勾上 = 不设库条件；勾其它项时自动取消「全部」
    const libsAll = computed({
      get: () => Object.values(libSel).every(v => !v),
      set: v => { if (v) Object.keys(libSel).forEach(k => { libSel[k] = false; }); },
    });

    const libSet = computed(() => new Set(
      Object.keys(libSel).filter(k => libSel[k]).map(Number)));

    // 全量合并：已索引视频（含内置 AI 视频）+ AI 生成产物（图片 / 未索引视频）
    const merged = computed(() => {
      const f = (kw.value || '').trim().toLowerCase();
      const libs = libSet.value, dur = new Set(durSel.value);
      const pinId = pin.vid;
      const out = [];
      for (const r of S.all) {
        if (!rowHit(r, { pin, libs, dur, kw: f })) continue;
        // 排序基准：created_at = 入库（录入）时间；mtime = 文件在磁盘上的修改时间。
        // 两个差很多——文件可能是几年前的老片，但昨天才录进库，所以默认按录入时间排。
        out.push({ kind: 'video', v: r,
                   t: +(r.created_at || r.mtime || 0), m: +(r.mtime || r.created_at || 0) });
      }
      // AI 生成产物：仅当「全部」或勾选「AI 生成」(id 0) 时显示；有时长筛选时不显示（产物无时长）
      // 锁定单部影片时当然也不显示——用户要看的就是那一部。
      const aiVisible = !pinId && !dur.size && (libs.size === 0 || libs.has(0));
      if (aiVisible) {
        // 已作为内置视频进网格的 AI 成片，按路径去重，避免重复
        const builtinPaths = new Set(
          S.all.filter(r => r.builtin)
              .map(r => (r.root + '/' + (r.rel || '')).replace(/\\/g, '/').toLowerCase()));
        for (const it of aiItems.value) {
          if (f && !((it.name || '') + ' ' + (it.rel || '')).toLowerCase().includes(f)) continue;
          if (it.type === 'video'
              && builtinPaths.has((it.file || '').replace(/\\/g, '/').toLowerCase())) continue;
          // AI 产物是刚生成的文件，生成时间 ≈ 录入时间，没有单独的入库时间
          out.push({ kind: 'ai', it, t: +(it.mtime || 0), m: +(it.mtime || 0) });
        }
      }
      const by = sort.value;
      const keyOf = r => (by === 'mtime' ? r.m : r.t);
      out.sort((a, b) => {
        if (by === 'name') return nameOf(a).localeCompare(nameOf(b), 'zh');
        if (by === 'lib') return (libOf(a) || '').localeCompare(libOf(b) || '', 'zh')
                               || nameOf(a).localeCompare(nameOf(b), 'zh');
        return keyOf(b) - keyOf(a);   // 时间倒序：最新的排最前
      });
      return out;
    });

    function nameOf(row) { return ((row.kind === 'video' ? row.v.name : row.it.name) || '').toLowerCase(); }
    function libOf(row) { return row.kind === 'video' ? (row.v.lib || '') : 'AI 生成'; }

    // 当前这一页（滚动时 limit 递增，纯客户端切片）
    const view = computed(() => merged.value.slice(0, limit.value));
    const allDone = computed(() => limit.value >= merged.value.length);

    function rowKey(row) {
      return row.kind === 'video' ? (row.v.root + '#' + row.v.id) : ('ai-' + (row.it.file || row.it.name));
    }

    const summary = computed(() => {
      const bits = [];
      if (libSet.value.size) {
        bits.push('库：' + [...libSet.value].map(id => {
          const l = allLibs.value.find(x => +x.id === +id);
          return l ? l.name : id;
        }).join('、'));
      }
      if (durSel.value.length) bits.push('时长：' + durSel.value.join('/'));
      return `命中 ${merged.value.length} / 共 ${S.all.length + aiItems.value.length} 条`
           + (bits.length ? ' · ' + bits.join(' · ') : ' · 未设条件');
    });

    // 纯前端分页：一次拉全量，筛选与排序都在客户端做，所以不带任何查询参数
    function videosUrl() { return '/api/videos?scope=all'; }

    // 拉全量（进页面 / 刷新事件 / 重新识别后）
    async function reload() {
      primed = false;
      try {
        const data = await api(videosUrl());
        S.all = Array.isArray(data) ? data : (data.items || []);
      } catch (e) { S.all = []; }
      await loadAi();   // 始终拉取 AI 生成产物（图片 + 未在索引里的视频），统一并入主网格
      primed = true;
      limit.value = PAGE;
      await nextTick();
      if (!allDone.value && sentinelNear()) loadMore();
    }

    // 滚动到底：多显示一页（纯客户端切片，不再发请求）
    async function loadMore() {
      if (!primed || allDone.value || loadingMore.value) return;
      loadingMore.value = true;
      limit.value = Math.min(limit.value + PAGE, merged.value.length);
      await nextTick();
      loadingMore.value = false;
      // 屏幕很高 / 一页撑不满时哨兵仍在视口内，IO 不会重复触发 → 继续补
      if (!allDone.value && sentinelNear()) loadMore();
    }

    // 切换页签会重新挂载，监听必须跟着解绑，否则回调会越积越多
    const offRefresh = BUS.on('assets:refresh', reload);
    onUnmounted(offRefresh);

    // 从 URL 参数还原筛选（首屏直达 / 前进后退 / 别人发来的链接都走这里）
    function applyFromUrl(q) {
      clearFilters(false);
      const p = paramsFromUrl(q || {});
      kw.value = p.kw;
      p.libs.forEach(id => { libSel[id] = true; });
      durSel.value = p.dur;
      sort.value = p.sort;
      pin.vid = p.vid; pin.root = p.root; pin.name = p.kw;
      showFilter.value = !!(p.libs.length || p.dur.length || p.vid);
      limit.value = PAGE;
    }

    function clearFilters(clearKw) {
      Object.keys(libSel).forEach(k => { libSel[k] = false; });
      durSel.value = [];
      pin.vid = null; pin.root = ''; pin.name = '';
      if (clearKw) kw.value = '';
    }

    // 当前筛选 → URL 参数（用 replace：筛选是连续微调，不该塞满历史栈）
    let syncing = false;
    function syncUrl() {
      if (syncing) return;
      syncing = true;
      Route.set('assets', urlFromState({
        libs: [...libSet.value],
        kw: (kw.value || '').trim(),
        dur: durSel.value,
        sort: sort.value,
        vid: pin.vid,
        root: pin.root,
      }), true);
      syncing = false;
    }

    /* URL → 筛选。App 的 onMounted 里 Route.init() 可能在 assets 组件还没
       创建时就广播过了（首屏直达链接就是这种情况），所以组件自己也要主动读一次
       location.hash，不能只靠 BUS。 */
    function hasFilter() {
      return !!(Object.values(libSel).some(Boolean) || durSel.value.length
              || (kw.value || '').trim() || pin.vid);
    }
    function consumeUrl() {
      const q = Route.parse().q;
      if (Object.keys(q).length) { applyFromUrl(q); return; }
      // URL 是空的但页面还留着筛选（切去别的页签再切回来）→ 把状态补回 URL，
      // 保证"地址栏复制出去的链接"和屏幕上看到的一致。
      if (hasFilter()) syncUrl();
    }
    onMounted(consumeUrl);
    onActivated(consumeUrl);

    /* URL → 筛选：首屏带参数直达、前进/后退、地址栏手改参数都走这条。
       只在本页激活时消费，避免在别的页签也去动筛选状态。 */
    const offRoute = BUS.on('route:change', (r) => {
      if (!r || r.tab !== 'assets') return;
      if (S.tab !== 'assets') return;
      if (r.hard) { applyFromUrl(r.q); return; }   // 外部来的（刷新/前进后退）
      // 内部 set 造成的广播：状态已经是自己设的，别再重置一遍
    });
    onUnmounted(offRoute);

    // 筛选/排序变化：纯客户端重算，只需把分页游标拨回第一页 + 同步 URL
    watch([libSet, durSel, kw, sort], () => {
      limit.value = PAGE;
      // 用户自己改了关键词，说明"只想看这部"的前提没了 → 解除精确锁定，
      // 否则他输入的字会被 vid 白名单挡掉，看起来像"搜索失灵"。
      if (pin.vid && (kw.value || '').trim() !== pin.name) {
        pin.vid = null; pin.root = ''; pin.name = '';
      }
      syncUrl();
    });
    // pin 变化也要拨分页游标（applyFromUrl 里已经拨过，这里兜底保险）
    watch(() => pin.vid, () => { limit.value = PAGE; });
    // 侧栏徽标显示的是命中条数（不只是当前已切片的那部分）
    watch(merged, m => { S.viewCount = m.length; }, { immediate: true });

    function resetFilters() {
      clearFilters(true);
      // 纯前端筛选：清条件即可，watch 会自动把分页游标拨回第一页 + 同步 URL
    }

    function unpin() {
      pin.vid = null; pin.root = ''; pin.name = '';
      limit.value = PAGE;
      syncUrl();
    }

    /* 复制当前筛选链接。放在标题栏旁边——素材页的筛选组合往往挺复杂
       （3 个库 + 时长档 + 关键词），手拼参数不现实，发给别人看更不可能。 */
    async function copyLink() {
      syncUrl();
      const url = location.href;
      try {
        await navigator.clipboard.writeText(url);
        toast('筛选链接已复制，可直接发给同事', 'success');
      } catch (e) {
        // 非 https / 无剪贴板权限时退回弹窗，至少让人能手动复制
        ElementPlus.ElMessageBox.alert(
          `<div style="word-break:break-all">${esc(url)}</div>`,
          '当前筛选的链接（请手动复制）', { dangerouslyUseHTMLString: true });
      }
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

    // 滚动分页：sentinel 进入视口附近就加载下一页（兼容窗口/容器滚动）
    onMounted(() => {
      if (typeof IntersectionObserver === 'undefined') return;
      io = new IntersectionObserver(entries => {
        if (entries.some(e => e.isIntersecting)) loadMore();
      }, { rootMargin: '300px 0px' });
      if (sentinel.value) io.observe(sentinel.value);
    });
    onUnmounted(() => { if (io) { io.disconnect(); io = null; } });

    return {
      // S 必须回传：模板里的空态文案要读 S.all（库里到底有没有素材），
      // 不回传时模板拿不到 S，Vue 抛 "Cannot read properties of undefined (reading 'all')"，
      // 整个组件渲染中断 —— 表现是「筛到一条不剩时整页空白」而不是显示空态提示。
      S,
      kw, sort, aiItems, openPic, openVid, delAi, pic, picOn, vid, vidOn, showFilter, libSel, durSel,
      allDone, loadingMore, sentinel, pin,
      allLibs, libsAll, view, rowKey, summary, picking, dlg, reOpt, asrOpt, asrEta,
      resetFilters, unpin, copyLink, onCmd, doRetranscribe, doAsr,
      viewCount: computed(() => merged.value.length),
      // 筛选面板底部的"当前链接参数"，让用户看得见地址栏被写成了什么样
      curQuery: computed(() => {
        const s = new URLSearchParams(urlFromState({
          libs: [...libSet.value],
          kw: (kw.value || '').trim(),
          dur: durSel.value,
          sort: sort.value,
          vid: pin.vid,
          root: pin.root,
        })).toString();
        return s ? '#assets?' + s : '';
      }),
      shareUrl: computed(() => location.origin + location.pathname + location.hash),
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
      fmtDur, fmtAgo: FS.fmtAgo, hdUrl,
      // 二级目录下的片子，片名加目录前缀，否则满屏「01.mp4 / 02.mp4」分不清是哪部的
      dirOf(v) {
        const rel = v.rel || '';
        const i = Math.max(rel.lastIndexOf('/'), rel.lastIndexOf('\\'));
        return i > 0 ? rel.slice(0, i).replace(/[\\/]+$/, '') : '';
      },
    };
  },
};

FS.AssetsView = AssetsView;
})();
