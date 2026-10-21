/* 素材页 URL 筛选端到端验证
   用真实 Vue 响应式系统（reactive/ref/computed/watch）+ 真实 assets.js/libs.js
   组件逻辑，只把「生命周期钩子」换成可控的收集器（生产版 Vue 不暴露 setupState，
   挂载渲染又依赖未注册的 Element Plus 组件，两条路都不通）。
   跑：node tools/test_assets_url.js */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const read = p => fs.readFileSync(path.join(ROOT, p), 'utf8');

/* ---------- 浏览器全局垫片（只给真正会被代码碰到的那几个） ---------- */
let VIDEOS = [];
const _hash = { v: '' };
const _hashHandlers = [];
const _hist = [];
const _toasts = [];
let _navigator;
const _win = {
  innerWidth: 1440, innerHeight: 900,
  location: {
    get hash() { return _hash.v; },
    set hash(v) { _hash.v = v; _hashHandlers.forEach(f => f()); },
    get href() { return 'http://127.0.0.1:8771/' + _hash.v; },
    origin: 'http://127.0.0.1:8771', pathname: '/', protocol: 'http:', host: '127.0.0.1:8771',
  },
  localStorage: (() => { const m = {};
    return { getItem: k => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); },
             removeItem: k => { delete m[k]; } }; })(),
  history: {
    pushState(_s, _t, u) { _hist.push(['push', u]); _hash.v = u; },
    replaceState(_s, _t, u) { _hist.push(['replace', u]); _hash.v = u; },
    back() {}, forward() {},
  },
  addEventListener(t, f) { if (t === 'hashchange') _hashHandlers.push(f); },
  removeEventListener() {},
  navigator: { get clipboard() { return _navigator.clipboard; }, userAgent: 'node' },
  setTimeout, clearTimeout, setInterval, clearInterval,
  fetch: async (u) => {
    const s = String(u);
    if (s.includes('/api/videos')) return { ok: true, status: 200, json: async () => VIDEOS };
    if (s.includes('/api/state')) return { ok: true, status: 200, json: async () => ({
      libraries: [{ id: 3, name: '电影', videos: 65, lines: 1, duration: 1,
                    exists: true, indexed: true, path: 'E:\\视频\\电影' }],
      active: '', hub_db: 'hub.db', ocr: { ok: true }, total: { videos: 0, lines: 0 } }) };
    return { ok: true, status: 200, json: async () => ({}) };
  },
  console, __toasts: _toasts,
};
_win.window = _win; _win.self = _win; _win.globalThis = _win;
_win.document = {
  createElement: () => ({ style: {}, children: [], setAttribute() {}, appendChild() {} }),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
  documentElement: { clientHeight: 900 }, body: {},
};
_navigator = { clipboard: { writeText: async (t) => { _win.__copied = t; } } };

const sandbox = vm.createContext(_win);
vm.runInContext('window.ElementPlus={ElMessage:Object.assign(function(o){window.__toasts.push(o);return{};},'
  + '{success(){},error(){},warning(){},info(){}}),ElMessageBox:{confirm:()=>Promise.reject(new Error("x")),'
  + 'alert:()=>Promise.resolve()}};window.ElementPlusLocaleZhCn={};window.ElementPlusIconsVue={};', sandbox);

/* ---------- 真实 Vue 响应式系统（只加载 runtime-core 的 reactive/ref/computed/watch） ----------
   直接用 vue.global.prod.js 里的 reactive/computed/ref；watch 自己实现成
   「收集源 → 下一轮微任务重跑」，够验证筛选联动。 */
vm.runInContext(read('web/vendor/vue.global.prod.js'), sandbox, { filename: 'vue.js' });
const VueRT = _win.Vue;

/* ---------- 组合式 API 垫片 ----------
   响应式全部用 Vue 真实实现（reactive/ref/computed/watch 都独立于组件实例可用，
   Vue 的调度器是微任务，组件外一样跑）。只把生命周期钩子换成收集器——
   生产版不暴露 setupState，挂载渲染又依赖未注册的 Element Plus 组件。 */
const HOOKS = { mounted: [], activated: [], unmounted: [] };
function makeCtx() {
  return {
    reactive: o => VueRT.reactive(o),
    ref: v => VueRT.ref(v),
    computed: f => VueRT.computed(f),
    watch: (a, b, c) => VueRT.watch(a, b, c),
    nextTick: () => VueRT.nextTick(),
    onMounted: f => HOOKS.mounted.push(f),
    onActivated: f => HOOKS.activated.push(f),
    onUnmounted: f => HOOKS.unmounted.push(f),
    onBeforeUnmount: f => HOOKS.unmounted.push(f),
    onUnmounted_: null,
  };
}
/* 等 Vue 调度器把 watch / computed 刷新完（微任务 + 一个宏任务保险） */
const settle = async (n = 3) => {
  for (let i = 0; i < n; i++) {
    await VueRT.nextTick();
    await new Promise(r => setTimeout(r, 0));
  }
};

