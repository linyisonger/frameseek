/* 按钮间距 / 对齐回归检查
   验证两件事：
   1) EP 的 `.el-button + .el-button{margin-left:12px}` 已被本项目样式归零，
      按钮间距只由 flex gap 决定，不再出现 gap + margin 叠加。
   2) 按钮内「图标 + 文字」间距统一：不管图标后面是 <span> 还是裸文本，
      间距都等于 --fs-gap-xs，不会一个 6px 一个 0。
   做法：按选择器优先级排序规则，算出某选择器的最终 margin-left /
   margin-right，然后跟期望值比对。属于纯 CSS 静态分析，不需要浏览器。 */

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const css = fs.readFileSync(path.join(ROOT, 'web', 'style.css'), 'utf8');
const epCss = fs.readFileSync(path.join(ROOT, 'web', 'vendor', 'element-plus.css'), 'utf8');

let pass = 0, fail = 0;
function ok(cond, msg) {
  if (cond) { pass++; console.log('  OK  ' + msg); }
  else { fail++; console.log('  FAIL ' + msg); }
}

/* ---------- 极简 CSS 解析：抽出选择器 + 声明块 ---------- */
function stripComments(text) { return text.replace(/\/\*[\s\S]*?\*\//g, ''); }
function parseRules(text) {
  // 去掉注释（注释里出现的 :has( 等字样不算选择器）
  const noCmt = stripComments(text);
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(noCmt))) {
    const sels = m[1].split(',').map(s => s.trim()).filter(Boolean);
    const decls = {};
    m[2].split(';').forEach(d => {
      const i = d.indexOf(':');
      if (i < 0) return;
      const k = d.slice(0, i).trim().toLowerCase();
      const v = d.slice(i + 1).trim();
      if (k) decls[k] = v;
    });
    if (sels.length) out.push({ sels, decls, order: out.length });
  }
  return out;
}

const project = parseRules(css);
const ep = parseRules(epCss);

