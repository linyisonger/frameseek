# -*- coding: utf-8 -*-
"""AI 生成产物「不做字幕/语音识别」隔离检查（2026-10-15）。

覆盖：
1. AI 库片子在素材页显示「AI 生成」标记，不出现「没台词 / 台词 N」；
2. AI 片三点菜单里没有台词类入口（重新导入 SRT / 重新识别 / 语音识别）；
3. 普通素材库片子的菜单照旧有这些入口；
4. 台词筛选对 AI 片不生效（它压根没有台词这回事）；
5. 后端兜底：对 AI 库调台词接口返回 400。

Playwright 两条硬规矩（踩过）：实时 SSE 让 networkidle 永不触发 → 用
domcontentloaded；<option> 不可见 → 用 state="attached"。
"""
import asyncio
import sys

from playwright.async_api import async_playwright

CHROME = r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-1208\chrome-win64\chrome.exe"
BASE = "http://127.0.0.1:8771"
OUT = r"D:\project\AIProjects\frameseek\_out"
AI_ROOT = r"D:\project\AIProjects\frameseek\生成"

ok = True
n = 0


def check(cond, msg):
    global ok, n
    n += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        ok = False


async def _settle(pg, tries=40):
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
        await pg.goto(BASE, wait_until="domcontentloaded")
        await pg.evaluate("switchTab('assets')")
        await pg.wait_for_timeout(900)

        # 只勾内置 AI 库
        await pg.evaluate("""
            resetFilters();
            const el = document.querySelector('#fLibs input[value="0"]');
            el.checked = true;
            el.dispatchEvent(new Event('change'));
        """)
        await _settle(pg)
        cnt = await pg.evaluate("VIEW.length")
        check(cnt >= 1 and await pg.evaluate("VIEW.every(v=>v.builtin===true)"),
              f"只勾 AI 库 → {cnt} 条，全是内置产物")

        # 1. 卡片标记：AI 生成，不是「没台词」
        tags = await pg.evaluate(
            "[...document.querySelectorAll('#videos .row2')].map("
            "e=>e.textContent.trim())")
        check(tags and all("AI 生成" in t for t in tags),
              f"AI 片卡片显示「AI 生成」标记，不显示台词数：{tags}")
        check(not any("没台词" in t for t in tags),
              "AI 片不出现「没台词」红色标签")

        # 2. AI 片菜单里没有台词类入口
        menu = await pg.evaluate(
            "[...document.querySelectorAll('#videos .vmenu')].map("
            "m=>[...m.querySelectorAll('a')].map(a=>a.textContent.trim()))")
        flat = [x for m in menu for x in m]
        check(not any("台词" in x for x in flat),
              f"AI 片菜单无台词类入口：{sorted(set(flat))}")
        check(any("预览" in x for x in flat) and any("打开所在文件夹" in x for x in flat)
              and any("删除素材及文件" in x for x in flat),
              "AI 片仍保留 预览 / 打开文件夹 / 删除")

        # 三点按钮提示文案要说明"AI 生成产物不做字幕识别"
        tip = await pg.evaluate(
            "document.querySelector('#videos .vmore').title")
        check("不做字幕识别" in tip, f"三点按钮提示区分 AI 片：{tip}")

        # 3. 普通素材库片子的菜单照旧
        await pg.evaluate("""
            resetFilters();
            const el = document.querySelector(
                '#fLibs input[value="%d"]');
            el.checked = true;
            el.dispatchEvent(new Event('change'));
        """ % 12)
        await _settle(pg)
        menu2 = await pg.evaluate(
            "[...document.querySelectorAll('#videos .vmenu')].map("
            "m=>[...m.querySelectorAll('a')].map(a=>a.textContent.trim()))")
        flat2 = [x for m in menu2 for x in m]
        check(any("重新识别台词" in x for x in flat2)
              and any("语音识别台词" in x for x in flat2)
              and any("重新导入同名 SRT" in x for x in flat2),
              "普通库片子的台词入口照旧保留")

        # 4. 台词筛选对 AI 片不生效
        await pg.evaluate("""
            resetFilters();
            const el = document.querySelector('#fLibs input[value="0"]');
            el.checked = true;
            el.dispatchEvent(new Event('change'));
            FILTER.lines = new Set(['has']);
            renderLibChips(); loadAssets();
        """)
        await _settle(pg)
        c2 = await pg.evaluate("VIEW.length")
        check(c2 == cnt, f"台词筛「有台词」不影响 AI 片（仍 {c2} 条）")

        # 5. 后端兜底
        for path in ("transcribe_asr", "retranscribe", "reimport"):
            r = await pg.evaluate("""async (args) => {
                const [api_, root] = args;
                const r = await fetch('/api/video/' + api_, {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({root: root, video_id: 1})});
                return {status: r.status, body: await r.text()};
            }""", [path, AI_ROOT])
            check(r["status"] == 400 and "AI 生成产物" in r["body"],
                  f"/api/video/{path} 对 AI 库返回 400：{r['status']}")

        check(not errs, f"无页面 JS 报错：{errs[:3]}")
        await pg.screenshot(path=OUT + r"\ai_isolate.png", full_page=False)
        await b.close()

    print("\n== %d 项，%s ==" % (n, "全绿" if ok else "有失败"))
    sys.exit(0 if ok else 1)


asyncio.run(main())