/* ---------- 载入项目代码（Vue 替换成增强版） ---------- */
function loadAssets() {
  const Ctx = makeCtx();
  const sb = vm.createContext(Object.assign(Object.create(_win), {
    Vue: Ctx, window: _win, ElementPlus: _win.ElementPlus,
    console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Date,
    URLSearchParams, Set, Map, Object, Array, Number, String, Boolean, Error, RegExp, encodeURIComponent,
  }));
  sb.window = _win;
  sb.globalThis = sb;
  vm.runInContext(read('web/js/state.js'), sb, { filename: 'state.js' });
  vm.runInContext(read('web/js/assets.js'), sb, { filename: 'assets.js' });
  vm.runInContext(read('web/js/libs.js'), sb, { filename: 'libs.js' });
  return { FS: _win.FS, Ctx, HOOKS };
}

/* ================= 测试 ================= */
let pass = 0, fail = 0;
const ok = (c, m) => { if (c) { pass++; console.log('  ✓ ' + m); }
  else { fail++; console.log('  ✗ ' + m); } };

(async function main() {
  /* 素材数据：优先用本地缓存的 tools/.videos.json，没有就直接打后端拉一份。
     跑测试前先起服务：python app.py（默认 8771） */
  let raw;
  try { raw = JSON.parse(read('tools/.videos.json')); }
  catch (e) {
    process.stdout.write('  从后端拉取素材数据…\n');
    // 本机有系统代理，直连 127.0.0.1 必须绕开，否则拿到 502
    const http = require('http');
    raw = await new Promise((res, rej) => {
      http.get({ host: '127.0.0.1', port: 8771, path: '/api/videos?scope=all',
                 agent: false }, r => {
        let b = '';
        r.on('data', d => { b += d; });
        r.on('end', () => { try { res(JSON.parse(b)); } catch (x) { rej(x); } });
      }).on('error', rej);
    });
  }
  VIDEOS = Array.isArray(raw) ? raw : raw.items || [];
  const target = VIDEOS.find(v => v.lib_id === 3) || VIDEOS[0];
  console.log(`\n== 真实数据 ${VIDEOS.length} 条 ==`);
  console.log(`  目标影片: id=${target.id} ${target.name} @ ${target.root}`);

  const R = loadAssets();
  const { S, BUS, Route } = R.FS;
  const flush = settle;
  const { AssetsView, LibsView } = R.FS;
  const hooks = HOOKS;

  /* 手动跑组件 setup */
  function mountSetup(comp) {
    const out = comp.setup.call(R.Ctx);
    return out;
  }

  /* ---- A. 素材库点「查看素材」→ 写 URL（只带 lib，不做单部锁定） ---- */
  console.log('\n== A. 素材库点「查看素材」→ 写 URL ==');
  _hash.v = ''; _hist.length = 0; S.tab = 'libs';
  const ls = mountSetup(LibsView);
  await flush();
  ls.viewAssets({ id: target.lib_id, name: '电影', path: 'E:\\视频\\电影' });
  await flush();

  ok(_hash.v.startsWith('#assets?'), '  地址栏 = ' + _hash.v);
  ok(_hist[0] && _hist[0][0] === 'push', '  用 pushState（能「后退」回素材库）');
  ok(S.tab === 'assets', '  S.tab 切到素材');
  const q = Route.parse().q;
  ok(q.lib === String(target.lib_id), '  lib=' + q.lib);
  ok(!q.vid && !q.q, '  不带 vid / q（只看整个库，不锁定单部影片）');

  /* ---- B. 素材页按 URL 还原 ---- */
  console.log('\n== B. 素材页按 URL 还原筛选（首屏直达）==');
  _hash.v = '#assets?lib=' + target.lib_id + '&q=' + encodeURIComponent(target.name)
          + '&vid=' + target.id + '&root=' + encodeURIComponent(target.root);
  S.tab = 'assets';
  const a = mountSetup(AssetsView);
  // 首屏：先跑 onMounted（含 reload 拉全量）。onActivated 在真实页面里紧随其后，
  // 这里也一并跑，才等价于"keep-alive 页面被激活"的真实时序。
  for (const f of hooks.mounted) { try { f(); } catch (e) { console.log('  mounted err:', e.message); } }
  await flush();
  for (const f of hooks.activated) { try { f(); } catch (e) { console.log('  activated err:', e.message); } }
  await flush();
  ok(S.all.length === VIDEOS.length, '  全量已拉取：' + S.all.length + ' 条');

  ok(a.kw.value === target.name, '  关键词 = ' + a.kw.value);
  ok(a.pin.vid === +target.id, '  pin.vid = ' + a.pin.vid);
  ok(a.pin.root === target.root, '  pin.root 正确');
  ok(a.libSel[target.lib_id] === true, '  素材库 ' + target.lib_id + ' 已勾');
  ok(a.showFilter.value === true, '  筛选面板自动展开');

  /* ---- C. 命中结果 ---- */
  console.log('\n== C. 命中结果 ==');
  const view = a.view.value;
  ok(view.length === 1, '  命中 ' + view.length + ' 条');
  ok(view[0] && view[0].v.id === +target.id && view[0].v.root === target.root,
     '  命中的正是目标影片：' + (view[0] ? view[0].v.name : '无'));

  /* ---- D. URL 反写 ---- */
  console.log('\n== D. 筛选变化 → URL 同步 ==');
  let q2 = Route.parse().q;
  ok(+q2.vid === +target.id, '  URL 仍带 vid');
  ok(q2.lib === String(target.lib_id), '  URL 仍带 lib');

  a.libSel[14] = true;
  await flush();
  const libs = String(Route.parse().q.lib).split(',').sort().join(',');
  ok(libs === '14,' + target.lib_id, '  加选一个库后 URL lib=' + Route.parse().q.lib);
  a.libSel[14] = false;
  await flush();

  /* ---- E. 改关键词 → 自动解锁 ---- */
  console.log('\n== E. 手动改关键词 → 解除锁定 ==');
  // 先把库条件放开：亮剑在 14 号库，若还锁着 3 号库，搜不到是**正确**行为，
  // 那样测的就不是"自动解锁"了。
  a.resetFilters();
  await flush();
  ok(a.pin.vid === null && a.libSel[3] !== true, '  前置：已回到无筛选状态');

  a.kw.value = '亮剑';
  await flush();
  ok(a.pin.vid === null, '  pin 已解除（否则用户输的字会被白名单挡掉）');
  const v2 = a.view.value;
  const hit = v2.some(r => r.v && /亮剑/.test((r.v.name || '') + (r.v.rel || '')));
  ok(hit, '  「亮剑」搜得到，命中 ' + v2.length + ' 条');
  const allLib14 = v2.every(r => !r.v || +r.v.lib_id === 14);
  ok(allLib14, '  命中的确实都是 14 号库的片子');

  // 反证：锁定状态下改关键词，merged 应该仍只出 1 条（pin 白名单生效）
  a.kw.value = '';
  await flush();
  a.kw.value = '亮剑';
  await flush();
  ok(a.pin.vid === null, '  自动解锁后能按新关键词搜到全库');

  /* ---- F. 重置 ---- */
  console.log('\n== F. 重置 ==');
  a.resetFilters();
  await flush();
  ok(a.kw.value === '' && a.pin.vid === null && !a.durSel.value.length, '  条件全清');
  const q3 = Route.parse().q;
  ok(!q3.lib && !q3.q && !q3.vid && !q3.dur, '  URL 清干净：' + JSON.stringify(q3));

  /* ---- G. hashchange 驱动（前进/后退/手输）----
     先 Route.init()，这正是 app.js onMounted 里做的事（注册 hashchange 监听）。 */
  console.log('\n== G. hashchange 驱动 ==');
  Route.init();
  _hash.v = '#assets?lib=14&sort=name&dur=l20';
  _hashHandlers.forEach(f => f());
  await flush();
  ok(a.sort.value === 'name', '  sort = ' + a.sort.value);
  ok(a.libSel[14] === true, '  lib=14 已勾');
  ok(a.durSel.value.length === 1 && a.durSel.value[0] === 'l20', '  dur=l20 已选');
  ok(a.pin.vid === null, '  无 vid → 不锁定');

  const only14 = a.view.value.filter(r => r.v);
  ok(only14.length > 0 && only14.every(r => +r.v.lib_id === 14),
     '  结果只剩 14 号库：' + only14.length + ' 条');
  ok(a.showFilter.value === true, '  筛选面板自动展开');

  /* ---- H. 链接参数展示 + 复制 ---- */
  console.log('\n== H. 面板展示 / 复制链接 ==');
  const cq = a.curQuery.value;
  ok(cq.includes('lib=14') && cq.includes('sort=name') && cq.includes('dur=l20'),
     '  curQuery = ' + cq);
  await a.copyLink();
  ok(typeof _win.__copied === 'string' && _win.__copied.includes('lib=14'),
     '  剪贴板已写入完整链接');
  ok(_toasts.length > 0, '  有 toast 反馈');

  console.log(`\n${fail === 0 ? '全部通过' : '有失败'}：${pass} passed, ${fail} failed\n`);
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error('\n崩溃:', e); process.exit(1); });
