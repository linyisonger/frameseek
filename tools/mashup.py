# -*- coding: utf-8 -*-
"""拾帧 FrameSeek · 原声混剪（不配音，烧原台词字幕）

给「名场面混剪」用：按 line_id 或显式时间窗切原片，保留原声，
把所有对白按成片时间轴写成 SRT 并烧进画面；中间可以插标题卡/章节卡。

用法：
    python tools/mashup.py --spec tools/mashup_specs/liya_mashup.json
    python tools/mashup.py --spec ... --vertical

spec 示例见 tools/mashup_specs/liya_mashup.json。字段：
    out        产物目录（相对项目根）
    size       [宽, 高]，默认 1280x720
    fps        默认 30
    crop_w     竖屏化时从 16:9 里裁出的内容宽度（4:3 素材填 960）
    segments   card: {type,title,sub,note,dur}
               clip: {type,root,line_id,pre,post}
                     或 {type,root,src_in,dur}
"""
import argparse
import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import config as C  # noqa: E402
from core import media  # noqa: E402
from core import indexer as IX  # noqa: E402

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"


def run(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=NO_WINDOW)


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- 卡片
def _font(path, size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.truetype(FONT_REG, size)


def make_card(path, title, sub="", note="", w=1280, h=720, small=False, bg=""):
    from PIL import Image, ImageDraw
    if bg and os.path.exists(bg):
        base = Image.open(bg).convert("RGB").resize((w, h), Image.LANCZOS)
        overlay = Image.new("RGBA", (w, h), (8, 10, 16, 176))
        img = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    else:
        img = Image.new("RGB", (w, h), (14, 16, 22))
    d = ImageDraw.Draw(img)
    if not (bg and os.path.exists(bg)):
        # 极简竖向渐变，比纯黑有层次
        for y in range(h):
            t = y / max(1, h - 1)
            d.line([(0, y), (w, y)],
                   fill=(int(13 + 7 * (1 - t)), int(15 + 9 * (1 - t)),
                         int(21 + 18 * (1 - t))))
    # 左侧竖条 + 顶部标识
    d.rectangle([70, 96, 74, h - 150], fill=(64, 128, 255))
    f_note = _font(FONT_REG, 26)
    d.text((96, 92), "《潜伏》 · 名场面", font=f_note, fill=(138, 146, 164))

    def center(y, text, size, fill, bold=True, max_w=None):
        # 自动缩字号：长标题（如结尾那句）不缩会顶到画幅两边被裁掉
        max_w = max_w or (w - 180)
        f = _font(FONT_BOLD if bold else FONT_REG, size)
        box = d.textbbox((0, 0), text, font=f)
        while box[2] - box[0] > max_w and size > 22:
            size -= 4
            f = _font(FONT_BOLD if bold else FONT_REG, size)
            box = d.textbbox((0, 0), text, font=f)
        d.text(((w - (box[2] - box[0])) / 2, y), text, font=f, fill=fill)

    if small:
        center(h * 0.33, title, 66, (242, 243, 245))
        if sub:
            center(h * 0.50, sub, 32, (255, 209, 102), bold=False)
    else:
        center(h * 0.24, title, 112, (255, 255, 255))
        if sub:
            center(h * 0.48, sub, 52, (255, 209, 102))
    if note:
        center(h * 0.66, note, 26, (138, 146, 164), bold=False)
    img.save(path, quality=95)


def card_video(png, dst, dur, w, h, fps):
    cmd = [C.FFMPEG, "-y", "-nostdin", "-v", "error",
           "-loop", "1", "-i", png,
           "-f", "lavfi", "-t", f"{dur:.3f}",
           "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
           "-t", f"{dur:.3f}", "-r", str(fps),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2",
           "-shortest", dst]
    p = run(cmd)
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        raise RuntimeError("卡片生成失败：" + (p.stderr or b"")[-400:].decode("utf-8", "ignore"))


# ---------------------------------------------------------------- 片段
def clip_video(src, src_in, dur, dst, w, h, fps):
    meta = media.video_meta(src)
    has_audio = bool(meta.get("acodec"))
    cmd = [C.FFMPEG, "-y", "-nostdin", "-v", "error",
           "-ss", f"{src_in:.3f}", "-t", f"{dur:.3f}", "-i", src]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}")
    cmd += ["-map", "0:v:0"]
    cmd += ["-map", "0:a:0"] if has_audio else ["-map", "1:a:0"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-b:a", "128k", "-movflags", "+faststart", dst]
    p = run(cmd)
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        tail = (p.stderr or b"")[-500:].decode("utf-8", "ignore")
        raise RuntimeError(f"切片段失败 {os.path.basename(src)} @{src_in:.1f}: {tail}")


def concat(paths, dst, tmp):
    lst = os.path.join(tmp, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '" + p.replace("\\", "/") + "'\n")
    p = run([C.FFMPEG, "-y", "-nostdin", "-v", "error",
             "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
             "-movflags", "+faststart", dst])
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        raise RuntimeError("拼接失败：" + (p.stderr or b"")[-500:].decode("utf-8", "ignore"))


def burn(src, srt, dst):
    p = run([C.FFMPEG, "-y", "-nostdin", "-v", "error", "-i", src,
             "-vf", media._sub_vf(srt), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "19", "-pix_fmt", "yuv420p",
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-movflags", "+faststart", dst])
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        raise RuntimeError("烧字幕失败：" + (p.stderr or b"")[-500:].decode("utf-8", "ignore"))


def normalize_audio(src, dst):
    p = run([C.FFMPEG, "-y", "-nostdin", "-v", "error", "-i", src,
             "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-movflags", "+faststart", dst])
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        raise RuntimeError("音频归一失败：" + (p.stderr or b"")[-500:].decode("utf-8", "ignore"))


def verticalize(src, dst, crop_w, w=1080, h=1920):
    """16:9 成片 → 9:16：中间裁出 4:3 内容，上下用模糊背景填。"""
    cw = int(crop_w)
    ch = 720
    x = max(0, (1280 - cw) // 2)
    fc = (f"[0:v]crop={cw}:{ch}:{x}:0,split=2[f1][f2];"
          f"[f1]scale={w}:{int(w * ch / cw)}[fg];"
          f"[f2]scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},boxblur=24:4,eq=brightness=-0.10[bg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    p = run([C.FFMPEG, "-y", "-nostdin", "-v", "error", "-i", src,
             "-filter_complex", fc, "-map", "0:a?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "copy",
             "-movflags", "+faststart", dst])
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        raise RuntimeError("竖屏化失败：" + (p.stderr or b"")[-500:].decode("utf-8", "ignore"))


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="FrameSeek 原声混剪")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--vertical", action="store_true", help="额外输出 9:16 竖版")
    args = ap.parse_args()

    with open(args.spec, "r", encoding="utf-8-sig") as f:
        spec = json.load(f)
    w, h = spec.get("size") or [1280, 720]
    fps = int(spec.get("fps") or 30)
    out = os.path.abspath(os.path.join(BASE, spec["out"]))
    tmp = os.path.join(out, "_tmp")
    os.makedirs(out, exist_ok=True)
    os.makedirs(tmp, exist_ok=True)

    segs, subs, t0, shards = [], [], 0.0, []
    for i, seg in enumerate(spec.get("segments") or []):
        typ = seg.get("type") or "clip"
        dst = os.path.join(tmp, f"seg{i:02d}.mp4")
        if typ == "card":
            png = os.path.join(tmp, f"card{i:02d}.png")
            make_card(png, seg.get("title") or "", seg.get("sub") or "",
                      seg.get("note") or "", w, h, small=bool(seg.get("small")),
                      bg=seg.get("bg") or "")
            dur = float(seg.get("dur") or 2.0)
            card_video(png, dst, dur, w, h, fps)
            segs.append({"type": "card", "dur": dur, "title": seg.get("title"),
                         "sub": seg.get("sub")})
            shards.append(dst)
            t0 += dur
            log(f"卡片 {i + 1}: {seg.get('title') or ''} ({dur}s)")
            continue

        root = os.path.abspath(seg.get("root") or spec.get("root") or "")
        if not root:
            raise SystemExit("clip 缺 root")
        st = IX.open_store(root)
        if seg.get("line_id"):
            ln = st.get_line(int(seg["line_id"]))
            if not ln:
                raise SystemExit(f"line_id {seg['line_id']} 不存在")
            v = st.get_video(ln["video_id"])
            pre = float(seg.get("pre") or 0.5)
            post = float(seg.get("post") or 1.8)
            src_in = max(0.0, float(ln["start"]) - pre)
            src_out = float(ln.get("end") or ln["start"]) + post
            vid = ln["video_id"]
        else:
            if not seg.get("video_id"):
                raise SystemExit("clip 需要 line_id 或 video_id")
            v = st.get_video(int(seg.get("video_id")))
            if not v:
                raise SystemExit(f"video_id {seg.get('video_id')} 不存在")
            src_in = float(seg.get("src_in") or 0)
            dur0 = float(seg.get("dur") or 2)
            src_out = src_in + dur0
            vid = v["id"]
        src = os.path.abspath(os.path.join(root, v["rel"]))
        dur = max(0.4, src_out - src_in)
        clip_video(src, src_in, dur, dst, w, h, fps)
        shards.append(dst)

        for ln2 in st.lines_of(vid):
            a = float(ln2.get("start") or 0)
            b = float(ln2.get("end") or (a + 1.5))
            if b <= src_in or a >= src_out:
                continue
            a = max(a, src_in)
            b = min(b, src_out)
            if b - a < 0.5:      # 只露半秒的残句不要，字幕会闪一下很脏
                continue
            subs.append({"text": ln2.get("text") or "",
                         "start": round(t0 + (a - src_in), 3),
                         "end": round(t0 + (b - src_in), 3)})
        segs.append({"type": "clip", "video": v["name"], "src_in": src_in,
                     "dur": dur, "line_id": seg.get("line_id")})
        t0 += dur
        log(f"片段 {i + 1}: {os.path.basename(src)[:30]} @{src_in:.1f} "
            f"({dur:.1f}s)")

    concat(shards, os.path.join(tmp, "concat.mp4"), tmp)
    srt = ""
    if subs:
        subs.sort(key=lambda x: x["start"])
        srt = media.write_srt_at(os.path.join(out, "subtitles.srt"), subs)
    final = os.path.join(out, "final.mp4")
    if srt:
        burn(os.path.join(tmp, "concat.mp4"), srt, final)
    else:
        normalize_audio(os.path.join(tmp, "concat.mp4"), final)

    result = {"ok": True, "video": final, "srt": srt,
              "duration": round(t0, 2), "segments": len(segs),
              "storyboard": segs}
    if args.vertical:
        vpath = os.path.join(out, "final_9x16.mp4")
        verticalize(final, vpath, spec.get("crop_w") or 960)
        result["vertical"] = vpath

    with open(os.path.join(out, "storyboard.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 封面：混剪第一帧/标题卡之后一帧都行，这里取 1s 处
    try:
        media.grab_shot(final, 1.0, os.path.join(out, "cover.jpg"), width=480)
    except Exception:
        pass

    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    log(json.dumps({k: result[k] for k in ("video", "duration", "segments")},
                   ensure_ascii=False))


if __name__ == "__main__":
    main()
