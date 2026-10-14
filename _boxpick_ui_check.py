# -*- coding: utf-8 -*-
"""方框复查页「选片」两级筛选：素材库下拉 + 片名搜索（2026-10-15）。

覆盖：库下拉选项与计数、按库过滤、片名搜索（含多词 AND）、无结果清空、
清空后恢复、选中片在结果内时保持选中、切库会换片、无 JS 报错。

Playwright 硬规矩：实时 SSE 让 networkidle 永不触发 → 用 domcontentloaded；
测试脚本串行跑（并发抢 CPU 会让慢路径假失败）。
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


async def _opts(pg):
    """片单 option 的 value 与文本。"""
    return await pg.evaluate(
        "[...document.querySelectorAll('#bxPick option')].map("
        "o=>({v:o.value, t:o.textContent}))")


async def _wait_raw(pg, tries=40):
    for _ in range(tries):
        got = await pg.evaluate("BX.raw.length")
        if got:
            return got
        await pg.wait_for_timeout(150)
    return 0


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME)
        pg = await b.new_page(viewport={"width": 1380, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(BASE, wait_until="domcontentloaded")
        await pg.evaluate("switchTab('box')")
        total = await _wait_raw(pg)
        check(total > 0, f"进入复查页拉到片单：{total} 部")

        # 1. 库下拉：全部库 + 每个有台词的库，带计数
        libopts = await pg.evaluate(
            "[...document.querySelectorAll('#bxLibPick option')].map("
            "o=>({v:o.value, t:o.textContent}))")
        libs = {}
        for r in await pg.evaluate(
                "BX.raw.map(x=>({root:x.root, lib:x.lib}))"):
            libs[r["root"]] = libs.get(r["root"], 0) + 1
        check(len(libopts) == len(libs) + 1,
              f"库下拉 = 全部库 + {len(libs)} 个库：{[o['t'] for o in libopts]}")
        check(libopts and f"（{total}）" in libopts[0]["t"],
              f"「全部库」带总数：{libopts[0]['t'] if libopts else '-'}")
        check(all(str(c) in o["t"] for o, c in
                  zip(libopts[1:], libs.values())),
              "每个库选项带自己的片数")

        # 2. 命中计数初始态
        hit0 = await pg.evaluate("document.querySelector('#bxHit').textContent")
        check(f"命中 {total} / {total}" in hit0, f"命中计数初始：{hit0}")

        # 3. 选一个库 → 片单只剩这个库的片子，且带库名的后缀去掉
        root2 = sorted(libs, key=lambda r: libs[r])[0]     # 片子最少的那个库
        await pg.evaluate("""r => {
            const el = document.querySelector('#bxLibPick');
            el.value = r;
            el.dispatchEvent(new Event('change'));
        }""", root2)
        await pg.wait_for_timeout(500)
        opts = await _opts(pg)
        check(len(opts) == libs[root2] and
              all(o["v"].split("|")[0] == root2 for o in opts if o["v"]),
              f"按库筛选 → {len(opts)} 部，全是该库（应 {libs[root2]}）")
        hit1 = await pg.evaluate("document.querySelector('#bxHit').textContent")
        check(f"命中 {libs[root2]} / {total}" in hit1, f"按库后命中计数：{hit1}")
        check(await pg.evaluate("BX.root") == root2,
              "切库后当前片也跟着换到该库")

        # 4. 回到全部库
        await pg.evaluate("""() => {
            const el = document.querySelector('#bxLibPick');
            el.value = '';
            el.dispatchEvent(new Event('change'));
        }""")
        await pg.wait_for_timeout(400)
        check(len(await _opts(pg)) == total, "切回「全部库」恢复全量")
        check(all("·" in o["t"] for o in await _opts(pg)),
              "全库模式下片名后面标出库名")

        # 5. 片名搜索（用一个真实片名的一部分）
        name = await pg.evaluate("BX.raw[0].name")
        key = name[:4]
        await pg.fill("#bxSearch", key)
        await pg.wait_for_timeout(400)
        opts = await _opts(pg)
        hits = await pg.evaluate("bxVideoList().length")
        check(hits == len(opts) and hits >= 1 and hits < total,
              f"搜索「{key}」→ 命中 {hits} / {total}")
        check(all(key.lower() in o["t"].lower() for o in opts),
              "搜索结果的片名都含关键字")

        # 6. 多词 = 全部命中（AND）
        two = key[:2] + " " + key[2:4]
        await pg.fill("#bxSearch", two)
        await pg.wait_for_timeout(400)
        hits2 = await pg.evaluate("bxVideoList().length")
        check(hits2 >= 1 and hits2 <= hits,
              f"多词「{two}」按 AND 命中 {hits2}")

        # 7. 搜不到 → 占位文案 + 右侧清空
        await pg.fill("#bxSearch", "zzz不存在的片子zzz")
        await pg.wait_for_timeout(400)
        opts = await _opts(pg)
        check(len(opts) == 1 and "没有匹配的片子" in opts[0]["t"],
              f"无结果时给出占位：{opts[0]['t'] if opts else '-'}")
        check(await pg.evaluate("BX.list.length") == 0
              and "没有匹配的片子" in await pg.evaluate(
                  "document.querySelector('#bxView').textContent"),
              "无结果时右侧清单与预览一起清空")
        hit2 = await pg.evaluate("document.querySelector('#bxHit').textContent")
        check(hit2.startswith("命中 0"), f"无结果时命中计数：{hit2}")

        # 8. 清空搜索 → 恢复全量
        await pg.fill("#bxSearch", "")
        await pg.wait_for_timeout(400)
        check(len(await _opts(pg)) == total, "清空搜索恢复全量")

        # 9. 当前选中的片子还在结果里 → 保持选中，不跳回第一条
        await pg.evaluate("""() => {
            const sel = document.querySelector('#bxPick');
            sel.value = sel.options[3].value;
            sel.dispatchEvent(new Event('change'));
        }""")
        await pg.wait_for_timeout(600)
        keep = await pg.evaluate("document.querySelector('#bxPick').value")
        kname = await pg.evaluate(
            "BX.raw.find(x => x.root+'|'+x.id === document."
            "querySelector('#bxPick').value).name")
        await pg.fill("#bxSearch", kname[:5])
        await pg.wait_for_timeout(400)
        check(await pg.evaluate("document.querySelector('#bxPick').value") == keep,
              f"搜索后当前片仍在结果里 → 保持选中（{kname[:16]}）")

        check(not errs, f"无页面 JS 报错：{errs[:3]}")
        await pg.screenshot(path=OUT + r"\box_picker.png", full_page=False)
        await b.close()

    print("\n== %d 项，%s ==" % (n, "全绿" if ok else "有失败"))
    sys.exit(0 if ok else 1)


asyncio.run(main())
