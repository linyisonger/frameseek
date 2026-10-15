# -*- coding: utf-8 -*-
"""素材页多维筛选 UI 检查（Vue 3 + Element Plus 版）。

覆盖：库多选（含内置 AI 库）、台词状态、分辨率、时长、编码/帧率、
入库时间、动图/封面、重置、素材库卡片不含内置库。

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

        # 2. 库多选含内置 AI 库（🔒 标记）
        chips = pg.eval_on_selector_all(
            "#fLibs .el-checkbox", "els => els.map(e => e.textContent.trim())")
        check(any("AI 生成" in c for c in chips),
              f"库筛选里有内置 AI 生成库：{chips}")
        check(sum(1 for c in chips if "🔒" in c) == 1, "内置库带只读锁标记")

        # 3. 素材库卡片列表不含内置库
        libnames = pg.eval_on_selector_all(
            "#libCards .fs-libcard b", "els => els.map(e => e.textContent.trim())")
        check("AI 生成" not in libnames,
              f"素材库卡片列表不含内置库：{libnames}")

        # 4. 素材列表默认能看到 AI 库片子
        metas = pg.eval_on_selector_all(
            "#videos .fs-vmeta span", "els => els.map(e => e.textContent)")
        check(any("AI 生成" in x for x in metas), "素材列表含 AI 生成库条目")

        total = pg.evaluate("__FS.S.all.length")

        # 5. 只勾 AI 库 → 剩下的全是内置库
        pg.click("#vResetBtn")
        settle(pg)
        pg.click('#fLibs .el-checkbox:has-text("AI 生成")')
        settle(pg)
        cnt = pg.evaluate("__FS.S.viewCount")
        metas = pg.eval_on_selector_all(
            "#videos .fs-vmeta span", "els => els.map(e => e.textContent)")
        all_ai = metas and all("AI 生成" in x for x in metas)
        check(all_ai and cnt >= 1, f"只勾 AI 库 → {cnt} 条且全是内置库")

        # 6. 叠加分辨率筛选
        pg.click("#vResetBtn")
        settle(pg)
        pg.click('#fRes .el-checkbox:has-text("1080p")')
        settle(pg)
        hs = pg.evaluate(
            "__FS.S.all.filter(v => !__FS.S.viewCount || true).length"
            " && [...document.querySelectorAll('#videos .fs-vcard')].length")
        # 高度断言直接读卡片上的数据不可行，读 S.all 按当前 viewCount 抽验证：
        res = pg.evaluate("""(() => {
            // view 过滤逻辑在组件里，这里按同样的规则从 S.all 重算一遍做交叉验证
            return __FS.S.all.filter(v => v.height >= 1000 && v.height < 2000).length;
        })()""")
        vc = pg.evaluate("__FS.S.viewCount")
        check(abs(vc - res) <= max(3, res * 0.05) and vc > 0,
              f"分辨率筛 1080p → 界面 {vc} 条 / 数据交叉验证 {res} 条")

        # 7. 时长筛选
        pg.click("#vResetBtn")
        settle(pg)
        pg.click('#fDur .el-checkbox:has-text("30 秒内")')
        settle(pg)
        ds = pg.evaluate(
            "__FS.S.all.filter(v => v.duration > 0 && v.duration < 30).length")
        vc = pg.evaluate("__FS.S.viewCount")
        check(abs(vc - ds) <= max(3, ds * 0.05) and vc > 0,
              f"时长筛 30 秒内 → 界面 {vc} 条 / 交叉验证 {ds} 条")

        # 8. 编码筛选下拉有真实选项
        # el-select 的内部 input 会被 placeholder 层挡住，点它的 wrapper
        SEL_WRAP = "xpath=//input[@id='{0}']/ancestor::div[contains(@class,'el-select__wrapper')]"
        pg.click("#vResetBtn")
        settle(pg)
        pg.click(SEL_WRAP.format("fCodec"))
        pg.wait_for_timeout(500)
        codecs = pg.eval_on_selector_all(
            ".el-select-dropdown:visible .el-select-dropdown__item",
            "els => els.map(e => e.textContent.trim())")
        check(len(codecs) >= 2, f"编码下拉按实际 vcodec 生成：{codecs}")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)

        # 9. 帧率筛选
        pg.click(SEL_WRAP.format("fFps"))
        pg.wait_for_timeout(500)
        pg.click('.el-select-dropdown:visible .el-select-dropdown__item:has-text("29.97")')
        settle(pg)
        fv = pg.evaluate(
            "__FS.S.all.filter(v => { const f = +v.fps || 0;"
            " return f && Math.abs(f - 29.97) < 1.2 || Math.abs(f - 30) < 1.2; }).length")
        vc = pg.evaluate("__FS.S.viewCount")
        check(abs(vc - fv) <= max(3, fv * 0.05) and vc > 0,
              f"帧率筛 29.97/30 → 界面 {vc} 条 / 交叉验证 {fv} 条")
        pg.click("#vResetBtn")
        settle(pg)

        # 10. 台词状态筛选
        pg.click('#fLines .el-checkbox:has-text("没台词")')
        settle(pg)
        # 台词筛选对 AI 生成产物不生效（它们没有台词这回事），
        # 所以交叉验证要把 builtin 也算进「不受影响」的那一边
        lc = pg.evaluate(
            "__FS.S.all.filter(v => v.builtin"
            " || ((v.line_count || 0) === 0 && !v.err)).length")
        vc = pg.evaluate("__FS.S.viewCount")
        check(abs(vc - lc) <= max(3, lc * 0.05) and vc > 0,
              f"台词筛「没台词」→ 界面 {vc} 条 / 交叉验证 {lc} 条")

        # 11. 汇总条
        summ = pg.text_content("#fSummary") or ""
        check("命中" in summ, f"筛选汇总条有内容：{summ.strip()[:70]}")

        # 12. 重置按钮恢复全量
        pg.click("#vResetBtn")
        settle(pg)
        after = pg.evaluate("__FS.S.viewCount")
        check(after == total, f"重置后回到全量 {after}/{total}")

        # 13. 重置按钮本身可点（勾一个筛选再点重置）
        pg.click('#fRes .el-checkbox:has-text("1080p")')
        settle(pg)
        pg.click("#vResetBtn")
        settle(pg)
        check(pg.evaluate("__FS.S.viewCount") == total, "点「重置」按钮也能回到全量")

        check(not errs, f"无页面 JS 报错：{errs[:3]}")
        pg.screenshot(path=os.path.join(OUT, "filter_panel.png"), full_page=False)
        b.close()

    print("\n== %d 项，%s ==" % (n, "全绿" if ok else "有失败"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
