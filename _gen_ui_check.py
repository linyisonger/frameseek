# -*- coding: utf-8 -*-
"""对话窗口 UI 检查（Vue 3 + Element Plus 版）：渲染 / 交互 / 窄屏 / 错误路径。

引擎就绪时真发两次请求（走错误路径也要有气泡）；未就绪时改为断言
「点发送被拦且给出提示」——引擎状态取决于 ComfyUI 是否在跑，不该让
回归结果跟着 GPU 忙不忙抖。
"""
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pw import launch

BASE = "http://127.0.0.1:8771"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")

ok = True


def check(cond, msg):
    global ok
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        ok = False


def main():
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector(".fs-menu .el-menu-item", timeout=20000)
        pg.wait_for_timeout(1500)
        pg.click('.fs-menu .el-menu-item[data-tab="gen"]')
        pg.wait_for_timeout(1200)

        # 1. 基础渲染
        check(pg.locator("#chatEmpty").is_visible(), "空状态提示可见")
        check(pg.locator("#genPrompt").is_visible(), "输入框可见")
        check(pg.locator("#btnGen").is_visible(), "发送按钮可见")
        srv = pg.locator(".fs-page-hd .fs-tools .el-tag").first.inner_text()
        check("引擎" in srv, f"引擎徽标：{srv.strip()[:40]}")

        # 宽高比下拉已填充（点开看弹出的选项）
        pg.click("#genAspect >> xpath=ancestor::div[contains(@class,'el-select__wrapper')]")
        pg.wait_for_timeout(500)
        n_aspect = pg.locator(".el-select-dropdown:visible .el-select-dropdown__item").count()
        check(n_aspect >= 2, f"宽高比下拉已填充（{n_aspect} 项）")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)

        # 1b. 品牌 logo：favicon data URI + 侧边栏 SVG
        fav = pg.evaluate("() => document.querySelector('link[rel=icon]').href")
        check(fav.startswith("data:image/svg+xml,") and "fsg" in fav,
              f"favicon 已是新 logo data URI（{len(fav)} 字符）")
        check(pg.locator(".fs-mark svg").count() == 1, "侧边栏 .fs-mark 内联 SVG 存在")
        check(pg.locator(".fs-mark svg rect").count() >= 2
              and pg.locator(".fs-mark svg path").count() >= 1,
              "侧边栏 logo 图形完整（渐变底+帧格+播放键）")

        ready = pg.evaluate("__FS.gen ? __FS.gen.ready : false")

        if ready:
            # 2. 空提示词：按钮直接禁用（Vue 版的拦截方式），不出气泡
            check(pg.locator("#btnGen").is_disabled(), "空提示词时发送按钮禁用")

            # 3. 发送路径
            pg.fill("#genPrompt", "A cat sitting on a windowsill, morning light.")
            pg.click("#btnGen")
            pg.wait_for_timeout(2500)
            check(pg.locator(".fs-bub.me").count() == 1, "用户气泡出现")
            check(pg.locator(".fs-bub.sys").count() >= 1, "系统气泡出现")
            meta = pg.locator(".fs-bub.sys").last.inner_text()
            print("    系统气泡内容:", meta.strip().replace("\n", " / ")[:90])
            check(len(meta.strip()) > 4, "系统气泡有状态文字")

            # 4. Enter 发送礼仪
            pg.fill("#genPrompt", "second prompt")
            pg.press("#genPrompt", "Enter")
            pg.wait_for_timeout(500)
            check(pg.locator(".fs-bub.me").count() == 2, "Enter 直接发送")
            check(pg.locator("#genPrompt").input_value() == "", "发送后清空输入框")
        else:
            # 引擎未就绪：发送按钮直接禁用，这本身就是拦截
            print("    （引擎未就绪，跳过真实发送，断言按钮禁用态）")
            check(pg.locator("#btnGen").is_disabled(), "引擎未就绪时发送按钮禁用")
            pg.fill("#genPrompt", "A cat sitting on a windowsill.")
            pg.wait_for_timeout(300)
            check(pg.locator("#btnGen").is_disabled(),
                  "填了提示词但引擎未就绪，发送仍禁用")

        # 5. 更多参数折叠
        check(not pg.locator("#genAdv").is_visible(), "高级参数默认收起")
        pg.click("#genMore")
        pg.wait_for_timeout(300)
        check(pg.locator("#genAdv").is_visible(), "点「更多」展开")

        # 6. 示例 chips 回填
        pg.click(".gen-ex >> nth=0")
        pg.wait_for_timeout(300)
        v = pg.locator("#genPrompt").input_value()
        check(len(v) > 20, "示例 chip 回填提示词")

        pg.screenshot(path=os.path.join(OUT, "chat_panel.png"), full_page=False)

        # 7. 窄屏 375px 无横向溢出
        pg.set_viewport_size({"width": 375, "height": 720})
        pg.wait_for_timeout(500)
        ov = pg.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        check(ov <= 1, f"375px 无横向溢出（溢出 {ov}px）")
        pg.screenshot(path=os.path.join(OUT, "chat_panel_375.png"), full_page=False)

        check(not errs, f"无 JS 错误（{errs[:2]}）" if errs else "无 JS 错误")
        b.close()
        print("RESULT:", "ALL_OK" if ok else "HAS_FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
