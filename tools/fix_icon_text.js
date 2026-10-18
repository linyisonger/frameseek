/* 把按钮内「</el-icon> 裸文本」统一包进 <span>。
   背景：EP 只给 `.el-button [class*=el-icon] + span { margin-left: 6px }` 留了
   图标与文字的间距，裸文本节点匹配不上 → 同一组按钮里有的分开有的贴死。
   纯 CSS 无法选中文本节点区分「图标独占」和「图标+裸文本」（文本节点不参与
   :last-child），所以统一在模板层包 <span>，让 EP 原生规则直接生效。

   用法：node tools/fix_icon_text.js [--check]
   --check 只报告不改写。 */
const fs = require('fs');
const path = require('path');

const DIR = path.join(__dirname, '..', 'web', 'js');
const CHECK = process.argv.includes('--check');

// </el-icon> 后面直接跟非空白、非 '<' 的内容，且该内容在 </button> 之前 → 包 span
const RE = /(<\/el-icon>)([^<\n]+?)(?=<\/el-button>)/g;

let total = 0;
const report = [];

for (const f of fs.readdirSync(DIR).filter(n => n.endsWith('.js'))) {
  const p = path.join(DIR, f);
  const src = fs.readFileSync(p, 'utf8');
  const hits = [];
  let m;
  const re = new RegExp(RE.source, 'g');
  while ((m = re.exec(src))) {
    const text = m[2];
    // 已经被 span 包过的不动
    if (/^\s*<\//.test(text) || /<span/.test(m[0])) continue;
    const line = src.slice(0, m.index).split('\n').length;
    hits.push({ line, text: text.trim().slice(0, 40) });
  }
  if (!hits.length) continue;
  report.push({ f, hits });
  total += hits.length;
}

if (CHECK) {
  console.log(`共发现 ${total} 处「图标 + 裸文本」按钮：`);
  for (const { f, hits } of report) {
    console.log(`\n${f}`);
    hits.forEach(h => console.log(`  L${h.line}  ${h.text}`));
  }
  process.exit(0);
}

for (const { f } of report) {
  const p = path.join(DIR, f);
  const src = fs.readFileSync(p, 'utf8');
  const out = src.replace(RE, (all, closeIcon, text) => {
    const t = text.trim();
    if (!t) return all;
    return `${closeIcon}<span>${t}</span>`;
  });
  fs.writeFileSync(p, out);
}
console.log(`已改写 ${total} 处（${report.length} 个文件）`);
