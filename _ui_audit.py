# -*- coding: utf-8 -*-
"""FrameSeek UI 习惯审核 v3：操作大小 + 行为习惯 + 误触风险。

审核维度（对应"日常行为习惯 / 操作大小是否符合用户习惯"）：
  A 操作大小  命中区 <24px 记偏小；<16 记过小；相邻可点元素间距 <4px 记误触风险
  B 点击可达  elementFromPoint 命中测试（被遮挡 = 点不到）
  C 键盘习惯  Tab 焦点顺序、焦点环可见、Esc 可关浮层
  D 反馈习惯  hover 有无 cursor/变色、按钮 disabled 态、loading 反馈
  E 危险操作  删除/清空类是否 danger 色 + 二次确认
  F 空状态    空列表有没有"下一步做什么"的引导
  G 视觉      文字截断、溢出视口、暗色正文对比度
  H 弹窗      每个典型弹窗都过一遍 A/B/G
"""
import os
import sys
import json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8771"
OUT = os.path.abspath("_out/uiaudit")
os.makedirs(OUT, exist_ok=True)
ISSUES = []          # (级别, 页面, 类别, 描述)
ERRS, BAD_HTTP = [], []


def log(s):
    print(s, flush=True)


def issue(level, page, cat, msg):
    ISSUES.append((level, page, cat, msg))
    mark = {"P0": "✗✗", "P1": "✗", "P2": "!", "OK": "✓"}[level]
    log(f"    {mark} [{cat}] {msg}")


# ---------------------------------------------------------------- 采集脚本
JS_SIZE = r"""
((scopeSel) => {
  const sel = 'button, select, input, a[href], label, .vmore, [onclick], '
            + '.nav-item, .bxrow';
  const scope = scopeSel ? document.querySelector(scopeSel) : document;
  const vis = [];
  scope.querySelectorAll(sel).forEach(el => {
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    if (cs.visibility === 'hidden' || cs.display === 'none'
        || +cs.opacity < 0.05 || r.width < 1 || r.height < 1) return;
    if (r.bottom < 0 || r.top > innerHeight) return;
    if (r.left > innerWidth - 1 || r.right < 1) return;
    vis.push({el, r, cs});
  });
  const small = [], tiny = [], crowd = [], nopointer = [];
  for (let i = 0; i < vis.length; i++) {
    const {el, r, cs} = vis[i];
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1);
    const hit = document.elementFromPoint(cx, cy);
    const reachable = hit && (el === hit || el.contains(hit) || hit.contains(el));
    const d = {tag: el.tagName.toLowerCase(), id: el.id || '',
      cls: (el.className || '').toString().split(' ')[0] || '',
      txt: (el.textContent || '').trim().slice(0, 14),
      w: Math.round(r.width), h: Math.round(r.height),
      hitBy: hit ? hit.tagName.toLowerCase() : 'none'};
    // checkbox 视觉只有 16px 是正常的（真正的点击区在包裹它的 label 上），
    // 尺寸检查跳过它，遮挡检查保留。
    const isBox = el.type === 'checkbox';
    // 链接/按钮类必须有 pointer 光标提示"可点"
    if ((el.tagName === 'BUTTON' || el.tagName === 'A' || el.hasAttribute('onclick'))
        && cs.cursor !== 'pointer' && !el.disabled) nopointer.push(d);
    if (!reachable) { d.reach = false; small.push(d); continue; }
    if (isBox) continue;
    if (r.height < 16 || r.width < 16) tiny.push(d);
    else if (r.height < 24 || r.width < 24) small.push(d);
    // 与下一个可见可点元素的间距（同一行的才算误触）
    for (let j = i + 1; j < vis.length; j++) {
      const r2 = vis[j].r;
      // 连体分段控件（.segs，iOS segmented 风格）贴边是刻意设计，
      // 靠 hover/on 状态区分，不算误触
      if (el.closest('.segs') && vis[j].el.closest('.segs')) continue;
      const sameRow = Math.abs(r.top - r2.top) < 6
        || Math.abs((r.top + r.height) - (r2.top + r2.height)) < 6;
      if (!sameRow) continue;
      const gap = Math.max(r.left - r2.right, r2.left - r.right);
      if (gap >= -0.5 && gap < 4) {
        crowd.push({a: d.txt || d.id, b: vis[j].el.textContent.trim().slice(0, 14)
          || vis[j].el.id, gap: Math.round(gap * 10) / 10});
        break;
      }
    }
  }
  return {n: vis.length, tiny, small, crowd, nopointer,
          unreachable: small.filter(x => x.reach === false)};
})
"""

