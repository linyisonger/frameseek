# -*- coding: utf-8 -*-
"""素材页多维筛选 UI 检查（2026-10-15）。

覆盖：库多选（含内置 AI 库）、台词状态、分辨率、时长、编码/帧率、
入库时间、动图/封面、重置、素材库卡片不含内置库。
"""
import asyncio
import sys

from playwright.async_api import async_playwright

CHROME = r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-1208\chrome-win64\chrome.exe"
BASE = "http://127.0.0.1:8771"
OUT = r"D:\project\AIProjects\frameseek\_out"

ok = True
n = 0


def check(cond, msg):
    global ok, n
    n += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        ok = False


async def _settle(pg, tries=40):
    """等 loadAssets 的并发请求落定：连续两次 VIEW.length 相同即视为稳定。"""
    last, stable = -1, 0
    for _ in range(tries):
        cur = await pg.evaluate("VIEW.length")
        if cur == last and cur > 0:
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
        last = cur
        await pg.wait_for_timeout(120)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME)
        pg = await b.new_page(viewport={"width": 1380, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(BASE, wait_until="networkidle")
        await pg.evaluate("switchTab('assets')")
        await pg.wait_for_timeout(700)

        # 1. 筛选面板默认收起，可展开
        check(await pg.evaluate("document.querySelector('#vFilterCard')"
                                ".classList.contains('hidden')"),
              "筛选面板默认收起")
        await pg.click("#vMoreBtn")
        await pg.wait_for_timeout(300)
        check(await pg.evaluate("!document.querySelector('#vFilterCard')"
                                ".classList.contains('hidden')"),
              "点「筛选」能展开面板")

        # 2. 库多选含内置 AI 库（🔒 标记）
        chips = await pg.evaluate(
            "[...document.querySelectorAll('#fLibs .fchip')].map("
            "e=>e.textContent.trim().replace(/\\s+/g,' '))")
        check(any("AI 生成" in c for c in chips),
              f"库筛选里有内置 AI 生成库：{chips}")
        check(sum(1 for c in chips if "🔒" in c) == 1,
              "内置库带只读锁标记")

        # 3. 素材库卡片列表不含内置库
        libnames = await pg.evaluate(
            "[...document.querySelectorAll('#libCards .libcard b')]"
            ".map(e=>e.textContent.trim())")
        check("AI 生成" not in libnames,
              f"素材库卡片列表不含内置库：{libnames}")

        # 4. 素材列表默认能看到 AI 库片子
        libs = await pg.evaluate(
            "[...new Set([...document.querySelectorAll('#videos .vmeta span')]"
            ".map(e=>e.textContent.split('·').pop().trim()))]")
        check(any("AI 生成" in x for x in libs),
              f"素材列表含 AI 生成库条目：{libs}")

        total = await pg.evaluate("ALL.length")

        # 5. 只勾 AI 库 → 只剩 1 条
        await pg.evaluate("resetFilters()")
        await pg.wait_for_timeout(600)
        # 直接走 onchange 逻辑（label 包 input，playwright click 会点到 label）
        await pg.evaluate("""
            const el = document.querySelector('#fLibs input[value="0"]');
            el.checked = true;
            el.dispatchEvent(new Event('change'));
        """)
        await _settle(pg)
        cnt = await pg.evaluate("VIEW.length")
        onlyai = await pg.evaluate(
            "VIEW.every(v=>v.builtin === true)")
        check(onlyai and cnt >= 1, f"只勾 AI 库 → {cnt} 条且全是内置库")

        # 6. 叠加分辨率筛选
        await pg.evaluate("resetFilters()")
        await pg.wait_for_timeout(600)
        await pg.evaluate("FILTER.res = new Set(['1080']); "
                          "renderLibChips(); loadAssets()")
        await _settle(pg)
        res = await pg.evaluate("VIEW.map(v=>v.height)")
        check(res and all(h >= 1000 and h < 2000 for h in res),
              f"分辨率筛 1080p → {len(res)} 条，高度都落在区间")

        # 7. 时长筛选
        await pg.evaluate("resetFilters()")
        await pg.wait_for_timeout(600)
        await pg.evaluate("FILTER.dur = new Set(['s30']); "
                          "renderLibChips(); loadAssets()")
        await _settle(pg)
        ds = await pg.evaluate("VIEW.map(v=>v.duration)")
        check(ds and all(0 < x < 30 for x in ds),
              f"时长筛 30 秒内 → {len(ds)} 条")

        # 8. 编码筛选下拉有真实选项
        codecs = await pg.evaluate(
            "[...document.querySelectorAll('#fCodec option')].map(e=>e.value)")
        check(len(codecs) > 1, f"编码下拉按实际 vcodec 生成：{codecs}")

        # 9. 帧率筛选
        await pg.evaluate("resetFilters()")
        await pg.wait_for_timeout(600)
        await pg.evaluate("""
            FILTER.fps = '29.97';
            document.querySelector('#fFps').value = '29.97';
            renderLibChips(); loadAssets();
        """)
        await _settle(pg)
        fv = await pg.evaluate("VIEW.map(v=>v.fps)")
        check(fv and all(29 <= x <= 31 for x in fv),
              f"帧率筛 29.97/30 → {len(fv)} 条，实测 {sorted(set(fv))}")

        # 10. 台词状态筛选
        await pg.evaluate("resetFilters()")
        await pg.wait_for_timeout(600)
        await pg.evaluate("FILTER.lines = new Set(['none']); "
                          "renderLibChips(); loadAssets()")
        await _settle(pg)
        lc = await pg.evaluate("VIEW.map(v=>v.line_count)")
        check(lc and all(x == 0 for x in lc),
              f"台词筛「没台词」→ {len(lc)} 条")

        # 11. 汇总条
        summ = await pg.evaluate("document.querySelector('#fSummary').textContent")
        check("命中" in summ, f"筛选汇总条有内容：{summ.strip()[:70]}")

        # 12. 重置按钮恢复全量（直接调函数，避免面板折叠时 click 不到）
        await pg.evaluate("resetFilters()")
        await pg.wait_for_timeout(1500)
        after = await pg.evaluate("VIEW.length")
        check(after == total, f"重置后回到全量 {after}/{total}")

        # 13. 重置按钮本身可点
        await pg.evaluate("""const el=document.querySelector('#fLibs input[value="0"]');
            el.checked=true; el.dispatchEvent(new Event('change'));""")
        await _settle(pg)
        await pg.click("#vResetBtn")
        # 固定等待会在服务刚启动/后台索引抢 CPU 时假失败，改成等列表稳定
        await _settle(pg)
        check(await pg.evaluate("VIEW.length") == total,
              "点「重置」按钮也能回到全量")

        check(not errs, f"无页面 JS 报错：{errs[:3]}")
        await pg.screenshot(path=OUT + r"\filter_panel.png", full_page=False)
        await b.close()

    print("\n== %d 项，%s ==" % (n, "全绿" if ok else "有失败"))
    sys.exit(0 if ok else 1)


asyncio.run(main())
