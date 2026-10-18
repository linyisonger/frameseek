/* URL 路由 + 素材页参数转换的纯逻辑单测（不依赖浏览器/DOM）
   跑：node tools/test_route.js */
'use strict';
const fs = require('fs');
const path = require('path');

// ---- 造一个最小 location/history 垫片，把 state.js 里的 Route 抠出来跑 ----
let _hash = '';
global.window = {
  addEventListener() {},
};
global.location = {
  get hash() { return _hash; },
  set hash(v) { _hash = v; },
  origin: 'http://127.0.0.1:8771',
  pathname: '/',
};
const _hist = [];
global.history = {
  pushState(_s, _t, url) { _hist.push(['push', url]); _hash = url; },
  replaceState(_s, _t, url) { _hist.push(['replace', url]); _hash = url; },
};
global.BUS = { _m: {}, on(k, f) { (this._m[k] = this._m[k] || []).push(f); },
               emit(k, ...a) { (this._m[k] || []).forEach(f => f(...a)); } };

// state.js 顶层引用 Vue / ElementPlus / window.FS，这里给个空壳让它跑过
global.Vue = { reactive: x => x, computed: f => f, ref: v => ({ value: v }),
               watch() {}, onMounted() {}, onUnmounted() {}, onActivated() {}, nextTick() {} };
global.ElementPlus = { ElMessage() {} };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });
global.setInterval = () => 0; global.clearInterval = () => {};
global.EventSource = function () { return { close() {} }; };
global.WebSocket = function () { return { close() {} }; };
global.WebSocket.OPEN = 1;
global.localStorage = { getItem: () => null, setItem() {} };

require(path.join(__dirname, '..', 'web', 'js', 'state.js'));
const { Route, S, BUS } = global.window.FS;

// ---- 把 assets.js 里的参数转换函数抠出来 ----
const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'js', 'assets.js'), 'utf8');
// 抠出 URL 参数转换 + 过滤判定这两段纯函数（都在组件定义之前）
const pick = [
  src.slice(src.indexOf('const URL_DUR'), src.indexOf('function durHit')),
  src.slice(src.indexOf('function durHit'), src.indexOf('const AssetsView')),
].join('\n');
const sandbox = { URLSearchParams };
new Function('exports', pick + `
exports.URL_DUR=URL_DUR;exports.paramsFromUrl=paramsFromUrl;
exports.urlFromState=urlFromState;exports.durHit=durHit;
exports.pinHit=pinHit;exports.rowHit=rowHit;`)(sandbox);
const { URL_DUR, paramsFromUrl, urlFromState, durHit, pinHit, rowHit } = sandbox;

let pass = 0, fail = 0;
function eq(a, b, msg) {
  const A = JSON.stringify(a), B = JSON.stringify(b);
  if (A === B) { pass++; console.log('  ✓ ' + msg); }
  else { fail++; console.log(`  ✗ ${msg}\n      期望 ${B}\n      实际 ${A}`); }
}

console.log('\n== 1. Route.build / parse 往返 ==');
eq(Route.build('assets', {}), '#assets', '空参数 → #assets');
eq(Route.build('assets', { lib: '1,2', q: '亮剑' }),
   '#assets?lib=1%2C2&q=%E4%BA%AE%E5%89%91', '参数会 encodeURIComponent');
_hash = '#assets?lib=1%2C2&q=%E4%BA%AE%E5%89%91';
let p = Route.parse();
eq(p.tab, 'assets', 'parse 出 tab');
eq(p.q, { lib: '1,2', q: '亮剑' }, 'parse 出参数并解码');
_hash = '#assets';
eq(Route.parse(), { tab: 'assets', q: {} }, '无参数时 q 为空对象');
_hash = '#';
eq(Route.parse(), { tab: '', q: {} }, '只有 # 时 tab 为空');
_hash = '#assets?lib=1&bad=';
p = Route.parse();
eq(p.q.lib, '1', '畸形参数不炸，其余照常');

