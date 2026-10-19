/* 校验 assets.js / libs.js 的模板能被真实 Vue 编译器编译通过
   跑：node tools/test_templates.js */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const read = p => fs.readFileSync(path.join(ROOT, p), 'utf8');

/* 最小 DOM 垫片：Vue 编译器启动时会做特性探测
   （div.innerHTML='<input type="checkbox">' 后读 firstChild.hasAttribute），
   没有真 HTML parser 就会在编译前就抛错。 */
function mkEl(tag) {
  let _html = '';
  const el = {
    tagName: String(tag || 'div').toUpperCase(), nodeType: 1,
    style: {}, dataset: {}, children: [], attributes: {}, textContent: '',
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    insertBefore(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k] != null ? this.attributes[k] : null; },
    removeAttribute(k) { delete this.attributes[k]; },
    hasAttribute(k) { return k in this.attributes; },
    addEventListener() {}, removeEventListener() {}, contains: () => false,
    querySelector: () => null, querySelectorAll: () => [],
    get firstChild() { return this.children[0] || null; },
    get lastChild() { return this.children[this.children.length - 1] || null; },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return _html; },
    set(v) {
      _html = String(v);
      this.children = [];
      const re = /<([a-zA-Z][\w-]*)((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*\/?>/g;
      let m;
      while ((m = re.exec(_html))) {
        const c = mkEl(m[1]);
        const ar = /([\w:-]+)(?:="([^"]*)")?/g;
        let a;
        while ((a = ar.exec(m[2] || ''))) if (a[1]) c.attributes[a[1]] = a[2] === undefined ? '' : a[2];
        c.parentNode = this;
        this.children.push(c);
      }
    },
  });
  return el;
}
const doc = {
  createElement: mkEl, createElementNS: (ns, t) => mkEl(t),
  createTextNode: t => ({ nodeType: 3, textContent: String(t) }),
  createComment: t => ({ nodeType: 8, textContent: String(t) }),
  createDocumentFragment: () => mkEl('fragment'),
  querySelector: () => null, querySelectorAll: () => [],
  getElementById: () => null, getElementsByTagName: () => [],
  addEventListener() {}, removeEventListener() {},
  documentElement: mkEl('html'), body: mkEl('body'), head: mkEl('head'),
  activeElement: null, hasFocus: () => false,
};
const w = { console, document: doc };
w.window = w; w.self = w; w.globalThis = w;
w.SVGElement = function(){}; w.HTMLElement = function(){}; w.Element = function(){};
w.Node = function(){}; w.Text = function(){}; w.Comment = function(){};
w.DocumentFragment = function(){};
w.MutationObserver = function(){ this.observe = function(){}; this.disconnect = function(){}; };
w.ResizeObserver = function(){ this.observe = function(){}; this.disconnect = function(){}; };
w.requestAnimationFrame = f => setTimeout(f, 0);
vm.createContext(w);
vm.runInContext(read('web/vendor/vue.global.prod.js'), w, { filename: 'vue.js' });

const Vue = w.Vue;

/* 模板是从 `template: \`` 到与之配对的收尾反引号。
   模板内部只有 \` 转义（没有 ${} 插值），所以按"未转义的反引号"找终点即可。 */
function grabTemplate(file) {
  const src = read(file);
  const tag = 'template: `';
  const start = src.indexOf(tag);
  if (start < 0) return null;
  let i = src.indexOf(tag) + tag.length;
  for (; i < src.length; i++) {
    if (src[i] === '`' && src[i - 1] !== '\\') return src.slice(start + tag.length, i);
  }
  return null;
}

let pass = 0, fail = 0;
const ok = (c, m) => { if (c) { pass++; process.stdout.write('  ✓ ' + m + '\n'); }
  else { fail++; process.stdout.write('  ✗ ' + m + '\n'); } };

/* 覆盖全部带模板的功能页。之前只查 assets/libs，
   按钮文字包 <span> 那次批量改写动过 aidraw/gen/clips，必须一起编译校验，
   否则模板写坏了要等到浏览器里才发现。 */
const files = ['web/js/assets.js', 'web/js/libs.js', 'web/js/aidraw.js',
               'web/js/gen.js', 'web/js/clips.js',
               'web/js/box.js', 'web/js/search.js'];
console.log('\n== 模板编译校验（真实 Vue 编译器）==');
files.forEach(f => {
  const tpl = grabTemplate(f);
  if (tpl === null) { ok(false, f + ' 取不到模板'); return; }
  let r;
  try { r = Vue.compile(tpl); }
  catch (e) { ok(false, f + ' 编译抛异常：' + e.message); return; }
  const errs = (r && r.errors) || [];
  if (errs.length) {
    ok(false, f + ' 编译错误 ' + errs.length + ' 处：');
    errs.slice(0, 6).forEach(e => process.stdout.write('       ' + String(e.message || e) + '\n'));
  } else {
    ok(true, f + ' 模板编译通过（' + tpl.length + ' 字符 → '
       + (r.code ? r.code.length : 0) + ' 字符 render 函数）');
  }
});

/* 新加的 UI 片段单独确认已被模板覆盖（防止改了 JS 却忘了同步模板） */
const a = read('web/js/assets.js');
console.log('\n== 新增 UI 挂载点 ==');
[
  ['vCopyBtn',        '复制筛选链接按钮'],
  ['fs-pinalert',     '锁定影片提示条'],
  ['fs-pinbar',       '锁定提示条内容'],
  ['fs-urlcode',      '当前链接参数展示'],
  ['id="fUrlRow"',    '链接参数行'],
  ['curQuery',        '链接参数 computed'],
].forEach(([needle, desc]) => {
  ok(a.indexOf(needle) >= 0, '模板/脚本含 ' + needle + '（' + desc + '）');
});
const l = read('web/js/libs.js');
ok(l.indexOf('Route.set(\'assets\'') >= 0, 'libs.js 用 Route.set 写跳转 URL');
ok(l.indexOf("replace: false") >= 0 || l.indexOf(', false)') >= 0, 'libs.js 用 pushState（可后退）');

console.log(`\n${fail === 0 ? '全部通过' : '有失败'}：${pass} passed, ${fail} failed\n`);
process.exit(fail === 0 ? 0 : 1);
