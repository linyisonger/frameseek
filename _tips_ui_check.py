# -*- coding: utf-8 -*-
"""按钮悬停说明回归（Vue 3 + Element Plus 版）。

说明不写在总览页里（没人会切过去看），而是挂在每个功能按钮的 title 上，
鼠标停一秒就出。这个脚本守住这条口径：哪个按钮的说明掉了，这里就红。

Element Plus 组件会把未声明的属性（title、id）透传到根元素上，
所以 id 和 title 断言跟原生写法差别不大；下拉菜单项要打开后才在
DOM 里可见，用 :visible 挑出真正弹出的那个 popper。

用法：先起服务（python app.py），再 `python _tips_ui_check.py`。
"""
import os
import sys
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pw import launch

BASE = os.environ.get("FS_BASE", "http://127.0.0.1:8771")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)

# 静态按钮：切到对应页就能查到（id 由 Element Plus 透传到组件根元素）
STATIC = {
    "libs":   ["#btnAddLib", "#btnScanAll", "#libPath"],
    "search": ["#btnSearch", "#fast"],
    "assets": ["#vMoreBtn", "#vResetBtn", "#vfilter"],
    "gen":    ["#btnGen", "#btnGenRef", "#genMore",
               ".fs-composer-row .el-button"],
    "hd":     ["#hdUpScript", "#hdInit", "#hdSuggest", "#hdRegions", "#hdUpImage",
               "#hdTplAnn", "#hdAnnotate", "#hdApprove", "#hdPreview", "#hdRender",
               "#hdAudition", "#hdMix", "#hdQa", "#hdClear"],
    "box":    ["#bxLibPick", "#bxSearch", "#bxPick", "#bxReload", "#bxFilter"],
}

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
    """返回 (带说明的数量, 总数, 缺说明的文案)。

    Element Plus 组件会把 id 透传到内部原生 input、title 留在组件根节点，
    两者不在同一个元素上。但浏览器悬停提示本来就是「取最近的带 title 祖先」，
    所以这里按 closest('[title]') 判定——只要鼠标停上去真能弹出说明就算过。"""
    rows = pg.eval_on_selector_all(
        sel, "els => els.map(e => [e.textContent.trim().slice(0, 14),"
             " ((e.closest('[title]') || {}).getAttribute"
             " ? (e.closest('[title]').getAttribute('title') || '') : '')])")
    miss = [t for t, tip in rows if len(tip) < 6]
    return len(rows) - len(miss), len(rows), miss


def goto_tab(pg, tab):
    pg.click(f'.fs-menu .el-menu-item[data-tab="{tab}"]')
    pg.wait_for_timeout(700)


def main():
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1440, "height": 960})
        pg.goto(BASE + "/#libs", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector(".fs-menu .el-menu-item", timeout=20000)
        pg.wait_for_timeout(1800)

        print("\n=== 1. 侧边栏不该再有「说明」总览页 ===")
        tabs = pg.eval_on_selector_all(
            ".fs-menu .el-menu-item", "els => els.map(e => e.dataset.tab)")
        check("help" not in tabs, f"导航里没有 help（现有：{tabs}）")

        print("\n=== 2. 静态按钮都有悬停说明 ===")
        for tab, sels in STATIC.items():
            goto_tab(pg, tab)
            for sel in sels:
                n, tot, miss = count_tips(pg, sel)
                check(tot > 0 and n == tot,
                      f"{tab}/{sel} {n}/{tot}"
                      + (f"（缺：{miss}）" if miss else ""))

        print("\n=== 3. 动态生成的按钮也要有 ===")
        # 素材卡片「···」按钮（Element Plus 会把 title 透传到 el-button 上）
        goto_tab(pg, "assets")
        try:
            pg.wait_for_selector(".fs-vcard", timeout=60000)
        except Exception:
            check(False, "素材页没渲染出卡片")
        pg.wait_for_timeout(800)
        n, tot, miss = count_tips(pg, ".fs-vmore")
        check(tot > 0 and n == tot,
              f"assets/.fs-vmore {n}/{tot}" + (f"（缺：{miss}）" if miss else ""))

        # 下拉菜单项：点开第一张卡片的菜单，检查弹出的那一份
        pg.click(".fs-vmore")
        pg.wait_for_timeout(800)
        n, tot, miss = count_tips(pg, ".el-dropdown-menu:visible .el-dropdown-menu__item")
        check(tot >= 4 and n == tot,
              f"assets/菜单项 {n}/{tot}" + (f"（缺：{miss}）" if miss else ""))
        pg.keyboard.press("Escape")
        pg.mouse.click(700, 60)
        pg.wait_for_timeout(300)

        print("\n=== 4. 方框复查的动作按钮 ===")
        goto_tab(pg, "box")
        pg.wait_for_selector("#bxSum", timeout=30000)
        pg.wait_for_timeout(1200)
        for sel in ["#bxProbe", "#bxAsr", "#bxEdit", "#bxPlay", "#bxPrev", "#bxNext"]:
            n, tot, miss = count_tips(pg, sel)
            check(tot > 0 and n == tot, f"box/{sel} {n}/{tot}")

        pg.screenshot(path=os.path.join(OUT, "tips_page.png"))
        b.close()

    print(f"\n{OK} 项通过，{BAD} 项失败")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
