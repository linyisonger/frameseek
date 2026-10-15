# -*- coding: utf-8 -*-
"""方框复查页「编辑台词 / 重新 OCR 识别 / 语音识别」三项的 UI 回归
（Vue 3 + Element Plus 版）。

覆盖：
  1. 工具条只剩 4 个动作按钮，且文案是新的（不再出现"现场复核""回填方框"）
  2. 页面里彻底搜不到 bxFill / bxViews / bxCorner 这些旧控件
  3. 点「编辑台词」弹窗正常：文本框预填当前句、起止时间可改、有 save_box 勾选
  4. 改一个字保存 → toast 出现、清单刷新为新文本
  5. 非法时间（结束早于开始）→ 弹"没保存"告警，不写库
  6. 还原文本，库里不留残留
  7. 「重新 OCR 识别」点下去能出结果面板（三帧表格）
  8. 「语音识别这句」点下去能出结果面板

适配说明：自定义 modal 换成了 el-dialog、toast 换成了 ElMessage、
原生 select 换成了 el-select——选片直接走 __FS.box 状态口子，
弹窗/toast 走 Element Plus 的真实 DOM（.el-dialog / .el-message /
.el-message-box）。
"""
import json
import os
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pw import launch

BASE = "http://127.0.0.1:8771"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)

PASS = 0
FAIL = 0
_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")
    return bool(cond)


def api_get(p):
    return json.loads(_op.open(BASE + p, timeout=300).read().decode("utf-8"))


def wait_toast(pg, keys, timeout_ms=60000):
    """等 ElMessage 出现且含任一关键词。"""
    t0 = time.time()
    txt = ""
    while (time.time() - t0) * 1000 < timeout_ms:
        pg.wait_for_timeout(300)
        txt = pg.evaluate(
            "() => {const e = document.querySelector('.el-message');"
            " return e ? e.textContent : ''}") or ""
        if txt and any(k in txt for k in keys):
            return txt
    return txt


def open_edit(pg):
    pg.click("#bxEdit")
    pg.wait_for_selector("#bxEText", state="visible", timeout=20000)
    pg.wait_for_timeout(300)


def close_edit(pg):
    pg.click("#mCancel")
    pg.wait_for_timeout(400)


def saved_text(pg):
    return (pg.evaluate(
        "() => {const e = document.getElementById('bxSavedTxt');"
        " return e ? e.textContent.trim() : ''}") or "")


