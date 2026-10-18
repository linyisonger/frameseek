/* 用真实浏览器（Edge headless）跑 tools/spacing_probe.html，解析输出里的断言结果。
   静态 CSS 分析（test_spacing.js）看不出「渲染后到底多少 px」，
   这一步是真实渲染验证：按钮间距、等宽、图标文字间距、弹窗 footer 是否贴死。

   用法：node tools/probe_spacing.js
   依赖：本机装了 Edge（默认路径），找不到就跳过并提示。 */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const ROOT = path.join(__dirname, '..');
const PAGE = path.join(__dirname, 'spacing_probe.html');

const CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];
const browser = CANDIDATES.find(p => fs.existsSync(p));

if (!browser) {
  console.log('未找到 Edge / Chrome，跳过真实渲染验证。');
  console.log('（静态 CSS 检查仍会跑：node test_spacing.js）');
  process.exit(0);
}

const fileUrl = 'file:///' + PAGE.replace(/\\/g, '/');
let dom = '';
try {
  dom = execFileSync(browser, [
    '--headless=new', '--disable-gpu', '--no-sandbox',
    '--allow-file-access-from-files',
    '--virtual-time-budget=6000',
    '--dump-dom', fileUrl,
  ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 60000 });
} catch (e) {
  console.log('浏览器执行失败：' + e.message);
  process.exit(1);
}

const m = dom.match(/<pre id="out"[^>]*>([\s\S]*?)<\/pre>/);
if (!m || !m[1].trim()) {
  console.log('探测页没有产出结果（可能被 headless 策略拦了）。');
  console.log('DOM 长度 ' + dom.length + '，标题：'
    + ((dom.match(/<title>(.*?)<\/title>/) || [, '(无)'])[1]));
  process.exit(1);
}

const text = m[1]
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&rarr;/g, '->');

console.log(text);

const passes = (text.match(/^PASS/gm) || []).length;
const fails = (text.match(/^FAIL/gm) || []).length;
console.log(`\n[probe] ${passes} passed, ${fails} failed（真实渲染：${path.basename(browser)}）`);
process.exit(fails === 0 ? 0 : 1);
