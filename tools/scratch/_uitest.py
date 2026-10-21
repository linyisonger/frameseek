# -*- coding: utf-8 -*-
"""FrameSeek UI 体检 v2：点击全链路 + 可点击性 + 溢出 + 404"""
import os, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8771"
OUT = os.path.abspath("_out/ui")
os.makedirs(OUT, exist_ok=True)
errs, bad_http = [], []


def log(s):
    print(s, flush=True)


HITTEST = """
(() => {
  const modal = [...document.querySelectorAll('.modal')].find(m => {
    const cs = getComputedStyle(m);
    return cs.display !== 'none' && cs.visibility !== 'hidden'
      && m.getBoundingClientRect().height > 0;
  }) || null;
  const scope = modal ? modal : document;
  const out = [];
  scope.querySelectorAll('button, select, input, a, label, .bxrow, .vmore, [onclick]')
    .forEach(el => {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (cs.visibility === 'hidden' || cs.display === 'none'
        || +cs.opacity < 0.05 || r.width < 1 || r.height < 1) return;
    if (r.bottom < 0 || r.top > innerHeight) return;
    // 横向滚出视口的（窄屏导航条）不算"点不到"，跳过
    if (r.left > innerWidth - 1 || r.right < 1) return;
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1);
    const hit = document.elementFromPoint(cx, cy);
    const ok = hit && (el === hit || el.contains(hit) || hit.contains(el));
    out.push({tag: el.tagName.toLowerCase(), id: el.id || '',
      cls: (el.className || '').toString().slice(0, 32),
      txt: (el.textContent || '').trim().slice(0, 16),
      w: Math.round(r.width), h: Math.round(r.height),
      hit: ok,
      hitBy: hit ? hit.tagName.toLowerCase() + '.' +
        (hit.className || '').toString().split(' ')[0] : 'none'});
  });
  return {out, hasModal: !!modal};
})()
"""

OVERFLOW = """
(() => {
  const bad = [];
  document.querySelectorAll('.panel.on *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    // 文字被横向截断（无 overflow 样式兜底）
    if (el.scrollWidth > el.clientWidth + 2 &&
        getComputedStyle(el).overflowX === 'visible' &&
        el.children.length === 0) {
      bad.push({t: el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''),
        txt: (el.textContent || '').trim().slice(0, 24),
        sw: el.scrollWidth, cw: el.clientWidth});
    }
    // 溢出视口右侧
    if (r.right > innerWidth + 1) {
      bad.push({t: 'OVERFLOW-X ' + el.tagName.toLowerCase() +
        (el.id ? '#' + el.id : ''), txt: (el.textContent || '').trim().slice(0, 24),
        sw: Math.round(r.right), cw: innerWidth});
    }
  });
  return bad.slice(0, 20);
})()
"""


def scan(pg, tag):
    res = pg.evaluate(HITTEST)
    items = res["out"]
    bad = [r for r in items if not r["hit"]]
    tiny = [r for r in items if r["h"] < 16 or r["w"] < 16]
    log(f"  扫描 {tag}{'(弹窗内)' if res['hasModal'] else ''}: "
        f"{len(items)} 个 | 点不到 {len(bad)} | 点击区过小 {len(tiny)}")
    for b in bad[:8]:
        log(f"    ✗ {b['tag']}#{b['id']}.{b['cls']} 『{b['txt']}』 "
            f"{b['w']}x{b['h']} 被 {b['hitBy']} 挡")
    for t in tiny[:6]:
        log(f"    ! 区小 {t['tag']}#{t['id']}.{t['cls']} 『{t['txt']}』 {t['w']}x{t['h']}")
    return bad


def overflow(pg, tag):
    bad = pg.evaluate(OVERFLOW)
    trunc = [b for b in bad if not b["t"].startswith("OVERFLOW")]
    over = [b for b in bad if b["t"].startswith("OVERFLOW")]
    log(f"  {tag}: 文字截断 {len(trunc)} | 溢出视口 {len(over)}")
    for b in (trunc + over)[:8]:
        log(f"    ! {b['t']} 『{b['txt']}』 {b['sw']}>{b['cw']}")
    return bad


def pick_test_lib(pg):
    opts = pg.eval_on_selector_all(
        "#bxPick option", "els => els.map(e => ({v: e.value, t: e.textContent}))")
    tgt = next((o["v"] for o in opts if "_testlib" in o["v"]), None)
    pg.select_option("#bxPick", tgt or opts[0]["v"])
    pg.wait_for_selector("#bxRows .bxrow", timeout=60000)
    pg.wait_for_timeout(1500)


