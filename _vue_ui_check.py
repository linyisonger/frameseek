# -*- coding: utf-8 -*-
"""Vue 3 + Element Plus 版界面冒烟检查。

整个前端从「原生 JS 拼 innerHTML」改成「Vue 组件 + Element Plus 组件」之后，
这个脚本守住几件最容易悄悄坏掉的事：
  1. vendor 里的依赖没 404、Vue 挂载成功（#app 有内容、没有 v-cloak 残留）
  2. 七个页签都能切进去、各自渲染出内容
  3. 浏览器控制台没有报错（模板编译错、组件没注册都会在这里冒出来）
  4. Element Plus 深色主题真的生效（不是白底黑字的默认亮色）
  5. 各页关键控件还在，且点得动

用法：先起服务（python app.py），再 `python _vue_ui_check.py`。
"""
import os
import sys
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pw import launch        # 本机 chromium 版本对齐用的启动辅助

BASE = os.environ.get("FS_BASE", "http://127.0.0.1:8771")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)

OK = 0
BAD = 0
ERRS = []


def check(cond, msg):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        BAD += 1
        ERRS.append(msg)
        print(f"  FAIL {msg}")


def main():
    with sync_playwright() as p:
        br = launch(p)
        pg = br.new_page(viewport={"width": 1500, "height": 950})

        console = []
        pg.on("console", lambda m: console.append((m.type, m.text)))
        failed = []
        pg.on("requestfailed", lambda r: failed.append(r.url))
        pg.on("pageerror", lambda e: console.append(("pageerror", str(e))))

        pg.goto(BASE, wait_until="domcontentloaded")
        # Vue 挂载 + 首屏 /api/state 回来需要一点时间
        pg.wait_for_selector(".fs-menu .el-menu-item", timeout=20000)
        pg.wait_for_timeout(2500)

        # ---- 1 依赖与挂载 ----
        print("\n[1] 依赖加载与挂载")
        check(not [u for u in failed if "/vendor/" in u],
              "vendor 依赖全部加载成功（无 404）")
        mounted = pg.evaluate(
            "() => document.querySelector('#app').children.length > 0")
        check(mounted, "Vue 已挂载（#app 有内容）")
        cloaked = pg.evaluate(
            "() => !!document.querySelector('#app[v-cloak]')")
        check(not cloaked, "v-cloak 已移除（模板编译成功）")
        has_vue = pg.evaluate("() => !!window.__FS && !!window.__FS.S")
        check(has_vue, "全局状态 __FS.S 可用")

        # ---- 2 深色主题 ----
        print("\n[2] 深色主题")
        bg = pg.evaluate(
            "() => getComputedStyle(document.body).backgroundColor")
        dark_html = pg.evaluate(
            "() => document.documentElement.classList.contains('dark')")
        check(dark_html, "html 带 .dark（Element Plus dark 变量生效）")
        # rgb 三个分量都低于 60 = 深色底
        rgb = [int(x) for x in bg.replace("rgb(", "").replace(")", "").split(",")[:3]]
        check(sum(rgb) / 3 < 60, f"页面底色是深色（{bg}）")
        fg = pg.evaluate("() => getComputedStyle(document.body).color")
        frgb = [int(x) for x in fg.replace("rgb(", "").replace(")", "").split(",")[:3]]
        check(sum(frgb) / 3 > 150, f"正文是浅色字（{fg}）")

        # ---- 3 侧栏与切换 ----
        print("\n[3] 七个页签")
        items = pg.query_selector_all(".fs-menu .el-menu-item")
        check(len(items) == 7, f"侧栏 7 个菜单项（实际 {len(items)}）")

        TABS = {
            "libs":   ".fs-libcards, .fs-libcard",
            "search": "#q",
            "assets": "#videos",
            "gen":    "#genPrompt",
            "hd":     ".fs-steps",
            "box":    "#bxPick",
            "clips":  "#clips",
        }
        for tab, sel in TABS.items():
            pg.click(f'.fs-menu .el-menu-item[data-tab="{tab}"]')
            pg.wait_for_timeout(900)
            ok = pg.is_visible(sel)
            check(ok, f"切到「{tab}」后渲染出 {sel}")

        # ---- 4 素材库 ----
        print("\n[4] 素材库")
        pg.click('.fs-menu .el-menu-item[data-tab="libs"]')
        pg.wait_for_timeout(1000)
        n_lib = len(pg.query_selector_all(".fs-libcard"))
        check(n_lib > 0, f"库卡片渲染出来（{n_lib} 个）")
        check(pg.is_visible("#btnAddLib"), "登记按钮在")
        check(pg.is_visible("#libPath"), "路径输入框在")
        stats = pg.text_content(".fs-stats") or ""
        check("个库" in stats or "还没有" in stats, f"侧栏统计有内容（{stats.strip()[:30]}）")

        # ---- 5 检索 ----
        print("\n[5] 台词检索")
        pg.click('.fs-menu .el-menu-item[data-tab="search"]')
        pg.wait_for_timeout(700)
        pg.fill("#q", "的")
        pg.click("#btnSearch")
        pg.wait_for_timeout(2500)
        n_row = len(pg.query_selector_all(".fs-rrow"))
        check(n_row > 0, f"搜「的」有结果（{n_row} 行）")
        if n_row:
            txt = pg.text_content(".fs-rrow .txt") or ""
            check("的" in txt, "结果文本包含关键词")
            check(len(pg.query_selector_all(".fs-rrow mark")) > 0, "关键词高亮了")
            check(pg.is_visible(".fs-rrow .ops .el-button"), "结果行有操作按钮")

        # ---- 6 素材与筛选 ----
        print("\n[6] 素材页与多维筛选")
        pg.click('.fs-menu .el-menu-item[data-tab="assets"]')
        pg.wait_for_timeout(2500)
        n_v = len(pg.query_selector_all(".fs-vcard"))
        check(n_v > 0, f"素材卡片渲染（{n_v} 张）")
        pg.click("#vMoreBtn")
        pg.wait_for_timeout(500)
        check(pg.is_visible("#vFilterCard"), "筛选面板能展开")
        check(len(pg.query_selector_all("#fLibs .el-checkbox")) > 0, "库筛选项渲染")
        check(len(pg.query_selector_all("#fRes .el-checkbox")) == 4, "分辨率 4 个选项")
        check(len(pg.query_selector_all("#fDur .el-checkbox")) == 4, "时长 4 个选项")
        check(pg.is_visible("#fCodec"), "编码下拉在")
        # 勾一个分辨率，看命中数变化
        before = pg.text_content("#fSummary") or ""
        pg.click('#fRes .el-checkbox:has-text("1080p")')
        pg.wait_for_timeout(1800)
        after = pg.text_content("#fSummary") or ""
        check(before != after, f"勾分辨率后汇总变化（{before[:22]} → {after[:22]}）")
        pg.click("#vResetBtn")
        pg.wait_for_timeout(1800)
        n_after = len(pg.query_selector_all(".fs-vcard"))
        check(n_after == n_v, f"重置回到全量（{n_after} == {n_v}）")

        # 三点菜单。注意：每张卡片都有一个隐藏的 popper，
        # pg.is_visible 只会看第一个匹配（永远是隐藏的那个），
        # 必须用 :visible 伪类挑出真正弹出的那个。
        pg.click(".fs-vmore")
        pg.wait_for_timeout(800)
        n_menu = pg.locator(".el-dropdown-menu:visible").count()
        check(n_menu > 0, "卡片三点菜单能打开")
        n_item = pg.locator(".el-dropdown-menu:visible .el-dropdown-menu__item").count()
        check(n_item >= 4, f"菜单项齐全（{n_item} 项）")
        pg.keyboard.press("Escape")
        pg.mouse.click(700, 60)

        # ---- 7 方框复查 ----
        print("\n[7] 方框复查")
        pg.click('.fs-menu .el-menu-item[data-tab="box"]')
        # 一部片两千条台词要逐条 verify，实测 3~5 秒才出得来，
        # 固定等待会假失败——等汇总条出现再断言。
        pg.wait_for_selector("#bxSum", timeout=30000)
        pg.wait_for_timeout(1500)
        check(pg.is_visible("#bxLibPick"), "库下拉在")
        check(pg.is_visible("#bxSearch"), "片名搜索在")
        check(pg.is_visible("#bxPick"), "片单下拉在")
        hit = pg.text_content("#bxHit") or ""
        check("命中" in hit, f"命中计数有内容（{hit.strip()}）")
        n_row = len(pg.query_selector_all(".fs-bxrow"))
        check(n_row > 0, f"台词清单渲染（{n_row} 条）")
        check(pg.is_visible("#bxSum"), "汇总条在")
        check(pg.is_visible("#bxView .fs-bximg img"), "台词帧图在")
        check(pg.is_visible("#bxProbe"), "重新 OCR 识别按钮在")
        check(pg.is_visible("#bxEdit"), "编辑台词按钮在")
        # 过滤台词
        if n_row:
            pg.fill("#bxFilter", "的")
            pg.wait_for_timeout(900)
            n_f = len(pg.query_selector_all(".fs-bxrow"))
            check(n_f <= n_row, f"过滤后条数不增（{n_f} <= {n_row}）")
            pg.fill("#bxFilter", "")
            pg.wait_for_timeout(900)

        # ---- 8 AI 生成 / 手绘 ----
        print("\n[8] AI 生成与手绘")
        pg.click('.fs-menu .el-menu-item[data-tab="gen"]')
        pg.wait_for_timeout(1500)
        check(pg.is_visible("#genPrompt"), "提示词输入框在")
        check(pg.is_visible("#genDur"), "时长下拉在")
        check(len(pg.query_selector_all(".fs-composer-row .el-button")) >= 4,
              "示例提示词按钮在")
        pg.click('.fs-menu .el-menu-item[data-tab="hd"]')
        pg.wait_for_timeout(1500)
        check(pg.is_visible(".fs-steps"), "六步进度条在")
        n_step = len(pg.query_selector_all(".fs-steps .el-step"))
        check(n_step == 6, f"正好 6 步（实际 {n_step}）")
        check(pg.is_visible("#hdInit"), "建工程按钮在")
        check(pg.is_visible("#hdAnn"), "标注输入框在")

        # ---- 9 控制台无错 ----
        print("\n[9] 控制台")
        bad = [c for c in console
               if c[0] in ("error", "pageerror")
               and "favicon" not in c[1].lower()]
        for t, m in bad[:10]:
            print(f"       [{t}] {m[:200]}")
        check(not bad, f"控制台没有报错（{len(bad)} 条）")

        pg.screenshot(path=os.path.join(OUT, "vue_ui.png"), full_page=False)
        pg.click('.fs-menu .el-menu-item[data-tab="libs"]')
        pg.wait_for_timeout(1200)
        pg.screenshot(path=os.path.join(OUT, "vue_libs.png"))
        pg.click('.fs-menu .el-menu-item[data-tab="box"]')
        pg.wait_for_timeout(2500)
        pg.screenshot(path=os.path.join(OUT, "vue_box.png"))
        br.close()

    print(f"\n==== {OK} 项通过 / {BAD} 项失败 ====")
    if BAD:
        for e in ERRS:
            print("  - " + e)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