JS_OVERFLOW = r"""
(() => {
  const bad = [];
  document.querySelectorAll('.panel.on *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    // ::after 外扩命中区的元素（vmore/close2）scrollWidth 会含外扩量，
    // 不是文字截断，跳过
    const cls = (el.className || '').toString();
    if (/\b(vmore|close2)\b/.test(cls)) return;
    if (el.scrollWidth > el.clientWidth + 2
        && getComputedStyle(el).overflowX === 'visible'
        && el.children.length === 0)
      bad.push({t: el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''),
        txt: (el.textContent || '').trim().slice(0, 24),
        sw: el.scrollWidth, cw: el.clientWidth});
    if (r.right > innerWidth + 1)
      bad.push({t: 'OVERFLOW ' + el.tagName.toLowerCase()
        + (el.id ? '#' + el.id : ''), txt: (el.textContent || '').trim().slice(0, 24),
        sw: Math.round(r.right), cw: innerWidth});
  });
  return bad.slice(0, 15);
})()
"""

JS_CONTRAST = r"""
((scopeSel) => {
  // 暗色主题抽查：正文类元素的文字 vs 背景亮度差
  const scope = scopeSel ? document.querySelector(scopeSel) : document;
  function lum(c) {
    const m = c.match(/[\d.]+/g); if (!m) return null;
    const [r, g, b] = m.slice(0, 3).map(Number);
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92
      : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  }
  function bgOf(el) {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = getComputedStyle(n).backgroundColor;
      if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c;
      n = n.parentElement;
    }
    return getComputedStyle(document.body).backgroundColor;
  }
  const bad = [];
  scope.querySelectorAll('p, span, b, td, div, label, a, button').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || +cs.opacity < 0.05) return;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return;
    const txt = (el.textContent || '').trim();
    if (!txt || el.children.length) return;
    const l1 = lum(cs.color), l2 = lum(bgOf(el));
    if (l1 == null || l2 == null) return;
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    if (ratio < 4.5)
      bad.push({txt: txt.slice(0, 20), ratio: Math.round(ratio * 100) / 100,
        size: cs.fontSize, color: cs.color,
        where: (el.closest('.panel.on') || {}).id
          || (el.closest('.modal-box') ? '弹窗' : '?')});
  });
  return bad.slice(0, 12);
})
"""

JS_EMPTYSTATE = r"""
(() => {
  // 空面板：既没有数据行、也没有任何可操作控件、连一句引导文字都没有
  const out = [];
  document.querySelectorAll('.panel.on').forEach(p => {
    const txt = (p.textContent || '').trim();
    const rows = p.querySelectorAll('.bxrow, .vcard, #results .row, #results > div, '
      + '.igrow, .librow, table tbody tr');
    const ctrls = p.querySelectorAll('input, button, select, a[href]');
    if (!rows.length && !ctrls.length && txt.length < 30)
      out.push({panel: p.id || p.className.split(' ')[0], hint: txt.slice(0, 60)});
  });
  return out;
})()
"""

JS_FOCUS = r"""
(() => {
  // Tab 序列前 12 个 + 焦点环是否可见
  const seq = [];
  const focusables = [...document.querySelectorAll(
    'button:not([disabled]), select:not([disabled]), input:not([disabled]), '
    + 'a[href], [tabindex]:not([tabindex="-1"])')]
    .filter(el => { const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0
        && getComputedStyle(el).visibility !== 'hidden'; });
  const first = focusables[0];
  let ring = null;
  if (first) {
    first.focus();
    const cs = getComputedStyle(first);
    ring = {outline: cs.outlineStyle, width: cs.outlineWidth,
            shadow: cs.boxShadow !== 'none'};
  }
  return {count: focusables.length, firstId: first ? (first.id || first.tagName) : null,
          ring};
})()
"""


