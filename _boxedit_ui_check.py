# -*- coding: utf-8 -*-
"""方框复查页「编辑台词 / 重新 OCR 识别 / 语音识别」三项的 UI 回归。

覆盖：
  1. 工具条只剩 4 个动作按钮，且文案是新的（不再出现"现场复核""回填方框"）
  2. 页面里彻底搜不到 bxFill / bxViews / bxCorner 这些旧控件
  3. 点「编辑台词」弹窗正常：文本框预填当前句、起止时间可改、有 save_box 勾选
  4. 改一个字保存 → toast 出现、清单刷新为新文本
  5. 非法时间（结束早于开始）→ 弹"没保存"告警，不写库
  6. 还原文本，库里不留残留
  7. 「重新 OCR 识别」点下去能出结果面板（三帧表格）
  8. 「语音识别这句」点下去能出结果面板

依赖 web 服务已在 8771 起。用法：
    python _boxedit_ui_check.py
"""
import os
import sys
import time
import json
import urllib.request

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
    """等 toast 出现且含任一关键词。"""
    t0 = time.time()
    txt = ""
    while (time.time() - t0) * 1000 < timeout_ms:
        pg.wait_for_timeout(400)
        txt = pg.evaluate(
            "() => {const e = document.getElementById('toast');"
            " return e ? e.textContent : ''}") or ""
        if txt and any(k in txt for k in keys):
            return txt
    return txt


def pick_first_lib(pg):
    opts = pg.eval_on_selector_all(
        "#bxPick option", "els => els.map(e => e.value)")
    pg.select_option("#bxPick", opts[0])
    pg.wait_for_selector("#bxRows .bxrow", timeout=120000)
    pg.wait_for_timeout(2000)


def open_edit(pg):
    """点开编辑弹窗并等到输入框可用。

    别用 pg.click("#bxEdit") 裸调：上一个弹窗刚关掉时遮罩还在，
    点会被吞掉，接着的 wait_for_selector 就白等到超时。直接调 openBxEdit
    并等弹窗真的显示出来，稳。
    """
    pg.evaluate("() => {const m=document.getElementById('modal');"
                " if (m) m.classList.add('hidden');}")
    pg.evaluate("() => {const b=document.getElementById('bxEdit');"
                " if (b) b.click();}")
    pg.wait_for_selector("#bxEText", state="visible", timeout=20000)


def close_edit(pg):
    pg.evaluate("() => {const b=document.getElementById('mCancel');"
                " if (b) b.click(); else"
                " document.getElementById('modal').classList.add('hidden');}")
    pg.wait_for_timeout(300)


def saved_text(pg):
    return pg.evaluate(
        "() => {const e = document.getElementById('bxSavedTxt');"
        " return e ? e.textContent.trim() : ''}") or ""


def main():
    from playwright.sync_api import sync_playwright

    libs = api_get("/api/boxaudit/library")
    if not libs:
        print("没有已索引素材，跳过")
        return
    target = max(libs, key=lambda x: x.get("lines", 0))
    print(f"目标素材：{target['name']}（{target['lines']} 条）")

    errs = []
    with sync_playwright() as p:
        exe = (r"C:\Users\Administrator\AppData\Local\ms-playwright"
               r"\chromium-1223\chrome-win64\chrome.exe")
        b = p.chromium.launch(headless=True,
                              executable_path=exe if os.path.exists(exe) else None)
        pg = b.new_page(viewport={"width": 1440, "height": 960})
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(f"[{m.type}] {m.text}")
              if m.type == "error" else None)

        print("\n=== 1. 工具条 ===")
        # 不能用 networkidle：页面开着实时通道（/api/events 长连接），
        # 网络永远不空闲，goto 会一直等到超时。domcontentloaded + 轮询元素。
        pg.goto(BASE + "/#box", wait_until="domcontentloaded", timeout=60000)
        # <option> 在 Playwright 眼里永远"不可见"，wait_for_selector 会空等
        # 到超时（locator 能解析到但 visible 检查过不去）。用 attached 状态。
        pg.wait_for_selector("#bxPick option", state="attached", timeout=60000)
        pg.wait_for_timeout(1500)
        pick_first_lib(pg)
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
        nw = pg.evaluate("() => document.getElementById('bxImg').naturalWidth")
        check(nw > 0, f"台词帧出图（宽 {nw}px）")

        # 说明要挂在按钮上，不是写在总览页里——逐个按钮查悬停文案
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
        check(pg.is_visible("#bxESaveBox"), "有「同时写入方框」勾选")
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
        # 关键：改一句话不该触发整份清单重载（两千条要重跑 verify，几秒）。
        # 这里等面板刷新；超 6 秒就说明退化成全量 reload 了。
        ok = False
        for _ in range(20):
            pg.wait_for_timeout(300)
            now = saved_text(pg)
            if now == new_text:
                ok = True
                break
        if not ok:
            # 打印真值 + 前端分支日志，别只报"不一致"
            dbg = pg.evaluate(
                "() => ({saved: (document.getElementById('bxSavedTxt')||{}).textContent,"
                " cur: BX.cur, inList: (BX.list.find(x => x.id === BX.cur)||{}).text,"
                " n: BX.list.length})")
            print(f"  实际面板值：{dbg}")
            for l in errs:
                if "dbg-save" in l:
                    print(f"    前端日志：{l}")
        print(f"  面板刷新耗时 {time.time() - t0:.1f}s")
        check(ok, "清单刷新为新文本")
        check(time.time() - t0 < 8, "保存没有退化成整表重载")
        first_row = pg.evaluate(
            "() => {const e = document.querySelector('#bxRows .bxtx');"
            " return e ? e.textContent.trim() : ''}")
        check(new_text in first_row, "左侧清单同步显示新文本")

        print("\n=== 5. 非法时间被拒 ===")
        # 第 5 步会故意打出一个 400，那是**预期**的拒绝。
        # 错误收集从这一步之后才开始，否则最后会误判成 JS 报错。
        open_edit(pg)
        pg.fill("#bxEText", new_text)
        pg.fill("#bxEStart", "30")
        pg.fill("#bxEEnd", "5")
        pg.click("#mYes")
        pg.wait_for_timeout(2500)
        alert_txt = pg.evaluate(
            "() => {const e = document.getElementById('mBody');"
            " return e ? e.textContent : ''}")
        print(f"  弹窗：{alert_txt.strip()[:60]}")
        check("结束时间" in alert_txt, "提示结束时间要晚于开始")
        pg.evaluate("() => {const b = document.getElementById('mYes');"
                    " if (b) b.click();}")
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
        # 没识别过之前，「叠识别框」应该是藏着的：没东西可叠，
        # 而且带上它服务端会重跑 OCR（1~3 秒），翻一句白等三秒。
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
        check(pg.is_visible("#bxLiveWrap"), "识别后「叠识别框」露出来")
        live = pg.evaluate(
            "() => {const e = document.getElementById('bxLiveTxt');"
            " return e ? e.textContent.trim() : ''}")
        check(bool(live), f"「重新识别结果」栏填上了内容（{live[:16]}）")
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
            " return e ? e.textContent : ''}")
        print(f"  toast: {t}")
        print(f"  {out.strip()[:70]}")
        check("语音识别" in out, "语音识别结果面板出现")
        check(bool(t), "语音识别有 toast 反馈")
        # 报告必须能扛住同句重渲染（保存后不该自己清空）
        open_edit(pg)
        close_edit(pg)
        still = pg.evaluate(
            "() => {const e = document.getElementById('bxProbeOut');"
            " return e ? e.textContent : ''}")
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
