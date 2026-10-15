# -*- coding: utf-8 -*-
"""白板手绘面板 UI 检查 + Web 全链路 e2e（Vue 3 + Element Plus 版）。

跑法（在项目根目录）：
    python _handdraw_ui_check.py

它会真实提交一次渲染任务（本地 CPU），所以顺带验证 /api/handdraw/render
→ tasks 表 → 实时事件 → 气泡媒体卡片这条链路。
截图落在 _out/hd_ui_*.png。

驱动方式变化：原生 JS 版直接调全局 hdRender()/hdRegions()；Vue 版状态在
组件里，通过 __FS.hd（响应式状态）+ __FS.hdApi（方法）驱动——这是 app.js
里专门给回归脚本留的口子。
"""
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pw import launch

BASE = "http://127.0.0.1:8771"
PROJECT = "光电效应"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")

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


def main():
    with sync_playwright() as p:
        br = launch(p)
        pg = br.new_page(viewport={"width": 1360, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        pg.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector(".fs-menu .el-menu-item", timeout=20000)
        pg.wait_for_timeout(1500)

        # --- 切到手绘面板 ---
        pg.click('.fs-menu .el-menu-item[data-tab="hd"]')
        pg.wait_for_selector(".fs-steps", timeout=15000)
        pg.wait_for_timeout(1500)

        check(pg.locator('.fs-menu .el-menu-item[data-tab="hd"]').count() == 1,
              "侧边栏有「手绘」入口")
        check(pg.locator(".fs-steps .el-step").count() == 6,
              "六步流程条渲染完整")
        srv = (pg.locator(".fs-page-hd .fs-tools .el-tag").first.inner_text()).strip()
        check("引擎就绪" in srv, f"引擎徽标显示就绪（{srv[:40]}）")
        # 2026-10-15：手绘工程整体搬出素材库，#hdLib 素材库选择器已随之移除
        check(pg.locator("#hdLib").count() == 0,
              "手绘面板不再有素材库下拉（工程已独立于素材库）")

        pg.screenshot(path=os.path.join(OUT, "hd_ui_1_top.png"), full_page=False)

        # --- 各卡片关键控件（v-show 隐藏仍在 DOM 里，count 都能查到）---
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
            check(pg.locator(sel).count() == 1, label)

        # --- 分步向导：一次只摆当前步骤，别把六步输入框全怼脸上 ---
        def goto(n):
            pg.click(f".fs-steps .el-step:nth-child({n})")
            pg.wait_for_timeout(350)

        check(not pg.locator("#hdTargetSec").first.is_visible(),
              "初始停在第 1 步，第 2 步的输入框不可见")
        goto(2)
        check(pg.locator("#hdTargetSec").first.is_visible()
              and not pg.locator("#hdUpScript").first.is_visible(),
              "跳到第 2 步只显示第 2 步控件")
        goto(1)
        check(pg.locator("#hdUpScript").first.is_visible(), "上一步/点步骤条能回到第 1 步")

        # --- 标注模板 ---
        goto(3)
        pg.click("#hdTplAnn")
        pg.wait_for_timeout(400)
        ann = pg.input_value("#hdAnn")
        check('"elements"' in ann and '"sceneDurationMs"' in ann,
              "标注模板已填入编辑框")
        try:
            pg.evaluate("JSON.parse(document.querySelector('#hdAnn').value)")
            check(True, "模板是合法 JSON")
        except Exception:
            check(False, "模板是合法 JSON")

        # --- 配音未勾联网许可应被拦（第 6 步控件，先跳步） ---
        goto(6)
        pg.fill("#hdVoiceText", "这是一段试听文案，包含数字 3 和问句：可以吗？")
        pg.click("#hdAudition")
        pg.wait_for_timeout(600)
        chat = pg.locator("#hdChat").inner_text()
        check("联网许可" in chat, "未勾联网许可时试听被拦并有提示")

        pg.screenshot(path=os.path.join(OUT, "hd_ui_2_form.png"))

        # --- 真跑一次渲染（Web 全链路 e2e） ---
        pg.evaluate(f"__FS.hd.project = {PROJECT!r}")
        pg.evaluate("__FS.hdApi.refresh()")
        pg.wait_for_timeout(1200)
        tl = (pg.locator("#hdTlStatus").inner_text()).strip()
        proj = (pg.locator("#hdProj").inner_text()).strip()
        check(tl == "approved", f"时间轴状态已批准（{tl}）")
        check(PROJECT in proj, f"工程卡片显示工程名（{proj[:50]}）")

        pg.evaluate("__FS.hdApi.regions()")
        pg.wait_for_timeout(6000)
        chat2 = pg.locator("#hdChat").inner_text()
        check("分区检查图" in chat2, "分区检查图已进气泡")
        check(pg.locator("#hdChat .bub-media img").count() >= 1,
              "检查图在气泡里以图片渲染")

        # 短预览渲染
        pg.evaluate("__FS.hdApi.render(true)")
        got_run = False
        for _ in range(40):
            pg.wait_for_timeout(1000)
            if pg.locator("#hdChat .bub-run").count() >= 1:
                got_run = True
                break
        check(got_run, "渲染任务出现进度气泡")
        pg.screenshot(path=os.path.join(OUT, "hd_ui_3_running.png"))

        # 等完成
        done = False
        for _ in range(120):
            pg.wait_for_timeout(1000)
            if pg.locator("#hdChat .bub-media video").count() >= 1:
                done = True
                break
        check(done, "渲染完成后气泡出现视频卡片")
        pg.screenshot(path=os.path.join(OUT, "hd_ui_4_done.png"))

        # --- 窄屏 ---
        pg.set_viewport_size({"width": 390, "height": 780})
        pg.wait_for_timeout(700)
        pg.screenshot(path=os.path.join(OUT, "hd_ui_5_narrow.png"))
        check(True, "窄屏截图已保存")

        real_errs = [e for e in errs if "favicon" not in e.lower()]
        check(not real_errs, f"无 JS 报错（{len(real_errs)}）")
        if real_errs:
            for e in real_errs[:5]:
                print("       ", e[:140])

        br.close()

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
