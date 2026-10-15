# -*- coding: utf-8 -*-
"""索引「中途终止 + 下次接着跑」回归。

覆盖用户提的三件事里的第 2 条：
  1. 索引跑一半能停（/api/scan/stop），任务被标成 stopped；
  2. 停止后**已完成**的素材都留在库里（有台词），不会白跑；
  3. 正在识别到一半的那一条**不留半截台词**（置回 pending，下次重跑）；
  4. 再点一次「索引」会跳过已完成的接着往下跑（不重做已完成的工作）。

做法：在 _testlib/_stoplib 下用 ffmpeg 切 3 个 90 秒片段（带硬字幕、不带同名
.srt，逼它走 OCR），临时登记成素材库跑一遍，等到第 2 个片段真的开始 OCR 时
按下停止。跑完把登记删掉，hub.db 恢复原样。

清理只删索引库 + .srt，不删整个目录——本机沙箱会把单轮 >50 个文件的删除当成
危险操作直接杀掉进程（`.frameseek` 里几百张截图，一次 rmtree 就能让脚本无声
退出、连最后的汇总都打不出来）。删不干净就自动换 _stoplib2、3…

进程模型注意：core.ocr 用 multiprocessing，Windows 上必须有 __main__ 保护，
否则子进程会把整个模块重跑一遍（这里的 main() 就写在保护里）。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

os.environ.setdefault("NO_PROXY", "*")

BASE = os.environ.get("FS_BASE", "http://127.0.0.1:8771")
ROOT = os.path.abspath(os.path.join("_testlib", "_stoplib"))   # 首选目录，不干净会自动换名
SRC = os.path.abspath(os.path.join("_testlib", "test5min.mp4"))
CLIP_SEC = 90
N_CLIP = 3


def db_path(root=None):
    return os.path.join(root or ROOT, ".frameseek", "index.db")

ok = True
n = 0


def check(cond, msg):
    global ok, n
    n += 1
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        ok = False


def log(*a):
    print(*a, flush=True)


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, timeout=30):
    return json.loads(_opener.open(BASE + url, timeout=timeout).read())


def post(url, timeout=600, **kw):
    # 参数名必须叫 url：body 里本身就有个字段叫 "path"（素材目录），
    # 形参也叫 path 会撞成 "got multiple values for argument 'path'"
    req = urllib.request.Request(
        BASE + url, data=json.dumps(kw).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return json.loads(_opener.open(req, timeout=timeout).read())


def db():
    c = sqlite3.connect(db_path(), timeout=20)
    c.row_factory = sqlite3.Row
    return c


def videos():
    """{name: (id, line_count, line_source, status)}"""
    with db() as c:
        return {r["name"]: (r["id"], r["line_count"], r["line_source"],
                            r["status"])
                for r in c.execute("SELECT * FROM videos")}


def lines_of(vid):
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM lines WHERE video_id=?",
                         (vid,)).fetchone()[0]


def latest_task():
    with db() as c:
        r = c.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else {}


def _ffmpeg():
    """找 ffmpeg。Bash 沙箱里 PATH 常常是残的，所以给个绝对路径兜底。"""
    p = os.environ.get("FFMPEG")
    if p and os.path.exists(p):
        return p
    w = shutil.which("ffmpeg")
    if w:
        return w
    for c in (r"D:\Program Files\ffmpeg-7.0.2-full_build\bin\ffmpeg.exe",
              r"C:\ffmpeg\bin\ffmpeg.exe"):
        if os.path.exists(c):
            return c
    return "ffmpeg"


def _try_clean(p):
    """把目录恢复成「刚登记、还没索引」的样子。

    先删索引库和上轮 OCR 落的 .srt；删不动（沙箱把删除拦下来、或索引库被服务
    进程占着句柄）就退一步**把表清空**——测试只要「md5 去重记录没了，等于全新库」
    这一个效果，清表和删文件等价。

    **绝不删整目录**：本机沙箱对单轮超过 50 个文件的删除会直接 SIGTERM 杀掉进程
    （`.frameseek` 里几百张台词截图，一次 rmtree 就能让整个回归脚本无声退出、
    连最后的汇总报告都带走）。
    """
    if not os.path.isdir(p):
        return True
    d = db_path(p)
    try:
        if os.path.exists(d):
            os.remove(d)
        for f in os.listdir(p):
            if f.lower().endswith(".srt"):
                os.remove(os.path.join(p, f))
        return True
    except OSError:
        pass
    try:
        c = sqlite3.connect(d, timeout=20)
        with c:
            for t in ("lines", "videos", "tasks", "ignored"):
                try:
                    c.execute(f"DELETE FROM {t}")
                except sqlite3.Error:
                    pass
        c.close()
    except sqlite3.Error:
        return False
    return True


def pick_root():
    """挑一个干净的测试目录：先把 _stoplib 清一清，清不动就退到 _stoplib2、3…"""
    global ROOT
    for p in [ROOT] + [ROOT + str(i) for i in range(2, 21)]:
        if _try_clean(p):
            ROOT = p
            return p
    raise RuntimeError("_testlib 下 _stoplib* 都清不干净，手动删一下")


def make_clips():
    """切 3 个带硬字幕的片段。不带同名 .srt，逼它走 OCR（否则秒读 srt 就跑完了）"""
    os.makedirs(ROOT, exist_ok=True)
    exe = _ffmpeg()
    out = []
    for i in range(1, N_CLIP + 1):
        p = os.path.join(ROOT, f"clip{i}.mp4")
        r = subprocess.run(
            [exe, "-y", "-loglevel", "error", "-ss", str(i * 20),
             "-t", str(CLIP_SEC), "-i", SRC, "-c", "copy", p],
            capture_output=True)
        if r.returncode != 0 or not os.path.exists(p):
            log("ffmpeg 切片失败：" + r.stderr.decode("utf-8", "ignore")[:300])
            return []
        out.append(p)
    return out


def wait_until(fn, timeout, step=3.0, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    log(f"（等待超时 {timeout}s：{what}）")
    return False


def running_tasks():
    try:
        return get("/api/tasks")
    except Exception:
        return []


def main():
    if not os.path.exists(SRC):
        log("✗ 缺测试素材 _testlib/test5min.mp4")
        return 1
    try:
        st = get("/api/state")
    except Exception as e:
        log(f"✗ 连不上服务 {BASE}：{e}")
        return 1
    log(f"服务在跑：{len(st.get('libraries') or [])} 个素材库")

    log(f"测试目录：{pick_root()}")
    clips = make_clips()
    if not clips:
        return 1
    log(f"切好 {len(clips)} 个 {CLIP_SEC}s 片段 → {ROOT}")

    reg = None
    try:
        reg = post("/api/libraries", path=ROOT, name="__stoplib__")
        if not reg.get("ok"):
            log(f"✗ 临时库登记失败：{reg}")
            return 1
        lid = reg["id"]
        log(f"临时库已登记 id={lid}")

        # ---------- 第一轮：跑起来 → 停 ----------
        post(f"/api/libraries/{lid}/scan", use_asr=True, with_gif=False,
             retry_empty=True)

        check(wait_until(lambda: latest_task().get("status") == "running",
                         60, 1.0, "第一轮任务开始跑"), "第一轮索引已启动")

        def clip2_ocr_running():
            """第 1 个片段已完成（done>=1）且消息里出现 OCR = 第 2 个正在 OCR。"""
            t = latest_task()
            return (t.get("status") == "running" and (t.get("done") or 0) >= 1
                    and "OCR" in (t.get("message") or ""))

        started = wait_until(clip2_ocr_running, 420, 3.0,
                             "第 1 个片段完成、第 2 个开始 OCR")
        check(started, "跑到第 2 个片段正在 OCR（此时按停止才有意义）")

        r = post("/api/scan/stop", root=ROOT)
        check(r.get("ok") is True, f"停止接口返回 ok：{r}")

        # ---------- 停止生效 ----------
        check(wait_until(lambda: latest_task().get("status") == "stopped",
                         60, 1.5, "任务状态变成 stopped"),
              "任务被标成 stopped（不是 done）")
        t = latest_task()
        log(f"停止后的任务记录：status={t.get('status')} done={t.get('done')} "
            f"msg={t.get('message')}")
        check("已停止" in (t.get("message") or ""), "摘要里说明了已停止")
        check((t.get("done") or 0) >= 1, "摘要里带上了已完成的个数")

        v = videos()
        log(f"停止后库里的素材：{v}")
        done_names = [k for k, x in v.items() if (x[1] or 0) > 0]
        zero_names = [k for k, x in v.items() if not (x[1] or 0)]
        check(len(done_names) >= 1,
              f"已跑完的素材保住了台词：{done_names}")
        check(len(zero_names) >= 1,
              f"被中断的素材没有半截台词：{zero_names}")
        for nm in zero_names:
            vid, lc, src, status = v[nm]
            check(lc == 0 and lines_of(vid) == 0 and status == "pending",
                  f"「{nm}」留成待识别（lines=0 / status={status}）")
        check(len(v) <= 2,
              f"没轮到的片段没有被碰（库里只有 {len(v)} 条记录）")

        # ---------- 第二轮：接着跑，跳过已完成的 ----------
        before = {k: x[1] for k, x in v.items() if (x[1] or 0) > 0}
        post(f"/api/libraries/{lid}/scan", use_asr=True, with_gif=False,
             retry_empty=True)
        check(wait_until(lambda: latest_task().get("status") == "running",
                         60, 1.0, "第二轮任务开始跑"), "第二轮索引已启动")
        # 这一轮里"已完成的"会被 md5 秒跳过 → 等它把第一条跳完
        hit = wait_until(
            lambda: (latest_task().get("message") or "").startswith("1/")
            or (latest_task().get("done") or 0) >= 1, 120, 1.5, "第二轮跳过第 1 条")
        check(hit, "第二轮很快跨过已完成的素材")
        r2 = post("/api/scan/stop", root=ROOT)
        check(r2.get("ok") is True, "第二轮的停止接口同样可用")
        wait_until(lambda: latest_task().get("status") == "stopped", 60, 1.5,
                   "第二轮停止生效")
        t2 = latest_task()
        log(f"第二轮任务记录：status={t2.get('status')} done={t2.get('done')} "
            f"msg={t2.get('message')}")
        check("跳过" in (t2.get("message") or ""),
              f"第二轮摘要说明跳过了已完成的：{t2.get('message')}")

        v2 = videos()
        after = {k: x[1] for k, x in v2.items() if k in before}
        check(after == before,
              f"已完成的素材台词数没变（没重做）：{before} → {after}")

        # ---------- 停止接口的边界 ----------
        try:
            post("/api/scan/stop", root=ROOT)
            check(False, "没有任务在跑时停止应该报错")
        except urllib.error.HTTPError as e:
            check(e.code == 404, f"没任务在跑时停止返回 404：{e.code}")
    finally:
        # 把临时登记删掉，hub.db 恢复原样；测试目录清成空库（清不动也不影响结果）
        try:
            if reg and reg.get("id"):
                post(f"/api/libraries/{reg['id']}/delete")
                log("临时库登记已删除")
        except Exception as e:
            log(f"（清理登记失败：{e}）")
        _try_clean(ROOT)

    log("")
    log("== %d 项，%s ==" % (n, "全绿" if ok else "有失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
