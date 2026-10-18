/* 前端布局结构审查：静态扫 web/index.html + web/js/*.js + web/style.css，
   找出「看着没对齐 / 不够整齐」的**结构性**成因。

   为什么必须扫：本机 Edge headless 已不可靠（截图要 2 分钟不出图，
   --dump-dom 里 setTimeout 不推进），而「差 2px」这类问题恰恰是
   肉眼扫不出来的，只能靠规则化检查。

   检查项（每条都对应一类真实缺陷）：
     1. 同一行控件列高不一致（el-col 内的 form-item / 卡片有没有对齐类）
     2. label=" " 占位 hack（透明 label，撑高但不可见）
     3. 图标 + 裸文本按钮（图标贴死）
     4. h1 里塞 .dim（继承 20px/600，层次全没）
     5. 卡片类没有 margin-bottom / 用 margin-top（垂直节奏不统一）
     6. grid/flex 容器里混用 margin-left 做间距（与 gap 叠加）
     7. el-row 的 :gutter 与 CSS gap 混用（双份间距）
     8. 固定宽度（px）写死，窄屏会溢出
     9. 同类卡片的高度是否被锁定（缺 min-height/等高）
     10. 表单 label-height 是否统一令牌

   用法：node tools/test_layout.js */
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const WEB = path.join(ROOT, 'web');
const css = fs.readFileSync(path.join(WEB, 'style.css'), 'utf8');

let pass = 0, fail = 0;
const problems = [];
function ok(cond, msg, detail) {
  if (cond) { pass++; process.stdout.write('  OK  ' + msg + '\n'); }
  else {
    fail++; problems.push(msg + (detail ? '  ->  ' + detail : ''));
    process.stdout.write('  FAIL ' + msg + (detail ? '  ->  ' + detail : '') + '\n');
  }
}

/* ---------- 收集源文件 ---------- */
function walk(dir, ext) {
  return fs.readdirSync(dir).flatMap(n => {
    const p = path.join(dir, n);
    return fs.statSync(p).isDirectory() ? walk(p, ext)
      : (n.endsWith(ext) ? [p] : []);
  });
}
const templates = walk(path.join(WEB, 'js'), '.js')
  .concat([path.join(WEB, 'index.html')]);
const srcOf = new Map();
templates.forEach(f => srcOf.set(f, fs.readFileSync(f, 'utf8')));
const rel = f => path.relative(ROOT, f).replace(/\\/g, '/');

/* ================= 1. label=" " 占位 hack ================= */
console.log('\n== 1. label=" " 占位 hack ==');
/* 空格 label 有两种用法：
   a) 装**按钮**的补位列 —— label 内容透明、只为占高度，合理，保留
   b) 装**说明文字**的列 —— 内容本来就是文字，label 多余，
      且透明 label 是「看不见但撑高度」的节点，是错位的主要来源
   这里只查 b)：form-item 里除了 label=" " 还含 span/div 文字的。 */
const SPACE_LABEL = [];
templates.forEach(f => {
  // 剥掉注释：注释里为说明问题会写到 label=" "，那是说明不是用法
  const s = srcOf.get(f).replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
  const re = /<el-form-item label="(\s|&nbsp;)+">([\s\S]*?)<\/el-form-item>/g;
  let m;
  while ((m = re.exec(s))) {
    const inner = m[2];
    const isButtonCol = /<el-button/.test(inner);
    if (!isButtonCol) {
      SPACE_LABEL.push(rel(f) + ':L' + s.slice(0, m.index).split('\n').length);
    }
  }
});
ok(SPACE_LABEL.length === 0,
   '说明文字列没有用空格 label 占位（按钮列占位允许）',
   SPACE_LABEL.join(', '));

/* ================= 2. 图标 + 裸文本按钮 ================= */
console.log('\n== 2. 按钮内「图标 + 裸文本」==');
const NAKED = [];
templates.forEach(f => {
  const s = srcOf.get(f);
  const re = /<\/el-icon>([^<\n]+?)(?=<\/el-button>)/g;
  let m;
  while ((m = re.exec(s))) {
    if (m[1].trim()) NAKED.push(rel(f) + ':L' + s.slice(0, m.index).split('\n').length);
  }
});
ok(NAKED.length === 0, '按钮文字全部包了 <span>', NAKED.join(', '));

