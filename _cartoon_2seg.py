# -*- coding: utf-8 -*-
"""两段H3 卡通视频（各5 秒）+ 自动拼接成 10 秒成片。

为什么不用 MCP 的 frameseek_generate_video：
  它把生成挂在 MCP server 进程的后台线程里，MCP 一重启（换会话/重连）
  子进程就被回收，任务卡在 running，实际没产出。这里直接串行调 h3gen.py，
  用 run_in_background 起，跑多久都不怕。

段1：深夜出租屋，惊醒 + 双手捂胸
段 2：偶尔躲墙角，蜷缩抱膝
"""
import os
import subprocess
import sys
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
H3GEN = os.path.join(BASE, "h3gen.py")
COMFY_ROOT = os.environ.get(
    "FRAMESEEK_COMFY_ROOT", r"D:\project\AIProjects\ComfyUI_windows_portable")
COMFY_APP_ROOT = os.path.join(COMFY_ROOT, "ComfyUI")
COMFY_PY = os.path.join(COMFY_ROOT, "python_embeded", "python.exe")
OUT_DIR = os.path.join(BASE, "AI生成")
LOG = os.path.join(BASE, "_out", "h3seg_last.log")

# 卡通风格写进每段：2D 动画、赛璐璐上色、线稿清晰，避免出成写实照片
STYLE = ("2D cartoon animation style, clean line art, flat cel shading, "
         "muted colors, soft rim light, anime-inspired")

SEGMENTS = [
    {
        "name": "出租屋惊醒捂胸",
        "prompt": (
            "[slow push in] " + STYLE + ". Late night inside a cramped, "
            "messy rental apartment. A young man in a wrinkled t-shirt "
            "jolts awake on the floor mattress, gasping, both hands "
            "clutching his chest, eyes wide with panic, shoulders hunched. "
            "Cold blue moonlight through a small window falls across his "
            "face; a cluttered desk with stacked bills and a dead laptop "
            "behind him, crumpled clothes on the floor. Handheld camera "
            "with slight tremble, dim low-key lighting, lonely anxious "
            "atmosphere, no text, no watermark."),
    },
    {
        "name": "墙角蜷缩躲藏",
        "prompt": (
            "[slow pan right] " + STYLE + ". The same young man curled up "
            "tight in the corner of the same dark small rental apartment, "
            "knees pulled to his chest, arms wrapped around his legs, head "
            "buried and lowered, hiding from the world. A single dim warm "
            "desk lamp casts a long shadow on the bare peeling wall; a "
            "half-packed suitcase sits beside him. Slow pull back to a "
            "wider frame, quiet lonely vulnerable atmosphere, dim low-key "
            "lighting, no text, no watermark."),
    },
]

FINAL = os.path.join(OUT_DIR, "出租屋低谷_10秒.mp4")


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_one(i, seg):
    dst = os.path.join(OUT_DIR, "出租屋低谷_%d.mp4" % i)
    seed = uuid.uuid4().int % (2 ** 31)
    cmd = [COMFY_PY, "-u", H3GEN,
           "--prompt", seg["prompt"],
           "--duration", "5.0",
           "--aspect", "16:9",
           "--megapixels", "0.3",
           "--steps", "12",
           "--seed", str(seed),
           "--out", dst,
           "--ffmpeg", "ffmpeg",
           "--comfy-root", COMFY_APP_ROOT]
    log("段%d 开始：%s  seed=%d" % (i, seg["name"], seed))
    t0 = time.time()
    logf = open(LOG, "a", encoding="utf-8", errors="replace")
    # stdout 走 PIPE 逐行读进度，stderr 落盘（aimdo 的 CUDA 日志量极大）
    proc = subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE,
                            stderr=logf, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    import json as _json
    while True:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = _json.loads(line)
        except Exception:
            continue
        t = msg.get("t")
        if t == "phase":
            log("  段%d %6.0fs  %s" % (i, time.time() - t0, msg.get("s")))
        elif t == "pct":
            log("  段%d %6.0fs  %5.1f%%  %s"
                % (i, time.time() - t0, msg.get("v", 0), msg.get("s")))
        elif t == "error":
            log("  段%d 报错：%s" % (i, msg.get("err")))
        elif t == "done":
            log("  段%d 产出 %s（%ss）"
                % (i, msg.get("path"), msg.get("duration")))
    logf.close()
    rc = proc.wait()
    if rc != 0 or not os.path.isfile(dst):
        raise RuntimeError("段%d 生成失败（rc=%s），详见 %s" % (i, rc, LOG))
    log("段%d 完成，用时 %.0fs" % (i, time.time() - t0))
    return dst


def concat(parts, out):
    """concat 滤镜拼接：两段参数一致，音视频一起接。"""
    lst = os.path.join(BASE, "_out", "_concat_%s.txt" % uuid.uuid4().hex[:8])
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/"))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", lst,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
           "-preset", "medium", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", out]
    p = subprocess.run(cmd, capture_output=True)
    try:
        os.remove(lst)
    except OSError:
        pass
    if p.returncode != 0 or not os.path.isfile(out):
        raise RuntimeError("拼接失败：" +
                           p.stderr.decode("utf-8", "replace")[-500:])
    log("拼接完成 -> %s" % out)


def main():
    if not os.path.isfile(COMFY_PY):
        raise SystemExit("找不到便携版 python：" + COMFY_PY)
    os.makedirs(OUT_DIR, exist_ok=True)
    parts = []
    for i, seg in enumerate(SEGMENTS, 1):
        parts.append(run_one(i, seg))
        # 两段之间彻底放显存，否则第二个进程容易 OOM
        log("释放显存后进入下一段")
        time.sleep(8)
    concat(parts, FINAL)
    # 探针时长
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", FINAL], capture_output=True, text=True)
    log("成片时长：%s 秒" % (p.stdout.strip() or "?"))
    log("ALLDONE %s" % FINAL)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log("失败：%s: %s" % (type(e).__name__, e))
        sys.exit(1)