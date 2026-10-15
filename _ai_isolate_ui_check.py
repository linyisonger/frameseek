# -*- coding: utf-8 -*-
"""AI 生成产物「不做字幕/语音识别」隔离检查（Vue 3 + Element Plus 版）。

覆盖：
1. AI 库片子在素材页显示「AI 生成」标记，不出现「没台词 / 台词 N」；
2. AI 片三点菜单里没有台词类入口（重新导入 SRT / 重新识别 / 语音识别）；
3. 普通素材库片子的菜单照旧有这些入口；
4. 台词筛选对 AI 片不生效（它压根没有台词这回事）；
5. 后端兜底：对 AI 库调台词接口返回 400。

Playwright 硬规矩（踩过）：实时 SSE 让 networkidle 永不触发 → 用
domcontentloaded；Element Plus 的下拉菜单每张卡渲染一份隐藏 popper，
断言要用 :visible 挑出弹出的那份。
"""
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pw import launch

BASE = os.environ.get("FS_BASE", "http://127.0.0.1:8771")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)
AI_ROOT = r"D:\project\AIProjects\frameseek\生成"

ok = True
n = 0


def check(cond, msg):
    global ok, n
    n += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        ok = False


def settle(pg, tries=50):
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


def menu_texts(pg):
    """当前弹出的那个下拉菜单的菜单项文本。"""
    pg.wait_for_timeout(500)
    return pg.eval_on_selector_all(
        ".el-dropdown-menu:visible .el-dropdown-menu__item",
        "els => els.map(e => e.textContent.trim())")


def main():
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1380, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector(".fs-menu .el-menu-item", timeout=20000)
        pg.wait_for_timeout(2000)
        pg.click('.fs-menu .el-menu-item[data-tab="assets"]')
        pg.wait_for_selector(".fs-vcard", timeout=60000)
        settle(pg)

        # 展开筛选面板，只勾内置 AI 库
        pg.click("#vMoreBtn")
        pg.wait_for_timeout(400)
        pg.click("#vResetBtn")
        settle(pg)
        pg.click('#fLibs .el-checkbox:has-text("AI 生成")')
        settle(pg)
        cnt = pg.evaluate("__FS.S.viewCount")

        # 1. 卡片标记：AI 生成，不是「没台词」
        tags = pg.eval_on_selector_all(
            "#videos .row2", "els => els.map(e => e.textContent.trim())")
        check(tags and all("AI 生成" in t for t in tags),
              f"AI 片卡片显示「AI 生成」标记，不显示台词数：{tags}")
        check(not any("没台词" in t for t in tags),
              "AI 片不出现「没台词」红色标签")

        # 2. AI 片菜单里没有台词类入口
        pg.click(".fs-vmore")
        flat = menu_texts(pg)
        check(not any("台词" in x for x in flat),
              f"AI 片菜单无台词类入口：{sorted(set(flat))}")
        check(any("预览" in x for x in flat)
              and any("打开所在文件夹" in x for x in flat)
              and any("删除素材及文件" in x for x in flat),
              "AI 片仍保留 预览 / 打开文件夹 / 删除")
        pg.keyboard.press("Escape")
        pg.mouse.click(700, 60)
        pg.wait_for_timeout(300)

        # 三点按钮提示文案要说明"AI 生成产物不做字幕识别"
        tip = pg.eval_on_selector(
            "#videos .fs-vmore", "e => e.getAttribute('title') || ''")
        check("不做字幕识别" in tip, f"三点按钮提示区分 AI 片：{tip}")

        # 3. 普通素材库片子的菜单照旧
        pg.click("#vResetBtn")
        settle(pg)
        pg.click('#fLibs .el-checkbox:has-text("电影")')
        settle(pg)
        pg.click(".fs-vmore")
        flat2 = menu_texts(pg)
        check(any("重新识别台词" in x for x in flat2)
              and any("语音识别台词" in x for x in flat2)
              and any("重新导入同名 SRT" in x for x in flat2),
              "普通库片子的台词入口照旧保留")
        pg.keyboard.press("Escape")
        pg.mouse.click(700, 60)
        pg.wait_for_timeout(300)

        # 4. 台词筛选对 AI 片不生效
        pg.click("#vResetBtn")
        settle(pg)
        pg.click('#fLibs .el-checkbox:has-text("AI 生成")')
        settle(pg)
        pg.click('#fLines .el-checkbox:has-text("有台词")')
        settle(pg)
        c2 = pg.evaluate("__FS.S.viewCount")
        check(c2 == cnt, f"台词筛「有台词」不影响 AI 片（仍 {c2} 条）")

        # 5. 后端兜底
        for path in ("transcribe_asr", "retranscribe", "reimport"):
            r = pg.evaluate("""async (args) => {
                const [api_, root] = args;
                const r = await fetch('/api/video/' + api_, {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({root: root, video_id: 1})});
                return {status: r.status, body: await r.text()};
            }""", [path, AI_ROOT])
            check(r["status"] == 400 and "AI 生成产物" in r["body"],
                  f"/api/video/{path} 对 AI 库返回 400：{r['status']}")

        check(not errs, f"无页面 JS 报错：{errs[:3]}")
        pg.screenshot(path=os.path.join(OUT, "ai_isolate.png"), full_page=False)
        b.close()

    print("\n== %d 项，%s ==" % (n, "全绿" if ok else "有失败"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