def audit_page(pg, name, clickable_probe=True):
    """一页的完整审核。"""
    log(f"=== {name} ===")
    res = pg.evaluate(JS_SIZE, None)
    log(f"  可交互元素 {res['n']} 个")
    if res["unreachable"]:
        for u in res["unreachable"][:8]:
            issue("P0", name, "点击可达",
                  f"{u['tag']}#{u['id']}.{u['cls']} 『{u['txt']}』 "
                  f"{u['w']}x{u['h']} 被 {u['hitBy']} 挡住")
    else:
        log("    ✓ 全部可点（无遮挡）")
    if res["tiny"]:
        for t in res["tiny"][:8]:
            issue("P1", name, "操作大小",
                  f"点击区过小 {t['tag']}#{t['id']}.{t['cls']} 『{t['txt']}』 "
                  f"{t['w']}x{t['h']}px（<16）")
    elif res["small"]:
        for s in res["small"][:8]:
            issue("P2", name, "操作大小",
                  f"点击区偏小 {s['tag']}#{s['id']}.{s['cls']} 『{s['txt']}』 "
                  f"{s['w']}x{s['h']}px（<24）")
    else:
        log("    ✓ 点击区均 ≥24px")
    if res["nopointer"]:
        for p in res["nopointer"][:6]:
            issue("P2", name, "反馈",
                  f"无 pointer 光标 {p['tag']}#{p['id']}.{p['cls']} 『{p['txt']}』")
    if res["crowd"]:
        seen = set()
        for c in res["crowd"]:
            k = (c["a"], c["b"])
            if k in seen:
                continue
            seen.add(k)
            issue("P2", name, "误触风险",
                  f"『{c['a']}』与『{c['b']}』间距仅 {c['gap']}px（<4）")
    bad = pg.evaluate(JS_OVERFLOW)
    trunc = [b for b in bad if not b["t"].startswith("OVERFLOW")]
    over = [b for b in bad if b["t"].startswith("OVERFLOW")]
    for b in trunc[:6]:
        issue("P2", name, "视觉", f"文字截断 {b['t']} 『{b['txt']}』 {b['sw']}>{b['cw']}")
    for b in over[:6]:
        issue("P1", name, "视觉", f"溢出视口 {b['t']} 『{b['txt']}』 {b['sw']}>{b['cw']}")
    if not trunc and not over:
        log("    ✓ 无截断/溢出")
    low = pg.evaluate(JS_CONTRAST, None)
    for c in low[:6]:
        issue("P2", name, "可读性",
              f"对比度 {c['ratio']}『{c['txt']}』 {c['size']} {c['color']}"
              f" @{c.get('where', '?')}")
    if low:
        log(f"    ! 低对比度文字 {len(low)} 处")
    empt = pg.evaluate(JS_EMPTYSTATE)
    if empt:
        for e in empt[:3]:
            issue("P2", name, "空状态", f"面板 {e['panel']} 无内容且无引导")
    pg.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=False)


def audit_modal(pg, name, open_fn, danger_btn=None):
    log(f"--- 弹窗：{name} ---")
    open_fn()
    # 不同弹窗确认按钮 id 不同（mYes / mDel），等 mfoot 里第一个按钮出现即可
    pg.wait_for_selector(".modal-box .mfoot button", timeout=8000)
    pg.wait_for_timeout(300)
    res = pg.evaluate(JS_SIZE, ".modal-box")
    log(f"  弹窗内可交互 {res['n']} 个")
    for u in (res["unreachable"] or [])[:6]:
        issue("P0", name, "点击可达",
              f"{u['tag']}#{u['id']} 『{u['txt']}』 {u['w']}x{u['h']} 被挡")
    for t in (res["tiny"] or [])[:6]:
        issue("P1", name, "操作大小",
              f"点击区过小 {t['tag']}#{t['id']} 『{t['txt']}』 {t['w']}x{t['h']}")
    for s in (res["small"] or [])[:6]:
        issue("P2", name, "操作大小",
              f"点击区偏小 {s['tag']}#{s['id']} 『{s['txt']}』 {s['w']}x{s['h']}")
    for c in (res["crowd"] or [])[:4]:
        issue("P2", name, "误触风险",
              f"『{c['a']}』与『{c['b']}』间距仅 {c['gap']}px")
    low = pg.evaluate(JS_CONTRAST, ".modal-box")
    for c in low[:6]:
        issue("P2", name, "可读性",
              f"对比度 {c['ratio']}『{c['txt']}』 {c['size']}")
    if danger_btn:
        # 危险操作必须"点两次"或弹窗里有醒目警示
        pg.click(danger_btn)
        pg.wait_for_timeout(300)
        st = pg.evaluate(
            "() => {const b = document.getElementById('mDel');"
            " return b ? {t: b.textContent, cls: b.className} : null}")
        if st and ("再点一次" in st["t"] or "确认" in st["t"]):
            log(f"    ✓ 危险操作需二次确认（{st['t']}）")
        else:
            issue("P1", name, "危险操作", "删除按钮没有二次确认机制")
    # Esc 应该能关（先拍照再关：关了就拍不到弹窗本身了）
    pg.screenshot(path=os.path.join(OUT, f"modal_{name}.png"))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(400)
    still = pg.evaluate(
        "() => !document.getElementById('modal').classList.contains('hidden')")
    if still:
        issue("P1", name, "键盘习惯", "Esc 关不掉弹窗")
    else:
        log("    ✓ Esc 可关")


