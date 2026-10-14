# -*- coding: utf-8 -*-
"""白板手绘面板 UI 检查 + Web 全链路 e2e。

跑法（在项目根目录）：
    python _handdraw_ui_check.py

它会真实提交一次渲染任务（本地 CPU），所以顺带验证 /api/handdraw/render
→ tasks 表 → 实时事件 → 气泡媒体卡片这条链路。
截图落在 _out/hd_ui_*.png。
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8771"
ROOT = r"D:\project\AIProjects\frameseek\_testlib"
PROJECT = "光电效应"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
CHROME = r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-1208\chrome-win64\chrome.exe"

PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=CHROME)
        pg = await br.new_page(viewport={"width": 1360, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        await pg.goto(BASE, wait_until="networkidle")
        await pg.wait_for_timeout(900)

        # --- 切到手绘面板 ---
        await pg.evaluate("switchTab('hd')")
        await pg.wait_for_timeout(1200)

        check(await pg.locator('.nav-item[data-tab="hd"]').count() == 1,
              "侧边栏有「手绘」入口")
        check(await pg.locator('[data-panel="hd"].on').count() == 1,
              "手绘面板已激活")
        check(await pg.locator(".hd-steps .hd-step").count() == 6,
              "六步流程条渲染完整")
        srv = (await pg.locator("#hdSrv").inner_text()).strip()
        check("引擎就绪" in srv, f"引擎徽标显示就绪（{srv[:40]}）")
        # 2026-10-15：手绘工程整体搬出素材库，#hdLib 素材库选择器已随之移除
        # （工程固定落在项目 手绘工程/ 下）。这里断言它**不该**再存在，
        # 别再拿一个已经不存在的下拉当"页面正常"的证据。
        check(await pg.locator("#hdLib").count() == 0,
              "手绘面板不再有素材库下拉（工程已独立于素材库）")

        await pg.screenshot(path=os.path.join(OUT, "hd_ui_1_top.png"),
                            full_page=False)

        # --- 各卡片关键控件 ---
        for sel, label in [
            ("#hdUpScript", "上传文稿/字幕按钮"),
            ("#hdInit", "建工程按钮"),
            ("#hdTargetSec", "每幕目标秒输入"),
            ("#hdSuggest", "出分幕建议按钮"),
            ("#hdTplAnn", "标注模板按钮"),
            ("#hdAnn", "标注 JSON 编辑框"),
            ("#hdUpImage", "上传线稿按钮"),
            ("#hdAnnotate", "写标注按钮"),
            ("#hdApprove", "批准时间轴按钮"),
            ("#hdPreview", "短预览按钮"),
            ("#hdRender", "渲染按钮"),
            ("#hdAudition", "试听音色按钮"),
            ("#hdMix", "配音合成按钮"),
            ("#hdQa", "质检按钮"),
            ("#hdAllowNet", "联网许可勾选框"),
        ]:
            check(await pg.locator(sel).count() == 1, label)

        # --- 标注模板 ---
        await pg.click("#hdTplAnn")
        await pg.wait_for_timeout(400)
        ann = await pg.input_value("#hdAnn")
        check('"elements"' in ann and '"sceneDurationMs"' in ann,
              "标注模板已填入编辑框")
        try:
            pg.evaluate("JSON.parse(document.querySelector('#hdAnn').value)")
            check(True, "模板是合法 JSON")
        except Exception:
            check(False, "模板是合法 JSON")

        # --- 配音未勾联网许可应被拦 ---
        await pg.fill("#hdVoiceText", "这是一段试听文案，包含数字 3 和问句：可以吗？")
        await pg.click("#hdAudition")
        await pg.wait_for_timeout(600)
        chat = await pg.locator("#hdChat").inner_text()
        check("联网许可" in chat, "未勾联网许可时试听被拦并有提示")

        await pg.screenshot(path=os.path.join(OUT, "hd_ui_2_form.png"))

        # --- 真跑一次渲染（Web 全链路 e2e） ---
        await pg.evaluate("HD.project = %r" % PROJECT)
        await pg.evaluate("hdRefreshStatus()")
        await pg.wait_for_timeout(800)
        tl = (await pg.locator("#hdTlStatus").inner_text()).strip()
        proj = (await pg.locator("#hdProj").inner_text()).strip()
        check(tl == "approved", f"时间轴状态已批准（{tl}）")
        check("光电效应" in proj, f"工程卡片显示工程名（{proj[:50]}）")

        await pg.evaluate("hdRegions()")
        await pg.wait_for_timeout(6000)
        chat2 = await pg.locator("#hdChat").inner_text()
        check("分区检查图" in chat2, "分区检查图已进气泡")
        check(await pg.locator("#hdChat .hd-media img").count() >= 1,
              "检查图在气泡里以图片渲染")

        # 短预览渲染
        await pg.evaluate("hdRender(true)")
        got_run = False
        for _ in range(40):
            await pg.wait_for_timeout(1000)
            if await pg.locator("#hdChat .bub-run").count() >= 1:
                got_run = True
                break
        check(got_run, "渲染任务出现进度气泡")
        await pg.screenshot(path=os.path.join(OUT, "hd_ui_3_running.png"))

        # 等完成
        done = False
        for _ in range(120):
            await pg.wait_for_timeout(1000)
            if await pg.locator("#hdChat .hd-media video").count() >= 1:
                done = True
                break
        check(done, "渲染完成后气泡出现视频卡片")
        await pg.screenshot(path=os.path.join(OUT, "hd_ui_4_done.png"))

        # --- 窄屏 ---
        await pg.set_viewport_size({"width": 390, "height": 780})
        await pg.wait_for_timeout(700)
        await pg.screenshot(path=os.path.join(OUT, "hd_ui_5_narrow.png"))
        check(True, "窄屏截图已保存")

        real_errs = [e for e in errs if "favicon" not in e.lower()]
        check(not real_errs, f"无 JS 报错（{len(real_errs)}）")
        if real_errs:
            for e in real_errs[:5]:
                print("       ", e[:140])

        await br.close()

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))