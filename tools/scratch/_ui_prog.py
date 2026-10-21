# -*- coding: utf-8 -*-
"""验证两件事：
1. OCR / 截图分片的进度能不能在页面上真的看见（右下角浮窗）
2. 重新识别台词是不是先清空旧台词再按最新逻辑识别
"""
import os
import sys
import time
import json
import sqlite3
import urllib.request

os.environ.setdefault("NO_PROXY", "*")
from playwright.sync_api import sync_playwright

EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                   r"ms-playwright\chromium-1223\chrome-win64\chrome.exe")
BASE = "http://127.0.0.1:8771"
DB = os.path.abspath("_testlib/.frameseek/index.db")
OUT = os.path.abspath("_out/ui2")
os.makedirs(OUT, exist_ok=True)

ROOT = os.path.abspath("_testlib")


def db_stat():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    v = c.execute("SELECT id,name,line_count,line_source,status FROM videos"
                  " ORDER BY id LIMIT 1").fetchone()
    n = c.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    f = c.execute("SELECT COUNT(*) FROM lines_fts").fetchone()[0]
    s = c.execute("SELECT COUNT(*) FROM lines WHERE shot IS NOT NULL"
                  " AND shot<>''").fetchone()[0]
    b = c.execute("SELECT COUNT(*) FROM lines WHERE box IS NOT NULL"
                  " AND box<>''").fetchone()[0]
    c.close()
    return dict(v), n, f, s, b


def log(*a):
    print(*a, flush=True)


def main():
    v0, n0, f0, s0, b0 = db_stat()
    log(f"重识别前：{v0['name']} 台词 {n0} 条 / fts {f0} / 截图 {s0} / 带框 {b0}"
        f" / 来源 {v0['line_source']}")

    opens = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(path, **kw):
        req = urllib.request.Request(
            BASE + path, data=json.dumps(kw).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        return json.loads(opens.open(req, timeout=300).read())

    # 上一轮测试可能把台词清空了。先用同名 srt 快速导回来，
    # 这样"重新识别会清空这些台词"才有的可清。
    if n0 == 0:
        log("台词是空的，先用同名 srt 导回来做基线…")
        post("/api/video/reimport", root=ROOT, video_id=v0["id"])
        for _ in range(120):
            time.sleep(3)
            vv, nn, _, _, _ = db_stat()
            if nn:
                n0, v0 = nn, vv
                break
        log(f"基线恢复：台词 {n0} 条 / 来源 {v0['line_source']}")
    snaps = []
    errs = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True,
                              executable_path=EXE if os.path.exists(EXE) else None)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
        pg.goto(BASE + "/#assets", wait_until="domcontentloaded")
        pg.wait_for_timeout(2500)

        # 找到测试素材，打开三点菜单
        pg.fill("#vfilter", "test5min")
        pg.wait_for_timeout(1200)
        card = pg.query_selector(".vcard")
        if not card:
            log("✗ 没找到测试素材卡片")
            b.close()
            return
        card.query_selector(".vmore").click()
        pg.wait_for_timeout(400)
        pg.click("text=重新识别台词")
        pg.wait_for_timeout(800)
        log("弹窗文案：")
        log("  " + pg.inner_text("#mBody").replace("\n", "\n  "))
        fcb = pg.query_selector("#mForce")
        log(f"  强制重跑 OCR 默认勾选：{fcb.is_checked() if fcb else 'N/A'}")
        if fcb and not fcb.is_checked():
            fcb.click()          # 这次要验证的是"按最新 OCR 逻辑重跑"
        pg.screenshot(path=os.path.join(OUT, "01_confirm.png"))
        pg.click("#mYes")
        pg.wait_for_timeout(1500)

        # 立刻看旧台词是不是已经清掉了
        v1, n1, _, _, _ = db_stat()
        log(f"点下确认 1.5s 后：库里台词 {n1} 条（应为 0）· 状态 {v1['status']}")

        # 抓进度浮窗
        seen = set()
        t0 = time.time()
        shot_done = False
        while time.time() - t0 < 420:
            vis = pg.evaluate(
                "() => {const e=document.getElementById('tasksLibs');"
                "return e && !e.classList.contains('hidden') && e.innerText.trim();}")
            if vis:
                key = vis.strip()
                if key not in seen:
                    seen.add(key)
                    snaps.append((round(time.time() - t0, 1), key))
                if not shot_done and len(seen) >= 2:
                    pg.screenshot(path=os.path.join(OUT, "02_dock.png"))
                    el = pg.query_selector("#tasksLibs")
                    if el:
                        el.screenshot(path=os.path.join(OUT, "03_dock_card.png"))
                    shot_done = True
            else:
                if seen:
                    break
            pg.wait_for_timeout(1500)
        log(f"浮窗状态变化 {len(snaps)} 次：")
        for t, s in snaps[:6] + (snaps[-3:] if len(snaps) > 6 else []):
            log(f"  [{t:6.1f}s] " + s.replace("\n", " | "))
        pg.screenshot(path=os.path.join(OUT, "04_after.png"))
        try:
            toast = pg.evaluate("() => {const e=document.getElementById('toast');"
                                "return e ? e.textContent : ''}")
            log("toast: " + (toast or "(无)"))
        except Exception:
            pass
        b.close()

    v2, n2, f2, s2, b2 = db_stat()
    log(f"重识别后：台词 {n2} 条 / fts {f2} / 截图 {s2} / 带框 {b2}"
        f" / 来源 {v2['line_source']} / 状态 {v2['status']}")
    log("")
    ok = True
    if n1 != 0:
        log("✗ 旧台词没被清空"); ok = False
    else:
        log("✓ 旧台词先清空")
    if f2 != n2:
        log(f"✗ 全文索引和台词数不一致（{f2} vs {n2}）——幽灵行还在"); ok = False
    else:
        log("✓ 全文索引与台词一致，无幽灵行")
    if not snaps:
        log("✗ 进度浮窗从头到尾没出现过"); ok = False
    else:
        joined = "\n".join(s for _, s in snaps)
        has_ocr = "OCR" in joined
        has_frame = "/" in joined and "%" in joined
        has_shots = "截图分片" in joined
        log(f"✓ 浮窗有进度：OCR阶段={has_ocr} 帧进度={has_frame} 截图分片={has_shots}")
        if not (has_ocr and has_frame):
            ok = False
    if v2["line_source"] != "ocr":
        log(f"✗ 来源是 {v2['line_source']}，不是按最新 OCR 逻辑重跑的"); ok = False
    else:
        log("✓ 走的是 OCR 重跑（没读回旧 .srt）")
    if errs:
        log(f"✗ 控制台报错 {len(errs)} 条：" + json.dumps(errs[:3], ensure_ascii=False))
        ok = False
    else:
        log("✓ 无控制台报错")
    log("")
    log("总判定：" + ("全部通过" if ok else "有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