def main():
    with sync_playwright() as p:
        exe = (r"C:\Users\Administrator\AppData\Local\ms-playwright"
               r"\chromium-1223\chrome-win64\chrome.exe")
        b = p.chromium.launch(headless=True,
                              executable_path=exe if os.path.exists(exe) else None)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("console", lambda m: ERRS.append(f"[{m.type}] {m.text}")
              if m.type == "error" else None)
        pg.on("pageerror", lambda e: ERRS.append(f"[pageerror] {e}"))
        pg.on("response", lambda r: BAD_HTTP.append(f"{r.status} {r.url}")
              if r.status >= 400 else None)

        # ---- 1 素材库页（首页） ----
        pg.goto(BASE + "/#libs", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2000)
        audit_page(pg, "1_素材库页")
        f = pg.evaluate(JS_FOCUS)
        log(f"  Tab 焦点：可聚焦 {f['count']} 个，首个 {f['firstId']}，"
            f"焦点环 outline={f['ring'] and f['ring']['outline']}"
            f"/w={f['ring'] and f['ring']['width']}")
        if f["ring"] and f["ring"]["outline"] == "none" and not f["ring"]["shadow"]:
            issue("P2", "1_素材库页", "键盘习惯", "焦点环不可见（outline:none 无替代）")

        # ---- 2 素材页 ----
        pg.click('.nav-item[data-tab="assets"]')
        pg.wait_for_timeout(3500)
        audit_page(pg, "2_素材页")

        # ---- 3 检索页 ----
        pg.click('.nav-item[data-tab="search"]')
        pg.wait_for_timeout(1200)
        audit_page(pg, "3_检索页")

        # ---- 4 方框复查页 ----
        pg.click('.nav-item[data-tab="box"]')
        pg.wait_for_timeout(3000)
        audit_page(pg, "4_方框复查页")

        # ---- 5 弹窗系列（素材页的菜单） ----
        pg.click('.nav-item[data-tab="assets"]')
        pg.wait_for_timeout(2500)
        more = pg.query_selector(".vmore")
        if more:
            more.click()
            pg.wait_for_timeout(500)

            def open_rt():
                pg.evaluate("askRetranscribe(0)")
            audit_modal(pg, "5_重新识别弹窗", open_rt)

            def open_asr():
                pg.evaluate("askTranscribeAsr(0)")
            audit_modal(pg, "6_语音识别弹窗", open_asr)

            def open_reimport():
                pg.evaluate("askReimportSrt(0)")
            audit_modal(pg, "7_重新导入弹窗", open_reimport)

            def open_del():
                pg.evaluate("askDelVideo(0,true)")   # 带文件删除：最危险的那种
            audit_modal(pg, "8_删除素材弹窗", open_del, danger_btn="#mDel")

            def open_del2():
                pg.evaluate("askDelVideo(0,false)")
            audit_modal(pg, "8b_移除记录弹窗", open_del2)

        # ---- 9 窄屏 ----
        for w in (375, 820):
            pg.set_viewport_size({"width": w, "height": 900})
            pg.click('.nav-item[data-tab="assets"]')
            pg.wait_for_timeout(2500)
            audit_page(pg, f"9_窄屏{w}_素材页")
            pg.evaluate("() => {const n=document.querySelector('nav');"
                        "n.scrollLeft = n.scrollWidth}")
            pg.wait_for_timeout(500)
            audit_page(pg, f"9_窄屏{w}_导航末尾")

        log("=== 汇总 ===")
        log(f"console 错误 {len(ERRS)}")
        for e in ERRS[:8]:
            log("    " + e[:150])
        log(f"HTTP >=400 {len(BAD_HTTP)}")
        for r in BAD_HTTP[:8]:
            log("    " + r[:150])
        cnt = {}
        for lv, _, cat, _ in ISSUES:
            cnt[(lv, cat)] = cnt.get((lv, cat), 0) + 1
        log("问题分布（级别/类别 -> 条数）:")
        for (lv, cat), n in sorted(cnt.items()):
            log(f"  {lv} {cat}: {n}")
        with open(os.path.join(OUT, "issues.json"), "w", encoding="utf-8") as fh:
            json.dump([{"level": l, "page": p, "cat": c, "msg": m}
                       for l, p, c, m in ISSUES], fh, ensure_ascii=False, indent=1)
        b.close()


if __name__ == "__main__":
    main()
