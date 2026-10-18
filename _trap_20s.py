# -*- coding: utf-8 -*-
"""明知是陷阱却不得不踏入：20 秒卡通片段（4 段 × 5 秒拼接）。

需求要点：
  - 主题：明知前方是陷阱，但又不得不踏入的悲惨经历
  - 卡通版本（沿用用户认可的 v2 强卡通风格块）
  - 不需要语音 —— H3 是视频音频一体模型，每段自带音轨，拼接时统一 -an 去掉

为什么拆 4 段：h3gen 单段上限约 15 秒（帧数 124~362），5 秒一段画质/速度/
显存都最稳（4060Ti 8G 实测每段约 190 秒），4 段串行共约 13 分钟。

分镜（叙事弧 = 犹豫 → 咬牙踏入 → 坠落困局 → 一线微光）：
  段1 站在陷阱边，手里的欠条/空钱包，明知有坑
  段2 闭眼咬牙，一步踏入，身体下坠
  段3 坑底抱膝，高墙环绕，孤独
  段4 抬头看一线光，没放弃但绝望
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
LOG = os.path.join(BASE, "_out", "h3trap_last.log")

# 强卡通风格块（用户上次反馈"再卡通一点"后定的版本）：
# 扁平色块 + 粗描边 + 无渐变无材质 + chibi 大头大眼 + 显式否定写实
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
        "name": "站在陷阱边犹豫",
        "prompt": (
            "[slow push in] " + STYLE + ". "
            "A chibi young man stands at the very edge of a round dark pit, "
            "a simple warning sign with a triangle mark next to it, he looks "
            "down at a torn paper and an empty wallet in his hands, "
            "shoulders slumped, eyes downcast, shoulders slumped, "
            "no way back but must go forward, "
            "flat blue and grey color palette, simple background shapes, "
            "sad helpless mood, no text, no watermark."),
    },
    {
        "name": "咬牙踏入",
        "prompt": (
            "[slow pan right] " + STYLE + ". "
            "The same chibi young man closes his eyes tight, clenches both "
            "fists, takes a deep breath, then steps forward into the dark pit, "
            "one leg lifted mid-step, body starting to fall, arms slightly "
            "open, facing forward with determination, "
            "flat dark blue and purple palette, simple shapes, "
            "brave but sorrowful mood, no text, no watermark."),
    },
    {
        "name": "坑底抱膝",
        "prompt": (
            "[slow zoom in] " + STYLE + ". "
            "The same chibi young man sits alone at the bottom of a deep "
            "smooth-walled pit, hugging his knees, head lowered and hidden "
            "in his arms, tall plain walls around him, a tiny round light "
            "far above like a small moon, flat dark blue and purple palette, "
            "very lonely, no text, no watermark."),
    },
    {
        "name": "抬头见一线光",
        "prompt": (
            "[slow pull back] " + STYLE + ". "
            "The same chibi young man still sitting at the bottom of the pit, "
            "lifts his head to look up at a thin beam of light from far "
            "above, his big round eyes reflect the light, mouth in a small "
            "brave line, one hand on his chest, "
            "flat warm yellow light against cool blue walls, "
            "desperate but still holding on, no text, no watermark."),
    },
]

FINAL = os.path.join(OUT_DIR, "明知陷阱仍踏入_20秒_卡通.mp4")


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_one(i, seg):
    dst = os.path.join(OUT_DIR, "明知陷阱_v1_%d.mp4" % i)
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
    """拼接并去音轨（用户明确不需要语音）。"""
    lst = os.path.join(BASE, "_out", "_concat_trap_%s.txt" % uuid.uuid4().hex[:8])
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/"))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", lst,
           "-an",                                     # 去音轨
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
           "-preset", "medium", "-movflags", "+faststart", out]
    p = subprocess.run(cmd, capture_output=True)
    try:
        os.remove(lst)
    except OSError:
        pass
    if p.returncode != 0 or not os.path.isfile(out):
        raise RuntimeError("拼接失败：" +
                           p.stderr.decode("utf-8", "replace")[-500:])
    log("拼接完成（无音轨）-> %s" % out)


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
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration:stream=codec_type",
         "-of", "csv=p=0", FINAL], capture_output=True, text=True)
    log("成片信息：%s" % (p.stdout.strip().replace("\n", " / ") or "?"))
    log("ALLDONE %s" % FINAL)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log("失败：%s: %s" % (type(e).__name__, e))
        sys.exit(1)