console.log('\n== 2. paramsFromUrl 白名单校验 ==');
let r = paramsFromUrl({ lib: '1,2,x,,3', dur: 's30,bogus,m5_20', sort: 'zzz', q: '  亮剑  ', vid: '12a', root: '' });
eq(r.libs, ['1', '2', '3'], 'lib 丢掉非数字和空段');
eq(r.dur, ['s30', 'm5_20'], 'dur 只认白名单');
eq(r.sort, 'time', '非法 sort 回落 time');
eq(r.kw, '亮剑', 'q 去首尾空格');
eq(r.vid, null, '非数字 vid → null');
r = paramsFromUrl({ vid: '77', root: 'D:\\电影' });
eq([r.vid, r.root], [77, 'D:\\电影'], '合法 vid/root 保留');
r = paramsFromUrl({});
eq(r, { libs: [], dur: [], sort: 'time', kw: '', vid: null, root: '' }, '空参数给出干净默认值');

console.log('\n== 3. urlFromState 输出（空值不写进 URL） ==');
eq(urlFromState({ libs: [], kw: '', dur: [], sort: 'time', vid: null, root: '' }), {},
   '默认全空 → 无参数');
eq(urlFromState({ libs: [0], kw: '', dur: [], sort: 'time', vid: null, root: '' }),
   { lib: '0' }, 'lib=0（AI 生成）要写出去，不能被当空值吃掉');
eq(urlFromState({ libs: [1, 2], kw: '亮剑', dur: ['s30'], sort: 'name', vid: null, root: '' }),
   { lib: '1,2', q: '亮剑', dur: 's30', sort: 'name' }, '多条件拼接');
eq(urlFromState({ libs: [], kw: '', dur: [], sort: 'time', vid: 9, root: '' }),
   { vid: 9 }, '只有 vid 时只写 vid');
eq(urlFromState({ libs: [], kw: '', dur: [], sort: 'time', vid: 9, root: 'D:\\x' }),
   { vid: 9, root: 'D:\\x' }, 'vid + root 一起写（多库 id 撞号要靠 root 区分）');

console.log('\n== 4. build → parse → paramsFromUrl 端到端 ==');
const st = { libs: [3, 5], kw: '无间道', dur: ['m30', 'l20'], sort: 'mtime', vid: 42, root: 'E:\\片库\\电影' };
_hash = Route.build('assets', urlFromState(st));
r = paramsFromUrl(Route.parse().q);
eq(r.libs, ['3', '5'], '  lib 往返一致');
eq(r.kw, '无间道', '  q 往返一致');
eq(r.dur, ['m30', 'l20'], '  dur 往返一致');
eq(r.sort, 'mtime', '  sort 往返一致');
eq([r.vid, r.root], [42, 'E:\\片库\\电影'], '  vid/root 往返一致');

console.log('\n== 5. Route.set / dispatch 行为 ==');
S.tab = 'libs';
let seen = [];
BUS.on('route:change', x => seen.push(x));
_hist.length = 0;
Route.set('assets', { lib: '1' }, true);
eq(_hash, '#assets?lib=1', 'set 写入 hash');
eq(_hist[0][0], 'replace', 'replace=true 用 replaceState');
eq(S.tab, 'assets', 'dispatch 自动切 S.tab');
eq(seen.length, 1, '广播了一次 route:change');
eq(seen[0].hard, false, '内部 set 的广播标记为 soft');

_hist.length = 0;
Route.set('assets', { lib: '1' }, false);
eq(_hist[0][0], 'push', 'replace=false 用 pushState（可后退）');

_hash = '#assets?lib=9&q=x';
Route.dispatch(true);
eq(S.tab, 'assets', '外部 hashchange 也会切 tab');
eq(seen[seen.length - 1].q, { lib: '9', q: 'x' }, 'hard 广播带上新参数');
eq(seen[seen.length - 1].hard, true, 'hard 标记为 true（外部来源）');

console.log('\n== 6. 边界：Windows 盘符 root ==');
_hash = Route.build('assets', { vid: '7', root: 'D:\\视频\\电影' });
p = Route.parse();
eq(p.q.root, 'D:\\视频\\电影', '反斜杠盘符路径穿过 encode/decode 不变形');

console.log('\n== 7. pin 精确锁定：拿真实素材数据验证 ==');
/* 素材数据：优先用本地缓存 tools/.videos.json，没有就直连后端拉一份。
   本机有系统代理，直连 127.0.0.1 必须绕开（node:http 要 agent:false）。 */