/* 特异性：a,b,c 三元组 + 伪类/属性算 b。够用来比较本项目里这些规则。 */
function specificity(sel) {
  const ids = (sel.match(/#[\w-]+/g) || []).length;
  const cls = (sel.match(/\.[\w-]+/g) || []).length
            + (sel.match(/\[[^\]]+\]/g) || []).length
            + (sel.match(/:(?!:)[\w-]+/g) || []).length;
  const tags = (sel.match(/(^|[\s>+~])[a-z][\w-]*/gi) || [])
    .map(s => s.trim()).filter(s => s && !s.startsWith('.') && !s.startsWith('#')).length;
  return [ids, cls, tags];
}
function cmpSpec(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
}

/* 找出所有能匹配到「相邻的第二个按钮」的选择器，按级联算出最终 margin-left */
console.log('\n== 1. 相邻按钮 margin-left 的级联结果 ==');
/* 直接在文本里定位 `.el-button+.el-button{...}` 这种声明块，不走选择器解析
   ——EP 是压缩过的，声明块前面的 @media 等上下文会让朴素解析器切错选择器。
   注意空格：EP 里是紧凑写法 `.el-button+.el-button`，本项目里是带空格的
   `.el-button + .el-button`，两者要分别匹配（正则里空格是字面量）。 */
function findDeclBlock(text, selector) {
  const re = new RegExp('(?:^|[,{};\\s])' + selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                   + '\\s*\\{([^}]*)\\}', 'g');
  const out = [];
  let m;
  while ((m = re.exec(text))) {
    const decls = {};
    m[1].split(';').forEach(d => {
      const i = d.indexOf(':');
      if (i > 0) decls[d.slice(0, i).trim().toLowerCase()] = d.slice(i + 1).trim();
    });
    out.push(decls);
  }
  return out;
}

const cands = [
  // EP 压缩过，没有空格
  ...findDeclBlock(epCss, '.el-button+.el-button').map(d => ({ val: d['margin-left'], from: 'EP 默认', later: 0 })),
  // 本项目带空格（也兼容无空格写法）
  ...findDeclBlock(css, '.el-button + .el-button').map(d => ({ val: d['margin-left'], from: '本项目', later: 100000 })),
  ...findDeclBlock(css, '.el-button+.el-button').map(d => ({ val: d['margin-left'], from: '本项目', later: 100000 })),
].filter(c => c.val !== undefined);
cands.forEach(c => console.log(`  候选  ${c.from.padEnd(8)} margin-left:${c.val.padEnd(6)} 特异性 0,2,0`));
ok(cands.some(c => c.from === 'EP 默认' && c.val === '12px'),
   'EP 确实内置了 12px（说明这条覆盖是有必要加的）');
ok(cands.some(c => c.from === '本项目' && c.val === '0'),
   '本项目把相邻按钮 margin-left 归零');
// 同特异性时后加载的 style.css 胜出 → 取 later 最大的
const win = cands.slice().sort((a, b) => a.later - b.later).pop();
console.log(`  → 最终生效: margin-left:${win.val}`);
ok(win.from === '本项目' && win.val === '0',
   '相邻按钮 margin-left 归零（EP 的 12px 已被压制，不会和 gap 叠加）');

console.log('\n== 2. 按钮组间距只由 gap 决定 ==');
// 所有承载多个按钮的容器，都得有 gap
const GAP_CONTAINERS = [
  '.fs-tools', '.fs-libft', '.fs-bxbar', '.fs-rrow .ops', '.bxacts',
  '.fs-dock-hd', '.fs-bub .bub-acts', '.chipops', '.fs-form-actions .el-form-item__content',
];
let allGap = true;
for (const sel of GAP_CONTAINERS) {
  const hit = project.find(r => r.sels.includes(sel) && r.decls.gap);
  if (!hit) { ok(false, `${sel} 声明了 gap`); allGap = false; continue; }
  const v = hit.decls.gap.trim();
  const norm = v.replace(/\s+/g, '');
  const okv = /^var\(--fs-gap-[a-z]+\)$/.test(norm) || /^\d+px$/.test(norm);
  if (!okv) allGap = false;
  console.log(`  ${sel.padEnd(44)} gap: ${v}`);
}
ok(allGap, '按钮组容器全部声明了 gap');

console.log('\n== 3. 弹窗 footer 已转 flex（否则归零后会贴死）==');
const dlgFooter = project.filter(r => r.sels.includes('.el-dialog__footer'));
ok(dlgFooter.length > 0, '定义了 .el-dialog__footer 覆盖');
const dfDecl = dlgFooter.map(r => r.decls).find(d => d.display === 'flex');
ok(!!dfDecl, 'el-dialog__footer 设为 flex（间距改由 gap 承担）');
ok(dfDecl && /var\(--fs-gap-[a-z]+\)/.test(dfDecl.gap || ''), 'el-dialog__footer 用了间距令牌');
const mbB = project.filter(r => r.sels.includes('.el-message-box__btns'));
ok(mbB.length > 0, '定义了 .el-message-box__btns 覆盖（确认框按钮不贴死）');

console.log('\n== 4. 按钮内图标 + 文字间距统一 ==');
const iconRules = project.filter(r => r.sels.some(s => /el-button.*el-icon/.test(s)));
iconRules.forEach(r => console.log(`  ${r.sels.join(' | ').padEnd(56)} ${JSON.stringify(r.decls).slice(0, 90)}`));
// 间距由「紧跟图标的 span」承担，所以模板里文字必须包 span
const spanRule = iconRules.find(r => /el-icon.*\+\s*span/.test(r.sels[0]));
ok(!!spanRule, '定义了「图标 + 紧跟 span」的间距规则');
ok(spanRule && spanRule.decls['margin-left'].includes('--fs-gap-xs'),
   '图标后文字间距 = --fs-gap-xs（收进令牌，不再是 EP 的 6px 硬编码）');

/* 守卫：模板里不得出现「图标 + 裸文本」。
   纯 CSS 区分不了裸文本（文本节点不参与 :last-child），一旦回退就会重新贴死。
   扫描范围包含 web/js/*.js 和 web/index.html —— 顶栏浮窗的按钮写在 index.html 里，
   只扫 js 目录会漏掉（踩过：fs-dock 的「收起/展开」就是这么贴死的）。 */
const nakedBtn = [];
const jsDir = path.join(ROOT, 'web', 'js');
function scanNaked(dir, filter) {
  for (const f of fs.readdirSync(dir).filter(filter)) {
    const p = path.join(dir, f);
    const src = fs.readFileSync(p, 'utf8');
    const re = /<\/el-icon>([^<\n]+?)(?=<\/el-button>)/g;
    let mm;
    while ((mm = re.exec(src))) {
      if (mm[1].trim()) {
        nakedBtn.push(f + ':L' + src.slice(0, mm.index).split('\n').length);
      }
    }
  }
}
scanNaked(jsDir, n => n.endsWith('.js'));
scanNaked(path.join(ROOT, 'web'), n => n === 'index.html');
console.log('  裸文本按钮位置:', nakedBtn.length ? nakedBtn.join(', ') : '无');
ok(nakedBtn.length === 0, '模板里所有按钮文字都包了 <span>（js + index.html 均 0 处裸文本）');

console.log('\n== 5. 间距刻度一致性 ==');
const defined = [...new Set([...css.matchAll(/(--fs-gap-[a-z]+)\s*:/g)].map(m => m[1]))].sort();
const used = [...new Set([...css.matchAll(/var\((--fs-gap-[a-z]+)\)/g)].map(m => m[1]))].sort();
console.log('  定义:', defined.join(', '));
console.log('  使用:', used.join(', '));
ok(defined.length === used.length && defined.every(d => used.includes(d)),
   '定义的令牌全部被使用，无冗余');
// 令牌值必须是 4 的倍数（刻度纪律）：从 :root 块里取 --fs-gap-* 的定义
const rootBlock = (css.match(/:root\s*\{([\s\S]*?)\}/) || [, ''])[1];
const vals = [...rootBlock.matchAll(/--fs-gap-[a-z]+:\s*(\d+)px/g)].map(m => +m[1]);
console.log('  刻度值:', vals.join(', ') || '(未取到)');
ok(vals.length === defined.length,
   `每个令牌都有 px 定义（${defined.length} 个定义 / 取到 ${vals.length} 个）`);
ok(vals.every(v => v % 4 === 0), '所有间距刻度都是 4 的倍数');
// 除了弹窗和按钮专属，不该再有裸 gap
const naked = [...css.matchAll(/(?<!-)gap:\s*(\d+)px/g)].map(m => m[1]);
const nakedUniq = [...new Set(naked)];
console.log('  剩余裸 gap 值:', nakedUniq.length ? nakedUniq.join(', ') : '无');
ok(nakedUniq.length === 0, '样式表内已无裸 gap 数值，全部走令牌');

console.log(`\n${fail === 0 ? '全部通过' : '有失败'}：${pass} passed, ${fail} failed\n`);
process.exit(fail === 0 ? 0 : 1);
