# -*- coding: utf-8 -*-
"""方框复查页「选片」两级筛选：素材库下拉 + 片名搜索（Vue 3 + Element Plus 版）。

覆盖：库下拉选项与计数、按库过滤、片名搜索（含多词 AND）、无结果清空、
清空后恢复、选中片在结果内时保持选中、切库会换片、无 JS 报错。

驱动方式：原生 select 换成了 el-select（点 wrapper 弹出 popper 再点选项），
片单断言读 __FS.box（app.js 里给回归脚本留的状态口子）。

Playwright 硬规矩：实时 SSE 让 networkidle 永不触发 → 用 domcontentloaded；
测试脚本串行跑（并发抢 CPU 会让慢路径假失败）。
"""
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pw import launch

BASE = os.environ.get("FS_BASE", "http://127.0.0.1:8771")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)

WRAP = "xpath=//input[@id='{0}']/ancestor::div[contains(@class,'el-select__wrapper')]"

ok = True
n = 0


def check(cond, msg):
    global ok, n
    n += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        ok = False


def popper_texts(pg):
    pg.wait_for_timeout(400)
    return pg.eval_on_selector_all(
        ".el-select-dropdown:visible .el-select-dropdown__item",
        "els => els.map(e => e.textContent.trim())")


def pick_option(pg, text):
    """点开片单下拉并选中包含指定文本的选项。"""
    pg.click(WRAP.format("bxPick"))
    pg.wait_for_timeout(400)
    pg.click(f'.el-select-dropdown:visible .el-select-dropdown__item:has-text("{text}")')
    pg.wait_for_timeout(300)