function fetchVideosSync() {
  try { return JSON.parse(fs.readFileSync(path.join(__dirname, '.videos.json'), 'utf8')); }
  catch (e) { /* 缓存没有，改走后端 */ }
  // 用子进程跑同步 HTTP，避免为了一个 await 把整个文件改成 async
  const { spawnSync } = require('child_process');
  const r = spawnSync(process.execPath, ['-e', `
    require('http').get({host:'127.0.0.1',port:8771,path:'/api/videos?scope=all',agent:false}, res => {
      let b=''; res.on('data',d=>b+=d); res.on('end',()=>process.stdout.write(b));
    }).on('error',()=>process.exit(1));
  `], { encoding: 'utf8', timeout: 20000 });
  if (r.status === 0 && r.stdout) { try { return JSON.parse(r.stdout); } catch (e) {} }
  return null;
}
const rows = fetchVideosSync();
if (rows) {
  const arr = Array.isArray(rows) ? rows : (rows.items || []);
  console.log(`  （真实数据 ${arr.length} 条）`);

  // 逐个库抽样：每个库随机 6 部，vid+root 必须各自只命中 1 条
  const libs = [...new Set(arr.map(v => v.root))];
  let allSingle = true;
  libs.forEach(root => {
    const sample = arr.filter(v => v.root === root);
    for (let i = 0; i < Math.min(6, sample.length); i++) {
      const t = sample[i];
      const hit = arr.filter(v => rowHit(v, { pin: { vid: +t.id, root: t.root },
        libs: new Set(), dur: new Set(), kw: '' }));
      if (hit.length !== 1 || hit[0].root !== t.root) {
        allSingle = false;
        console.log(`      ✗ ${root} / ${t.name}: 命中 ${hit.length} 条`);
      }
    }
  });
  eq(allSingle, true, '  各库抽样 pin 均唯一命中');

  // pin + 关键词叠加：片名缩小范围，命中数不应变 0
  const t0 = arr.find(v => v.lib_id === 3) || arr[0];
  const hits = arr.filter(v => rowHit(v, { pin: { vid: +t0.id, root: t0.root },
    libs: new Set(), dur: new Set(), kw: t0.name.toLowerCase() }));
  eq(hits.length, 1, '  pin + 片名关键词叠加仍命中 1 条（goAsset 会同时带上两者）');

  // pin + 库 + 时长：多条件与逻辑
  const longOnes = arr.filter(v => v.duration >= 1200);
  const h3 = arr.filter(v => rowHit(v, { pin: null, libs: new Set([3]),
    dur: new Set(['l20']), kw: '' }));
  eq(h3.length, longOnes.filter(v => +v.lib_id === 3).length,
     '  仅库+时长分档：命中数 = 该库 20 分以上的条数');

  // 时长分档边界
  eq(durHit(29.9, new Set(['s30'])), true, '  29.9s 命中 s30');
  eq(durHit(30, new Set(['s30'])), false, '  30s 不再命中 s30（归 m30）');
  eq(durHit(30, new Set(['m30'])), true, '  30s 命中 m30');
  eq(durHit(300, new Set(['m30'])), false, '  300s 不再命中 m30');
  eq(durHit(1200, new Set(['l20'])), true, '  1200s 命中 l20');

  // 找一个"跨库 id 撞号"的素材，验证只比 id 会误伤、加上 root 就精确
  const idMap = {};
  arr.forEach(v => { (idMap[v.id] = idMap[v.id] || []).push(v); });
  const collided = Object.entries(idMap).filter(([, vs]) => vs.length > 1);
  console.log('  跨库撞号 id 组数:', collided.length);
  eq(collided.length > 0, true, '  真实数据里确实存在 id 撞号（说明必须用 root 区分）');

  if (collided.length) {
    const [cid, cvs] = collided.find(([, vs]) => new Set(vs.map(x => x.root)).size > 1)
                    || collided[0];
    const one = cvs[0];
    const naive = arr.filter(v => rowHit(v, { pin: { vid: +cid, root: '' },
      libs: new Set(), dur: new Set(), kw: '' }));
    const exact = arr.filter(v => rowHit(v, { pin: { vid: +cid, root: one.root },
      libs: new Set(), dur: new Set(), kw: '' }));
    eq(exact.length, 1, `  vid=${cid} + root 精确锁定 → 只命中 1 条`);
    eq(exact[0].root, one.root, '  命中的是目标库那部');
    console.log(`      （若不校验 root，会误命中 ${naive.length} 条）`);
  }
} else {
  console.log('  （跳过：后端未启动，取不到真实素材数据）');
}

console.log(`\n${fail === 0 ? '全部通过' : '有失败'}：${pass} passed, ${fail} failed\n`);
process.exit(fail === 0 ? 0 : 1);
