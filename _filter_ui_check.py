# -*- coding: utf-8 -*-
"""素材页筛选 UI 检查（Vue 3 + Element Plus 版）。

2026-10-15 精简：筛选只剩「素材库」和「时长」两档（用户嫌选项太多），
本脚本同步收敛——库多选（含内置 AI 库）、时长筛选、重置、汇总条、
素材库卡片不含内置库。

口径变化：原生 JS 版直接改 FILTER 全局再触发 loadAssets；
Vue 版状态住在组件里，测试一律走真实 UI（点 checkbox / 下拉），
数据断言读 window.__FS.S（给回归脚本留的口子）。
"""
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pw import launch

BASE = os.environ.get("FS_BASE", "http://127.0.0.1:8771")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)

ok = True
n = 0


def check(cond, msg):
    global ok, n
    n += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        ok = False


def goto_tab(pg, tab):
    pg.click(f'.fs-menu .el-menu-item[data-tab="{tab}"]')
    pg.wait_for_timeout(600)


def settle(pg, tries=50):
    """等筛选后的列表落定：连续几次 viewCount 相同且 > 0 即视为稳定。"""
    last, stable = -1, 0
    for _ in range(tries):
        cur = pg.evaluate("window.__FS && __FS.S ? __FS.S.viewCount : -1")
        if cur == last and cur > 0:
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
        last = cur
        pg.wait_for_timeout(120)


def main():
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1380, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector(".fs-menu .el-menu-item", timeout=20000)
        pg.wait_for_timeout(2000)
        goto_tab(pg, "assets")
        pg.wait_for_selector(".fs-vcard", timeout=60000)
        settle(pg)

        # 1. 筛选面板默认收起，可展开
        check(pg.evaluate(
            "getComputedStyle(document.querySelector('#vFilterCard')).display"
            " === 'none'"), "筛选面板默认收起")
        pg.click("#vMoreBtn")
        pg.wait_for_timeout(400)
        check(pg.is_visible("#vFilterCard"), "点「筛选」能展开面板")

        # 2. 只剩两组筛选项：素材库 + 时长
        rows = pg.eval_on_selector_all(
            "#vFilterCard .fs-frow .fs-label",
            "els => els.map(e => e.textContent.trim())")
        check(rows == ["素材库", "时长"],
              f"筛选只剩素材库/时长两组：{rows}")
        check(len(pg.query_selector_all("#fLibs .el-checkbox")) >= 2,
              "素材库筛选项在")
        check(len(pg.query_selector_all("#fDur .el-checkbox")) == 4,
              "时长 4 个分档在")
        for gone in ("#fLines", "#fRes", "#fCodec", "#fFps", "#fAge",
                     "#fHasGif", "#fHasCover", "#fNoGif"):
            check(pg.query_selector(gone) is None, f"{gone} 已移除")

        # 3. 库多选含内置 AI 库（🔒 标记）
        chips = pg.eval_on_selector_all(
            "#fLibs .el-checkbox", "els => els.map(e => e.textContent.trim())")
        check(any("AI 生成" in c for c in chips),
              f"库筛选里有内置 AI 生成库：{chips}")
        check(sum(1 for c in chips if "🔒" in c) == 1, "内置库带只读锁标记")

        # 4. 素材库卡片列表不含内置库
        libnames = pg.eval_on_selector_all(
            "#libCards .fs-libcard b", "els => els.map(e => e.textContent.trim())")
        check("AI 生成" not in libnames,
              f"素材库卡片列表不含内置库：{libnames}")

        # 5. 素材列表默认能看到 AI 库片子
        metas = pg.eval_on_selector_all(
            "#videos .fs-vmeta span", "els => els.map(e => e.textContent)")
        check(any("AI 生成" in x for x in metas), "素材列表含 AI 生成库条目")

        total = pg.evaluate("__FS.S.all.length")

        # 6. 只勾 AI 库 → 剩下的全是内置库
        pg.click("#vResetBtn")
        settle(pg)
        pg.click('#fLibs .el-checkbox:has-text("AI 生成")')
        settle(pg)
        cnt = pg.evaluate("__FS.S.viewCount")
        metas = pg.eval_on_selector_all(
            "#videos .fs-vmeta span", "els => els.map(e => e.textContent)")
        all_ai = metas and all("AI 生成" in x for x in metas)
        check(all_ai and cnt >= 1, f"只勾 AI 库 → {cnt} 条且全是内置库")

        # 7. 时长筛选 + 数据交叉验证
        pg.click("#vResetBtn")
        settle(pg)
        pg.click('#fDur .el-checkbox:has-text("30 秒内")')
        settle(pg)
        ds = pg.evaluate(
            "__FS.S.all.filter(v => v.duration > 0 && v.duration < 30).length")
        vc = pg.evaluate("__FS.S.viewCount")
        check(abs(vc - ds) <= max(3, ds * 0.05) and vc > 0,
              f"时长筛 30 秒内 → 界面 {vc} 条 / 交叉验证 {ds} 条")

        # 8. 汇总条
        summ = pg.text_content("#fSummary") or ""
        check("命中" in summ and "时长" in summ,
              f"筛选汇总条有内容：{summ.strip()[:70]}")

        # 9. 重置按钮恢复全量
        pg.click("#vResetBtn")
        settle(pg)
        after = pg.evaluate("__FS.S.viewCount")
        check(after == total, f"重置后回到全量 {after}/{total}")

        # 10. 重置按钮本身可点（勾一个筛选再点重置）
        pg.click('#fDur .el-checkbox:has-text("30 秒内")')
        settle(pg)
        pg.click("#vResetBtn")
        settle(pg)
        check(pg.evaluate("__FS.S.viewCount") == total, "点「重置」按钮也能回到全量")

        # 11. 顶部关键字框仍在（它不算"筛选"那一档，是搜索）
        check(pg.is_visible("#vfilter"), "顶部关键字过滤框仍在")

        check(not errs, f"无页面 JS 报错：{errs[:3]}")
        pg.screenshot(path=os.path.join(OUT, "filter_panel.png"), full_page=False)
        b.close()

    print("\n== %d 项，%s ==" % (n, "全绿" if ok else "有失败"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