def main():
    with sync_playwright() as p:
        exe = (r"C:\Users\Administrator\AppData\Local\ms-playwright"
               r"\chromium-1223\chrome-win64\chrome.exe")
        b = p.chromium.launch(headless=True,
                              executable_path=exe if os.path.exists(exe) else None)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("console", lambda m: errs.append(f"[{m.type}] {m.text}")
              if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(f"[pageerror] {e}"))
        pg.on("response", lambda r: bad_http.append(f"{r.status} {r.url}")
              if r.status >= 400 else None)

        log("=== 1. 复查页 ===")
        pg.goto(BASE + "/#box", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2500)
        pick_test_lib(pg)
        log(f"  清单 {pg.eval_on_selector_all('#bxRows .bxrow', 'e=>e.length')} 行")
        pg.wait_for_timeout(2500)
        nw = pg.evaluate("() => document.getElementById('bxImg').naturalWidth")
        log(f"  帧图宽 {nw}")
        scan(pg, "复查页")
        overflow(pg, "复查页")
        pg.screenshot(path=os.path.join(OUT, "01_box.png"))

        log("=== 2. 编辑台词（弹窗 → 改一个字 → 保存）===")
        pg.evaluate("() => {const t=document.getElementById('toast');"
                    "if(t){t.classList.remove('on');t.textContent=''}}")
        pg.click("#bxEdit")
        pg.wait_for_selector("#mYes", timeout=10000)
        scan(pg, "编辑弹窗")
        pg.fill("#bxEText", "测试改字")
        pg.click("#mYes")
        for _ in range(60):
            pg.wait_for_timeout(500)
            t = pg.evaluate(
                "() => {const e = document.getElementById('toast');"
                " return e ? e.textContent : ''}")
            if t and "保存" in t:
                break
        log(f"  toast: {t}")
        if "保存" not in (t or ""):
            log("  ✗ 保存 toast 没出现，可能没写入")
        pg.wait_for_timeout(2000)

        log("=== 3. 播放这句 → 弹窗 → 关闭 ===")
        pg.click("#bxPlay")
        pg.wait_for_timeout(2500)
        opened = pg.evaluate(
            "() => !document.getElementById('player').classList.contains('hidden')")
        log(f"  播放器打开: {opened}")
        if opened:
            closebtn = pg.query_selector("#btnClosePlayer")
            vis = pg.evaluate(
                "() => {const b = document.getElementById('btnClosePlayer');"
                "const r = b.getBoundingClientRect();"
                "const h = document.elementFromPoint(r.left + r.width/2,"
                " r.top + r.height/2);"
                "return {w: Math.round(r.width), h: Math.round(r.height),"
                " hit: !!(h && (h === b || b.contains(h)))}}")
            log(f"  关闭按钮 {vis['w']}x{vis['h']} 可点: {vis['hit']}")
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(1200)
            still = pg.evaluate(
                "() => !document.getElementById('player').classList.contains('hidden')")
            log(f"  ESC 后仍开着: {still}")
            if still:
                closebtn.click()
                pg.wait_for_timeout(1000)
                log("  点关闭按钮后: "
                    + str(pg.evaluate("() => !document.getElementById('player')"
                                      ".classList.contains('hidden')")))
            pg.wait_for_timeout(500)
            paused = pg.evaluate(
                "() => document.getElementById('video').paused")
            log(f"  关闭后视频已暂停: {paused}")
        pg.screenshot(path=os.path.join(OUT, "03_play.png"))

        log("=== 4. 检索页 ===")
        pg.click('.nav-item[data-tab="search"]')
        pg.wait_for_timeout(1200)
        pg.fill("#q", "伯虎")
        pg.click("#btnSearch")
        pg.wait_for_timeout(4000)
        n = pg.eval_on_selector_all("#results .row, #results > div", "e=>e.length")
        log(f"  检索结果 {n} 条")
        scan(pg, "检索页")
        overflow(pg, "检索页")
        pg.screenshot(path=os.path.join(OUT, "04_search.png"))

        log("=== 5. 素材页：三点菜单 ===")
        pg.click('.nav-item[data-tab="assets"]')
        pg.wait_for_timeout(3500)
        cards = pg.eval_on_selector_all(".vmore", "e=>e.length")
        log(f"  ··· 按钮 {cards} 个")
        if cards:
            pg.click(".vmore")
            pg.wait_for_timeout(800)
            vis = pg.evaluate("() => [...document.querySelectorAll('.vmenu')]"
                              ".filter(m=>!m.classList.contains('hidden')).length")
            log(f"  菜单展开 {vis} 个")
            scan(pg, "素材菜单")
            pg.keyboard.press("Escape")
            pg.mouse.click(700, 60)
            pg.wait_for_timeout(600)
        scan(pg, "素材页")
        overflow(pg, "素材页")
        pg.screenshot(path=os.path.join(OUT, "05_assets.png"))

        log("=== 6. 素材库页 ===")
        pg.click('.nav-item[data-tab="libs"]')
        pg.wait_for_timeout(2000)
        scan(pg, "素材库页")
        overflow(pg, "素材库页")
        pg.screenshot(path=os.path.join(OUT, "06_libs.png"))

        log("=== 7. 窄屏 820 / 375 ===")
        for w in (820, 375):
            pg.set_viewport_size({"width": w, "height": 900})
            pg.click('.nav-item[data-tab="box"]')
            pg.wait_for_timeout(2500)
            scan(pg, f"窄屏{w}")
            overflow(pg, f"窄屏{w}")
            nav = pg.evaluate(
                "() => {const n = document.querySelector('nav');"
                "return {sw: n.scrollWidth, cw: n.clientWidth,"
                " ox: getComputedStyle(n).overflowX}}")
            log(f"  导航条 内容宽 {nav['sw']} / 可视 {nav['cw']} "
                f"overflow-x={nav['ox']}"
                + ("（可横向滚动）" if nav["sw"] > nav["cw"]
                   and nav["ox"] in ("auto", "scroll") else "（无需滚动）"))
            # 滚到最右再扫一次，确认滚过去之后都能点
            pg.evaluate("() => {const n=document.querySelector('nav');"
                        "n.scrollLeft = n.scrollWidth}")
            pg.wait_for_timeout(600)
            scan(pg, f"窄屏{w}滚到末尾")
            pg.screenshot(path=os.path.join(OUT, f"07_{w}.png"))

        log("=== 8. 控制台与 HTTP ===")
        log(f"  console 错误 {len(errs)}")
        for e in errs[:10]:
            log("    " + e[:140])
        log(f"  HTTP >=400 {len(bad_http)}")
        for r in bad_http[:10]:
            log("    " + r[:140])
        b.close()


if __name__ == "__main__":
    main()