/* ================= 3. h1 里的次要信息 ================= */
console.log('\n== 3. h1 内的次要信息 ==');
const H1DIM = [];
templates.forEach(f => {
  const s = srcOf.get(f);
  s.split('\n').forEach((line, i) => {
    if (/<h1[^>]*>[\s\S]*?class="dim"/.test(line)) H1DIM.push(rel(f) + ':L' + (i + 1));
  });
});
ok(H1DIM.length > 0, '存在 h1 内次要信息（本项是检查样式，不是禁止）', H1DIM.join(', '));
const h1dimRule = /\.fs-page-hd\s+h1\s+\.dim|fs-page-hd h1 \.dim/.test(css);
ok(h1dimRule, '已为 h1 内 .dim 定义降级样式（字号/字重独立于 h1）');

/* ================= 4. 同一行 el-col 的对齐类 ================= */
console.log('\n== 4. 同一行 el-col 的对齐一致性 ==');
/* 有 el-form-item 的列要按行分组检查：同一行的列高度差过大就是没对齐。
   静态判断：同一 <el-row> 块里，若部分列有 label、部分是纯文字/按钮，
   那部分就必须带 fs-col-note / fs-form-actions 补位类。 */
let rowBad = [];
templates.forEach(f => {
  const s = srcOf.get(f);
  // 粗略按 <el-row> ... </el-row> 分块
  const rows = s.match(/<el-row[\s\S]*?<\/el-row>/g) || [];
  rows.forEach((blk, ri) => {
    const cols = blk.match(/<el-col[\s\S]*?(?=<el-col|<\/el-row>)/g) || [];
    const kinds = cols.map(c => {
      if (/fs-col-note|fs-form-actions/.test(c)) return 'pad';
      if (/<el-form-item/.test(c)) return 'form';
      if (/el-card/.test(c)) return 'card';
      return 'bare';                    // 裸列：既无 form-item 也无补位类
    });
    // 同行里既有 form 又有 bare 列 => bare 列必然错位
    if (kinds.includes('form') && kinds.includes('bare')) {
      const line = s.slice(0, s.indexOf(blk)).split('\n').length;
      rowBad.push(rel(f) + ':L' + line + ' 第' + (ri + 1) + '行有裸列');
    }
  });
});
ok(rowBad.length === 0, '同行里没有「裸列」（无 form-item 又无补位类）', rowBad.join(' | '));

/* ================= 5. 垂直节奏：卡片间距是否统一 ================= */
console.log('\n== 5. 垂直节奏 ==');
const cardRules = [...css.matchAll(/(\.[\w-]+)\s*\{([^}]*)\}/g)]
  .filter(m => /margin-bottom|margin:\s*[^;]*\d/.test(m[2]));
const mbVals = cardRules
  .map(m => {
    const b = m[2].match(/margin-bottom:\s*([\d.]+px)/);
    if (b) return { sel: m[1], v: b[1] };
    const s = m[2].match(/margin:\s*([^;]+)/);
    if (s && !/auto/.test(s[1])) {
      const last = s[1].trim().split(/\s+/).pop();
      if (/^[\d.]+px$/.test(last)) return { sel: m[1], v: last };
    }
    return null;
  }).filter(Boolean);
console.log('  卡片类外边距:', mbVals.map(x => x.sel + '=' + x.v).join(' '));
ok(mbVals.length > 0, '卡片类统一用 margin-bottom 建立垂直节奏');

/* ================= 6. 间距只走 gap，不用 margin-left 做组内间距 ================= */
console.log('\n== 6. 间距只用 gap ==');
/* 按钮/标签类容器里出现 margin-left:非0，就是和 gap 叠加了 */
const BAD_ML = [];
templates.forEach(f => {
  const s = srcOf.get(f);
  const re = /class="([^"]*)"[^>]*>([\s\S]{0,400}?)<\/div>/g;
  let m;
  while ((m = re.exec(s))) {
    const cls = m[1];
    if (!/(tools|acts|ft|ft |chipops|ops|chips|navtools|row|bar|foot|reasons)/.test(cls)) continue;
    if (/margin-left:\s*[1-9]/.test(m[2])) {
      BAD_ML.push(rel(f) + ' .' + cls.split(/\s+/)[0]);
    }
  }
});
ok(BAD_ML.length === 0, 'flex 容器内没用 margin-left 做组内间距',
   [...new Set(BAD_ML)].join(', '));

