# -*- coding: utf-8 -*-
"""卡通加强版：两段 H3 卡通视频（各 5 秒）+ 拼接成 10 秒。

相对 _cartoon_2seg.py 的改动（用户反馈"再卡通一点"）：
v1 虽然写了 flat cel shading，但画面仍偏写实动画风——原因有三：
  1) 写实向的道具词（dead laptop / stacked bills / peeling wall / crumpled
     clothes）会诱导模型去画材质细节；
  2) 摄影词（handheld camera / low-key lighting / long shadow / rim light）
     本身就是写实电影语言；
  3) 缺角色层面的简化指令，脸和手仍按写实结构生成。
v2 改法：
  - STYLE 前置并加厚，追加"非写实"显式否定词（模型能吃 not photorealistic
    这类否定，虽然 h3gen 没有独立 negative 参数，但写在正向里仍起作用）；
  - 删掉全部摄影/光比词，换成动画语言（flat colors / simple shapes）；
  - 角色设计简化（大眼、圆头、简单线条），这是卡通感的主要来源；
  - 道具简化为几何体（几摞纸、一台方电脑、一个方行李箱）。

用法：python _cartoon_2seg_v2.py   （配合 Bash 的 run_in_background）
"""
import json
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
LOG = os.path.join(BASE, "_out", "h3seg_v2_last.log")

# v2 风格块：显式否定写实 + 扁平色块 + 简化角色
STYLE = (
    "flat 2D cartoon, bold clean black outline, flat solid color fills, "
    "no shading gradients, no texture, simple rounded shapes, "
    "bright simple color palette, "
    "chibi character design with big round eyes and rounded face, "
    "childlike storybook cartoon, "
    "not photorealistic, not 3d render, not realistic"
)

SEGMENTS = [
    {
        "name": "出租屋惊醒捂胸",
        "prompt": (
            "[slow push in] " + STYLE + ". "
            "A young man with a big round head and simple cartoon face lies "
            "on a thin mattress on the floor and suddenly sits up in panic, "
            "both hands pressed on his chest, eyes wide, mouth open small. "
            "The room is drawn with simple shapes: one small square window "
            "showing a round moon, a plain desk, a few neat stacks of paper, "
            "a simple laptop, a few flat rectangles of clothes on the floor, "
            "a tiny refrigerator. Flat blue color palette, calm simple "
            "background shapes, lonely but gentle mood, no text, no watermark."),
    },
    {
        "name": "墙角蜷缩躲藏",
        "prompt": (
            "[slow pan right] " + STYLE + ". "
            "The same big-headed cartoon young man sits curled in the corner "
            "of the same simple room, knees pulled up, arms hugging his legs, "
            "head lowered resting on his arms. Drawn with simple shapes only: "
            "one round lamp with a soft yellow glow, a plain square wall, a "
            "simple boxy suitcase with a small handle beside him. Flat warm "
            "orange and brown color palette, soft and lonely but tender mood, "
            "no text, no watermark."),
    },
]

FINAL = os.path.join(OUT_DIR, "出租屋低谷_10秒_卡通版.mp4")


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_one(i, seg):
    dst = os.path.join(OUT_DIR, "出租屋低谷_v2_%d.mp4" % i)
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
    proc = subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE,
                            stderr=logf, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
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
            msg = json.loads(line)
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
    lst = os.path.join(BASE, "_out", "_concat_v2_%s.txt" % uuid.uuid4().hex[:8])
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
        log("释放显存后进入下一段")
        time.sleep(8)
    concat(parts, FINAL)
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