def main():
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1380, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="domcontentloaded")
        pg.wait_for_selector(".fs-menu .el-menu-item", timeout=20000)
        pg.wait_for_timeout(1500)
        pg.click('.fs-menu .el-menu-item[data-tab="box"]')
        pg.wait_for_selector("#bxSum", timeout=30000)
        pg.wait_for_timeout(800)

        total = pg.evaluate("__FS.box.raw.length")
        check(total > 0, f"进入复查页拉到片单：{total} 部")

        # 1. 库下拉：全部库 + 每个有台词的库，带计数
        pg.click(WRAP.format("bxLibPick"))
        libopts = popper_texts(pg)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        libs = pg.evaluate("""(() => {
            const m = {};
            __FS.box.raw.forEach(x => { m[x.root] = (m[x.root] || 0) + 1; });
            return m;
        })()""")
        check(len(libopts) == len(libs) + 1,
              f"库下拉 = 全部库 + {len(libs)} 个库：{libopts}")
        check(libopts and f"（{total}）" in libopts[0],
              f"「全部库」带总数：{libopts[0] if libopts else '-'}")
        check(all(str(c) in o for o, c in zip(libopts[1:], libs.values())),
              "每个库选项带自己的片数")

        # 2. 命中计数初始态
        hit0 = pg.text_content("#bxHit") or ""
        check(f"命中 {total} / {total}" in hit0, f"命中计数初始：{hit0}")

        # 3. 选一个库 → 片单只剩这个库的片子，且库名后缀消失
        info = pg.evaluate("""(() => {
            const m = {};
            __FS.box.raw.forEach(x => { m[x.root] = (m[x.root] || 0) + 1; });
            const r = Object.keys(m).sort((a, b) => m[a] - m[b])[0];
            return { root: r, cnt: m[r],
                     name: __FS.box.raw.find(x => x.root === r).lib };
        })()""")
        pg.click(WRAP.format("bxLibPick"))
        pg.wait_for_timeout(400)
        pg.click(f'.el-select-dropdown:visible .el-select-dropdown__item:has-text("{info["name"]}")')
        pg.wait_for_timeout(400)
        # 切库后清单要重新拉（一部片 verify 好几秒），轮询等它回来。
        # 注意 list 里是台词行，没有 root 字段——归属要用 rep.video.id 对 pick 验。
        loaded = False
        for _ in range(40):
            if pg.evaluate(
                "r => __FS.box.pick.startsWith(r) && __FS.box.list.length > 0"
                " && !!__FS.box.rep", info["root"]):
                loaded = True
                break
            pg.wait_for_timeout(500)
        same_video = pg.evaluate("""(() => {
            const [, id] = __FS.box.pick.split('|');
            return __FS.box.rep && String(__FS.box.rep.video.id) === id;
        })()""")
        check(loaded and same_video
              and pg.evaluate("__FS.box.filtered") == info["cnt"],
              f"按库筛选 → {pg.evaluate('__FS.box.filtered')} 部，全是该库（应 {info['cnt']}）")
        hit1 = pg.text_content("#bxHit") or ""
        check(f"命中 {info['cnt']} / {total}" in hit1, f"按库后命中计数：{hit1}")
        check(pg.evaluate("r => __FS.box.pick.startsWith(r)", info["root"]),
              "切库后当前片也跟着换到该库")

        # 4. 回到全部库
        pg.click(WRAP.format("bxLibPick"))
        pg.wait_for_timeout(400)
        pg.click('.el-select-dropdown:visible .el-select-dropdown__item:has-text("全部库")')
        pg.wait_for_timeout(600)
        check(pg.evaluate("__FS.box.filtered") == total, "切回「全部库」恢复全量")

        # 5. 片名搜索（用一个真实片名的一部分）
        name = pg.evaluate("__FS.box.raw[0].name")
        key = name[:4]
        pg.fill("#bxSearch", key)
        pg.wait_for_timeout(500)
        hits = pg.evaluate("__FS.box.filtered")
        check(1 <= hits < total, f"搜索「{key}」→ 命中 {hits} / {total}")

        # 6. 多词 = 全部命中（AND）
        two = key[:2] + " " + key[2:4]
        pg.fill("#bxSearch", two)
        pg.wait_for_timeout(500)
        hits2 = pg.evaluate("__FS.box.filtered")
        check(1 <= hits2 <= hits, f"多词「{two}」按 AND 命中 {hits2}")

        # 7. 搜不到 → 右侧清单与预览一起清空
        pg.fill("#bxSearch", "zzz不存在的片子zzz")
        pg.wait_for_timeout(600)
        check(pg.evaluate("__FS.box.list.length") == 0,
              "无结果时右侧清单清空")
        check("没有匹配" in (pg.text_content("#bxView") or "")
              or "没有匹配" in (pg.text_content(".fs-bxlist") or ""),
              "无结果时给出占位提示")
        hit2 = pg.text_content("#bxHit") or ""
        check(hit2.startswith("命中 0"), f"无结果时命中计数：{hit2}")

        # 8. 清空搜索 → 恢复全量
        pg.fill("#bxSearch", "")
        pg.wait_for_timeout(600)
        check(pg.evaluate("__FS.box.filtered") == total, "清空搜索恢复全量")

        # 9. 当前选中的片子还在结果里 → 保持选中，不跳回第一条
        pick4 = pg.evaluate("""(() => {
            const lst = __FS.box.raw;
            return lst[Math.min(3, lst.length - 1)].root + '|'
                 + lst[Math.min(3, lst.length - 1)].id;
        })()""")
        pg.evaluate(f"__FS.box.pick = {pick4!r}")
        pg.wait_for_timeout(2500)      # 等清单拉完（一部片 verify 好几秒）
        kname = pg.evaluate(
            "__FS.box.raw.find(x => x.root + '|' + x.id === __FS.box.pick).name")
        pg.fill("#bxSearch", kname[:5])
        pg.wait_for_timeout(600)
        check(pg.evaluate("__FS.box.pick") == pick4,
              f"搜索后当前片仍在结果里 → 保持选中（{kname[:16]}）")

        check(not errs, f"无页面 JS 报错：{errs[:3]}")
        pg.screenshot(path=os.path.join(OUT, "box_picker.png"), full_page=False)
        b.close()

    print("\n== %d 项，%s ==" % (n, "全绿" if ok else "有失败"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