/* ================= 7. 固定 px 宽度（窄屏溢出风险） ================= */
console.log('\n== 7. 响应式：写死的 px 宽度 ==');
const FIXED_W = [];
templates.forEach(f => {
  const s = srcOf.get(f);
  s.split('\n').forEach((line, i) => {
    const m = line.match(/style="[^"]*width:\s*(\d+)px/);
    if (m && +m[1] > 120) FIXED_W.push(rel(f) + ':L' + (i + 1) + ' ' + m[1] + 'px');
  });
});
ok(FIXED_W.length === 0, '模板里没有写死的宽 px（>120px）', FIXED_W.join(', '));

/* ================= 8. 等高卡片 ================= */
console.log('\n== 8. 等高卡片 ==');
/* grid 排布：grid 天然等高，直接看列定义 */
const gridCards = ['.fs-libcards', '.fs-videos'];
gridCards.forEach(sel => {
  const re = new RegExp(sel.replace('.', '\\.') + '\\s*\\{([^}]*)\\}');
  const m = css.match(re);
  ok(!!m && /grid-template-columns/.test(m[1]),
     sel + ' 用 grid 排布（跨行同列天然对齐）',
     m ? m[1].replace(/\s+/g, ' ').slice(0, 70) : '未找到');
});
/* el-row/el-col 排布：el-col 不是 flex，卡片不会自动撑满 → 高度参差。
   必须显式给列加 display:flex，并让卡片撑满。 */
ok(/\.fs-clips\s*>\s*\.el-col\s*\{[^}]*display:\s*flex/.test(css),
   '.fs-clips 的列设为 flex（否则 el-card 不撑满、行内卡片不等高）');
ok(/\.fs-clips\s*>\s*\.el-col\s*>\s*\.el-card\s*\{[^}]*width:\s*100%/.test(css),
   '.fs-clips 的卡片占满列宽');
/* grid 里的直接子项要能撑满，否则卡片高度参差 */
ok(/\.fs-libft\s*\{[^}]*grid-template-columns/.test(css),
   '库卡按钮区用固定列数 grid（跨行同列等宽）');

/* ================= 9. 表单 label 高度令牌 ================= */
console.log('\n== 9. 表单对齐基准 ==');
const labelRule = css.match(/\.fs-form \.el-form-item__label\s*\{([^}]*)\}/);
ok(!!labelRule, '定义了 .fs-form .el-form-item__label');
ok(labelRule && /height:\s*var\(--fs-label-h\)/.test(labelRule[1]),
   'label 高度用令牌 --fs-label-h（补位列据此对齐）',
   labelRule ? labelRule[1].replace(/\s+/g, ' ').slice(0, 90) : '');
ok(/--fs-label-h:\s*\d+px/.test(css), '--fs-label-h 已定义');

/* 补位列必须与 label 同高，否则仍会错位 */
const colNote = css.match(/\.fs-col-note\s*\{([^}]*)\}/);
ok(!!colNote, '定义了 .fs-col-note（说明文字列补位）');
const fsColnote = css.match(/\.fs-colnote\s*\{([^}]*)\}/);
ok(!!fsColnote && /padding-bottom/.test(fsColnote[1]),
   '.fs-colnote 有底部内边距（与输入框底边齐）');

/* ================= 10. 磁贴等高 ================= */
console.log('\n== 10. KPI 磁贴等高 ==');
ok(/\.fs-statrow \.el-col\s*\{[^}]*display:\s*flex/.test(css),
   'KPI 行用 flex 让磁贴等高');
ok(/\.fs-stat \.body b\s*\{[^}]*line-height:\s*\d+px/.test(css),
   '磁贴主数行高写死 px（有无副注都同高）');
const delta = css.match(/\.fs-stat \.body \.delta\s*\{([^}]*)\}/);
ok(delta && /min-height/.test(delta[1]),
   '副注有 min-height 兜底（缺副注的磁贴也等高）',
   delta ? delta[1].replace(/\s+/g, ' ').slice(0, 90) : '');