def main():
    libs = api_get("/api/boxaudit/library")
    if not libs:
        print("没有已索引素材，跳过")
        return
    target = max(libs, key=lambda x: x.get("lines", 0))
    print(f"目标素材：{target['name']}（{target['lines']} 条）")

    errs = []
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1440, "height": 960})
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(f"[{m.type}] {m.text}")
              if m.type == "error" else None)

        print("\n=== 1. 工具条 ===")
        # 不能用 networkidle：实时通道（/ws、/api/events）永不空闲
        pg.goto(BASE + "/#box", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector(".fs-menu .el-menu-item", timeout=20000)
        pg.wait_for_timeout(1500)
        # 选片走状态口子：直接设 pick（root|id），省掉 el-select 逐层点击
        pg.evaluate(
            "v => { __FS.box.pick = v; }",
            f"{target['root']}|{target['id']}")
        pg.wait_for_selector(".fs-bxrow", timeout=120000)
        pg.wait_for_timeout(1500)

        acts = pg.eval_on_selector_all(
            "#bxView .bxacts button", "els => els.map(e => e.textContent.trim())")
        print(f"  按钮：{acts}")
        check(any("重新 OCR 识别" in a for a in acts), "有「重新 OCR 识别」")
        check(any("语音识别" in a for a in acts), "有「语音识别这句」")
        check(any("编辑台词" in a for a in acts), "有「编辑台词」")
        check(not any("现场复核" in a for a in acts), "没有「现场复核」")
        check(not any("回填" in a for a in acts), "没有「回填方框」")

        print("\n=== 2. 旧控件已清干净 ===")
        html = pg.evaluate("() => document.body.innerHTML")
        for dead in ("bxFill", "bxViews", "bxCorner", "doBxFill"):
            check(dead not in html, f"页面无 {dead}")
        # 等台词帧真的加载出来（naturalWidth > 0），抽帧要一两秒
        loaded = False
        for _ in range(40):
            pg.wait_for_timeout(500)
            if pg.evaluate(
                "() => {const e = document.getElementById('bxImg');"
                " return e ? e.naturalWidth : 0}") > 0:
                loaded = True
                break
        nw = pg.evaluate(
            "() => document.getElementById('bxImg').naturalWidth")
        check(loaded and nw > 0, f"台词帧出图（宽 {nw}px）")

        tips = pg.eval_on_selector_all(
            "#bxView .bxacts button",
            "els => els.map(e => [e.textContent.trim(), (e.getAttribute('title')||'').trim()])")
        no_tip = [t[0] for t in tips if len(t[1]) < 6]
        check(not no_tip, f"每个动作按钮都有悬停说明（缺：{no_tip}）")
        prev_tip = pg.eval_on_selector(
            "#bxPrev", "e => (e.getAttribute('title')||'').trim()")
        check("快捷键" in prev_tip, f"翻页按钮提示了快捷键（{prev_tip}）")

        print("\n=== 3. 编辑弹窗 ===")
        orig = saved_text(pg)
        open_edit(pg)
        pre = pg.eval_on_selector("#bxEText", "e => e.value")
        check(pre.strip() == orig.strip(),
              f"文本框预填当前句（{pre.strip()[:18]}）")
        check(pg.is_visible("#bxEStart") and pg.is_visible("#bxEEnd"),
              "起止时间输入框可见")
        dlg_txt = pg.evaluate(
            "() => {const d = document.querySelector('.el-dialog');"
            " return d ? d.textContent : ''}") or ""
        check("同时把这次识别到的画面方框写进库" in dlg_txt,
              "有「同时写入方框」勾选")
        pg.screenshot(path=os.path.join(OUT, "boxedit_dialog.png"))
        close_edit(pg)

        print("\n=== 4. 保存改动 ===")
        new_text = orig.strip() + "·改"
        t0 = time.time()
        open_edit(pg)
        pg.fill("#bxEText", new_text)
        pg.click("#mYes")
        t = wait_toast(pg, ["已保存", "没保存", "失败"])
        print(f"  toast: {t}（{time.time() - t0:.1f}s）")
        check("已保存" in t, "保存成功 toast")
        # 关键：改一句话不该触发整份清单重载（两千条要重跑 verify，几秒）
        refreshed = False
        for _ in range(20):
            pg.wait_for_timeout(300)
            if saved_text(pg) == new_text:
                refreshed = True
                break
        print(f"  面板刷新耗时 {time.time() - t0:.1f}s")
        check(refreshed, "清单刷新为新文本")
        check(time.time() - t0 < 8, "保存没有退化成整表重载")
        first_row = pg.evaluate(
            "() => {const e = document.querySelector('.fs-bxrow .bxtx');"
            " return e ? e.textContent.trim() : ''}")
        check(new_text in first_row, "左侧清单同步显示新文本")

        print("\n=== 5. 非法时间被拒 ===")
        # 这一步故意打出一个 400（预期的拒绝），错误收集之后重置
        open_edit(pg)
        pg.fill("#bxEText", new_text)
        pg.fill("#bxEStart", "30")
        pg.fill("#bxEEnd", "5")
        pg.click("#mYes")
        pg.wait_for_timeout(2500)
        alert_txt = pg.evaluate(
            "() => {const e = document.querySelector('.el-message-box');"
            " return e ? e.textContent : ''}") or ""
        print(f"  弹窗：{alert_txt.strip()[:60]}")
        check("结束时间" in alert_txt, "提示结束时间要晚于开始")
        pg.evaluate(
            "() => {const b = document.querySelector("
            "'.el-message-box__btns button'); if (b) b.click();}")
        pg.wait_for_timeout(800)
        errs.clear()          # 前面的 400 是故意的，从这里重新收

        print("\n=== 6. 还原 ===")
        open_edit(pg)
        pg.fill("#bxEText", orig)
        pg.click("#mYes")
        t = wait_toast(pg, ["已保存", "失败"])
        check("已保存" in t, "还原成功")
        check(saved_text(pg) == orig, "库里文本已还原")

        print("\n=== 7. 重新 OCR 识别 ===")
        # 没识别过之前「叠识别框」是藏着的：没东西可叠，带上它服务端
        # 还会重跑 OCR（1~3 秒），翻一句白等三秒
        check(not pg.is_visible("#bxLiveWrap"), "识别前「叠识别框」是藏着的")
        pg.click("#bxProbe")
        pg.wait_for_selector("#bxProbeOut .bxpbtbl tr", timeout=180000)
        n = pg.eval_on_selector_all("#bxProbeOut .bxpbtbl tr", "e => e.length")
        check(n >= 1, f"出 {n} 行逐帧结果")
        verdict = pg.evaluate(
            "() => {const e = document.querySelector('#bxProbeOut .bxpv b');"
            " return e ? e.textContent.trim() : ''}")
        print(f"  结论：{verdict}")
        check(verdict in ("识别一致", "识别不一致"), "结论文案已改成识别口径")
        # el-checkbox 会把 id 透传到内部隐藏 input（opacity:0），
        # is_visible 直接查它永远是 False——查外层的 label
        check(pg.is_visible("label:has(#bxLiveWrap)"),
              "识别后「叠识别框」露出来")
        live = pg.evaluate(
            "() => {const e = document.getElementById('bxLiveTxt');"
            " return e ? e.textContent.trim() : ''}")
        check(bool(live) and live != "还没识别",
              f"「重新识别结果」栏填上了内容（{live[:16]}）")
        # 报告必须扛得住同句重渲染
        open_edit(pg)
        close_edit(pg)
        kept = pg.evaluate(
            "() => {const e = document.querySelector('#bxProbeOut .bxpv b');"
            " return e ? e.textContent.trim() : ''}")
        check(kept == verdict, "报告在同一句内重渲染后仍然保留")

        print("\n=== 8. 语音识别这句 ===")
        pg.click("#bxAsr")
        t = wait_toast(pg, ["语音识别", "没听出"], timeout_ms=240000)
        out = pg.evaluate(
            "() => {const e = document.getElementById('bxProbeOut');"
            " return e ? e.textContent : ''}") or ""
        print(f"  toast: {t}")
        print(f"  {out.strip()[:70]}")
        check("语音识别" in out, "语音识别结果面板出现")
        check(bool(t), "语音识别有 toast 反馈")
        # 报告必须能扛住同句重渲染（保存后不该自己清空）
        open_edit(pg)
        close_edit(pg)
        still = pg.evaluate(
            "() => {const e = document.getElementById('bxProbeOut');"
            " return e ? e.textContent : ''}") or ""
        check("语音识别" in still, "语音报告在重渲染后仍保留")
        pg.screenshot(path=os.path.join(OUT, "boxedit_page.png"))

        real_errs = [e for e in errs if "favicon" not in e.lower()]
        check(not real_errs, f"无 JS 报错（{real_errs[:2]}）")
        b.close()

    print(f"\n{'=' * 46}\n通过 {PASS} / {PASS + FAIL}"
          + (f"，失败 {FAIL}" if FAIL else "，全绿"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
