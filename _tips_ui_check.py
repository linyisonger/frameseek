# -*- coding: utf-8 -*-
"""按钮悬停说明回归。

说明不写在总览页里（没人会切过去看），而是挂在每个功能按钮的 title 上，
鼠标停一秒就出。这个脚本守住这条口径：哪个按钮的说明掉了，这里就红。

用法：先起服务（python app.py），再 `python _tips_ui_check.py`。
"""
import os
import sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get("FS_BASE", "http://127.0.0.1:8771")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)

# 静态按钮：切到对应页就能查到
STATIC = {
    "libs":   ["#btnAddLib", "#btnScanAll"],
    "search": ["#btnSearch", "#fast"],
    "assets": ["#vMoreBtn", "#vResetBtn", "#vfilter"],
    "gen":    ["#btnGen", "#btnGenRef", "#genMore", ".gen-ex"],
    "hd":     ["#hdUpScript", "#hdInit", "#hdSuggest", "#hdRegions", "#hdUpImage",
               "#hdTplAnn", "#hdAnnotate", "#hdApprove", "#hdPreview", "#hdRender",
               "#hdAudition", "#hdMix", "#hdQa", "#hdClear"],
    "box":    ["#bxPick", "#bxReload", "#bxFilter", "#bxSegs button"],
}

# 动态按钮：要等数据渲染出来才有
DYNAMIC = [
    ("assets", ".vcard", ".vmore"),           # 素材卡片「···」
    ("assets", ".vcard", ".vmenu a"),         # 卡片菜单里的每一项
]

OK = 0
BAD = 0


def check(cond, msg):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ✓ {msg}")
    else:
        BAD += 1
        print(f"  ✗ {msg}")


def count_tips(pg, sel):
    """返回 (带说明的数量, 总数, 缺说明的文案)。"""
    rows = pg.eval_on_selector_all(
        sel, "els => els.map(e => [e.textContent.trim().slice(0, 14),"
             " (e.getAttribute('title')||'').trim()])")
    miss = [t for t, tip in rows if len(tip) < 6]
    return len(rows) - len(miss), len(rows), miss


def main():
    exe = (r"C:\Users\Administrator\AppData\Local\ms-playwright"
           r"\chromium-1223\chrome-win64\chrome.exe")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True,
                              executable_path=exe if os.path.exists(exe) else None)
        pg = b.new_page(viewport={"width": 1440, "height": 960})
        pg.goto(BASE + "/#libs", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(1500)

        print("\n=== 1. 侧边栏不该再有「说明」总览页 ===")
        tabs = pg.eval_on_selector_all(
            ".nav-item", "els => els.map(e => e.dataset.tab)")
        check("help" not in tabs, f"导航里没有 help（现有：{tabs}）")

        print("\n=== 2. 静态按钮都有悬停说明 ===")
        for tab, sels in STATIC.items():
            pg.evaluate(f"() => switchTab('{tab}')")
            pg.wait_for_timeout(400)
            for sel in sels:
                n, tot, miss = count_tips(pg, sel)
                check(tot > 0 and n == tot,
                      f"{tab}/{sel} {n}/{tot}"
                      + (f"（缺：{miss}）" if miss else ""))

        print("\n=== 3. 动态生成的按钮也要有 ===")
        for tab, wait_sel, sel in DYNAMIC:
            pg.evaluate(f"() => switchTab('{tab}')")
            try:
                pg.wait_for_selector(wait_sel, timeout=60000)
            except Exception:
                check(False, f"{tab} 页没渲染出 {wait_sel}，跳过不了")
                continue
            pg.wait_for_timeout(600)
            n, tot, miss = count_tips(pg, sel)
            check(tot > 0 and n == tot,
                  f"{tab}/{sel} {n}/{tot}" + (f"（缺：{miss}）" if miss else ""))

        pg.screenshot(path=os.path.join(OUT, "tips_page.png"))
        b.close()

    print(f"\n{OK} 项通过，{BAD} 项失败")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