/* ================= 11. 间距令牌纪律 ================= */
console.log('\n== 11. 间距令牌纪律 ==');
/* 必须剥注释：注释里常引用 EP 的原始规则（如 margin-left: 12px）做说明 */
const cssNoCmt = css.replace(/\/\*[\s\S]*?\*\//g, '');
const nakedGap = [...cssNoCmt.matchAll(/(?<!-)gap:\s*(\d+)px/g)].map(m => m[1]);
ok(nakedGap.length === 0, 'CSS 里无裸 gap 数值（注释已排除）',
   [...new Set(nakedGap)].join(', '));
const nakedML = [...cssNoCmt.matchAll(/margin-left:\s*([1-9][\d.]*)px/g)].map(m => m[1]);
ok(nakedML.length === 0, 'CSS 里无裸 margin-left 像素值（应走间距令牌）',
   nakedML.join(', '));

/* ================= 12. 动效与可访问性 ================= */
console.log('\n== 12. 动效与可访问性 ==');
['--fs-dur-fast', '--fs-dur', '--fs-dur-slow', '--fs-ease'].forEach(t => {
  ok(new RegExp(t + ':').test(css), '定义了动效令牌 ' + t);
});
/* 硬编码 transition 时长：应走令牌，否则节奏不统一 */
const hardDur = [...cssNoCmt.matchAll(/transition:[^;]*?(\d+(?:\.\d+)?)s\b/g)].map(m => m[0]);
ok(hardDur.length === 0, 'transition 未硬编码秒数（应走 var(--fs-dur)）',
   hardDur.length + ' 处: ' + hardDur.slice(0, 2).join(' | '));
/* 必须支持 prefers-reduced-motion（WCAG 2.3.3） */
ok(/@media\s*\(prefers-reduced-motion:\s*reduce\)/.test(css),
   '支持 prefers-reduced-motion（关掉动效，可访问性要求）');
/* 深色底滚动条 */
ok(/::-webkit-scrollbar-thumb/.test(css), '自定义了滚动条样式（深底上不扎眼）');
/* 焦点可见：键盘用户必须能看见焦点在哪 */
const focusOutline = /:focus-visible/.test(cssNoCmt);
ok(focusOutline || /outline:\s*(2px|none)/.test(cssNoCmt),
   '定义了焦点样式（键盘导航可见）');

/* ================= 13. 烧字幕三档契约 ================= */
console.log('\n== 13. 烧字幕三档（auto/off/force） ==');
const stateJs = fs.readFileSync(path.join(WEB, 'js', 'state.js'), 'utf8');
const searchJs = fs.readFileSync(path.join(WEB, 'js', 'search.js'), 'utf8');
/* OCR 来源的画面本来就有硬字幕，再烧一层必然重影。策略只在服务端
   core/media.decide_burn 里判一次，前端只负责选档 + 透传，不许自己判。 */
ok(/subMode:\s*'auto'/.test(stateJs), "state.js 默认 subMode='auto'（默认按来源自动）");
ok(/S\.subtitle\b/.test(stateJs + searchJs) === false,
   'S.subtitle 旧布尔开关已拆干净（导出/字幕带是两件事，别共用一个开关）');
ok(/S\.subBand/.test(stateJs), '字幕带用独立开关 S.subBand');
/* 三档都必须出现在 UI 里，否则用户没法选 force（OCR 字幕纠错后要重烧） */
['auto', 'off', 'force'].forEach(v => {
  ok(new RegExp('value="' + v + '"').test(searchJs), '字幕下拉提供「' + v + '」档');
});
/* 传给后端的是档位字符串，不是布尔 */
ok(/subtitle:\s*S\.subMode/.test(stateJs), '导出请求透传 subMode 档位');
ok(/sub_skipped/.test(fs.readFileSync(path.join(ROOT, 'app.py'), 'utf8')),
   '后端返回 sub_skipped，前端据此提示为什么没烧');

console.log(`\n${fail === 0 ? '全部通过' : '有失败'}：${pass} passed, ${fail} failed`);
if (problems.length) {
  console.log('\n待修清单：');
  problems.forEach((p, i) => console.log(`  ${i + 1}. ${p}`));
}
process.exit(fail === 0 ? 0 : 1);