# -*- coding: utf-8 -*-
"""对话窗口 UI 检查：渲染 / 交互 / 窄屏 / 错误路径。"""
import asyncio
import sys

from playwright.async_api import async_playwright

CHROME = r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-1208\chrome-win64\chrome.exe"
BASE = "http://127.0.0.1:8771"
OUT = r"D:\project\AIProjects\frameseek\_out"

ok = True


def check(cond, msg):
    global ok
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        ok = False


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME)
        pg = await b.new_page(viewport={"width": 1280, "height": 800})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(BASE, wait_until="networkidle")
        await pg.evaluate("switchTab('gen')")
        await pg.wait_for_timeout(600)

        # 1. 基础渲染
        check(await pg.locator("#chatEmpty").is_visible(), "空状态提示可见")
        check(await pg.locator("#genPrompt").is_visible(), "输入框可见")
        check(await pg.locator("#btnGen").is_visible(), "发送按钮可见")
        srv = await pg.locator("#genSrv").inner_text()
        check("引擎" in srv, f"引擎徽标：{srv.strip()[:40]}")
        check(await pg.locator("#genAspect option").count() >= 6,
              "宽高比下拉已填充")

        # 1b. 品牌 logo：favicon data URI + 侧边栏 SVG
        fav = await pg.evaluate(
            "() => document.querySelector('link[rel=icon]').href")
        check(fav.startswith("data:image/svg+xml,") and "fsg" in fav,
              f"favicon 已是新 logo data URI（{len(fav)} 字符）")
        mark_svg = pg.locator(".mark svg")
        check(await mark_svg.count() == 1, "侧边栏 .mark 内联 SVG 存在")
        check(await pg.locator(".mark svg rect").count() >= 2
              and await pg.locator(".mark svg path").count() >= 1,
              "侧边栏 logo 图形完整（渐变底+帧格+播放键）")

        # 2. 空提示词 → toast 拦截，不出气泡
        await pg.click("#btnGen")
        await pg.wait_for_timeout(300)
        check(await pg.locator(".bub.me").count() == 0, "空提示词被拦截")

        # 3. 正常发送 → 用户气泡 + 系统气泡（GPU 忙，走错误路径也要有气泡）
        await pg.fill("#genPrompt", "A cat sitting on a windowsill, morning light.")
        await pg.click("#btnGen")
        await pg.wait_for_timeout(2500)
        check(await pg.locator(".bub.me").count() == 1, "用户气泡出现")
        check(await pg.locator(".bub.sys").count() >= 1, "系统气泡出现")
        meta = await pg.locator(".bub.sys").last.inner_text()
        print("    系统气泡内容:", meta.strip().replace("\n", " / ")[:90])
        check(len(meta.strip()) > 4, "系统气泡有状态文字")

        # 4. Enter 发送礼仪
        await pg.fill("#genPrompt", "second prompt")
        await pg.press("#genPrompt", "Enter")
        await pg.wait_for_timeout(400)
        check(await pg.locator(".bub.me").count() == 2, "Enter 直接发送")
        check(await pg.locator("#genPrompt").input_value() == "", "发送后清空输入框")

        # 5. 更多参数折叠
        check(not await pg.locator("#genAdv").is_visible(), "高级参数默认收起")
        await pg.click("#genMore")
        await pg.wait_for_timeout(200)
        check(await pg.locator("#genAdv").is_visible(), "点「更多」展开")

        # 6. 示例 chips 回填
        await pg.click(".gen-ex >> nth=0")
        await pg.wait_for_timeout(200)
        v = await pg.locator("#genPrompt").input_value()
        check(len(v) > 20, "示例 chip 回填提示词")

        await pg.screenshot(path=OUT + "/chat_panel.png", full_page=False)

        # 7. 窄屏 375px 无横向溢出
        await pg.set_viewport_size({"width": 375, "height": 720})
        await pg.wait_for_timeout(400)
        ov = await pg.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        check(ov <= 1, f"375px 无横向溢出（溢出 {ov}px）")
        await pg.screenshot(path=OUT + "/chat_panel_375.png", full_page=False)

        check(not errs, f"无 JS 错误（{errs[:2]}）" if errs else "无 JS 错误")
        await b.close()
        print("RESULT:", "ALL_OK" if ok else "HAS_FAIL")


asyncio.run(main())
