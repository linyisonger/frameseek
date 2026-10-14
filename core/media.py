# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - ffmpeg 相关：探测、md5、封面、动图、抽帧、切片"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from core import config as C

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run(cmd: list, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, timeout=timeout,
        creationflags=NO_WINDOW,
    )


def ffprobe_json(path: str) -> dict:
    try:
        p = run([C.FFPROBE, "-v", "error", "-show_format", "-show_streams",
                 "-of", "json", path], timeout=60)
        return json.loads(p.stdout.decode("utf-8", "ignore") or "{}")
    except Exception:
        return {}


def video_meta(path: str) -> dict:
    """返回 duration/width/height/sub_streams(字幕流索引列表)
    以及 fps/codec/bitrate/resolution（2026-10-15 加，给素材页筛选用）"""
    info = ffprobe_json(path)
    out = {"duration": 0.0, "width": 0, "height": 0, "sub_streams": [],
           "fps": 0.0, "vcodec": "", "acodec": "", "bitrate": 0}
    try:
        out["duration"] = float(info.get("format", {}).get("duration") or 0)
    except Exception:
        pass
    try:
        out["bitrate"] = int(float(info.get("format", {}).get("bit_rate") or 0))
    except Exception:
        pass
    for s in info.get("streams", []):
        ct = s.get("codec_type")
        if ct == "video" and not out["width"]:
            out["width"] = int(s.get("width") or 0)
            out["height"] = int(s.get("height") or 0)
            out["vcodec"] = str(s.get("codec_name") or "")
            if not out["duration"]:
                try:
                    out["duration"] = float(s.get("duration") or 0)
                except Exception:
                    pass
            # 帧率：优先 avg_frame_rate，退 r_frame_rate
            for key in ("avg_frame_rate", "r_frame_rate"):
                raw = str(s.get(key) or "")
                if "/" in raw:
                    a, _, b = raw.partition("/")
                    try:
                        a, b = float(a), float(b)
                        if b > 0:
                            out["fps"] = round(a / b, 3)
                            break
                    except Exception:
                        pass
        elif ct == "audio" and not out["acodec"]:
            out["acodec"] = str(s.get("codec_name") or "")
        elif ct == "subtitle":
            out["sub_streams"].append(int(s.get("index", -1)))
    return out


def file_md5(path: str) -> str:
    """大文件走采样（首/中/尾 + 大小），小文件全量，够防重复且快"""
    size = os.path.getsize(path)
    h = hashlib.md5()
    if size <= C.MD5_FULL_LIMIT:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    else:
        with open(path, "rb") as f:
            for pos in (0, size // 2, max(0, size - C.MD5_SAMPLE)):
                f.seek(pos)
                h.update(f.read(C.MD5_SAMPLE))
        h.update(str(size).encode())
    return h.hexdigest()


def make_cover(video: str, out: str, at: float = None, width: int = 480) -> bool:
    os.makedirs(os.path.dirname(out), exist_ok=True)
    meta = video_meta(video)
    t = at if at is not None else min(3.0, max(0.0, (meta["duration"] or 3) * 0.1))
    p = run([C.FFMPEG, "-y", "-ss", f"{t:.3f}", "-i", video,
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", out],
            timeout=120)
    return os.path.exists(out) and os.path.getsize(out) > 0


def make_gif(video: str, out: str, duration: float = None) -> bool:
    """视频动图预览：默认取 10% 处往后 4 秒"""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    meta = video_meta(video)
    dur = meta["duration"] or 10
    start = min(max(0.0, dur * 0.1), max(0.0, dur - 2))
    sec = C.GIF_SEC if duration is None else duration
    sec = min(sec, max(1.0, dur - start))
    p = run([C.FFMPEG, "-y", "-ss", f"{start:.3f}", "-t", f"{sec:.3f}", "-i", video,
             "-vf", f"fps={C.GIF_FPS},scale={C.GIF_WIDTH}:-2:flags=lanczos",
             "-loop", "0", out], timeout=300)
    return os.path.exists(out) and os.path.getsize(out) > 0


def grab_shot(video: str, t: float, out: str, width: int = None) -> bool:
    """按时间点抓一帧（台词截图）。-ss 放 -i 前是解码前 seek，快但不精确到帧；
    放后面精确但慢。这里放前面，误差 <0.5s，剪辑场景够用"""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    w = width or C.SHOT_WIDTH
    p = run([C.FFMPEG, "-y", "-ss", f"{max(0.0, t):.3f}", "-i", video,
             "-frames:v", "1", "-vf", f"scale={w}:-2",
             "-q:v", str(C.SHOT_QUALITY), out], timeout=120)
    return os.path.exists(out) and os.path.getsize(out) > 0


def write_srt(video: str, rows: list) -> str:
    """在视频同目录写同名 .srt（视频名.srt）。

    用户可以直接用记事本/剪映改，改完在页面点"重新导入字幕"就能刷新数据库
    和台词截图，不用重新转写一遍。
    """
    base = os.path.splitext(video)[0]
    out_path = base + ".srt"

    def ts(x):
        ms = int(round(max(0.0, float(x or 0)) * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for r in rows or []:
        txt = str(r.get("text") or "").strip()
        if not txt:
            continue
        st = float(r.get("start") or 0)
        en = float(r.get("end") or 0)
        if en <= st:
            en = st + 1.5
        blocks.append(f"{len(blocks) + 1}\n{ts(st)} --> {ts(en)}\n{txt}\n")
    if not blocks:
        return ""
    # utf-8-sig：记事本/剪映打开不乱码
    with open(out_path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(blocks))
    return out_path


def srt_sidecar(video: str):
    """同名外挂字幕文件路径（没有就返回 ''）"""
    base = os.path.splitext(video)[0]
    for ext in C.SUB_EXT:
        p = base + ext
        if os.path.exists(p):
            return p
    return ""


def stream_rate(path: str):
    """(fps, sec_per_pts)。fps 用来算 select 容差，sec_per_pts 用来把 -frame_pts
    输出的整数 pts 换回秒"""
    info = ffprobe_json(path)
    fps = 0.0
    spt = 0.0
    for s in info.get("streams", []):
        if s.get("codec_type") != "video":
            continue
        try:
            a, b = str(s.get("r_frame_rate", "0/0")).split("/")
            fps = float(a) / float(b)
        except Exception:
            pass
        try:
            a, b = str(s.get("time_base", "0/0")).split("/")
            spt = float(a) / float(b)
        except Exception:
            pass
        break
    return (fps if fps > 0 else 24.0), (spt if spt > 0 else 0.001)


def grab_shots_batch(video: str, times: list, outs: list, width: int = None,
                     workers: int = None, hwaccel: bool = None,
                     on_seg=None) -> list:
    """批量抽帧：一次 ffmpeg 顺序解一段视频，段内用 select 一次命中该段所有台词
    时间点，而不是每张图都 seek 一次。

    实测（92 分钟 / 2309 条台词）：逐帧 seek 8 并发 6.0 分钟，批量分段 1.4 分钟，
    加快解 1.15 分钟——快 4~5 倍。原因是每张图单独 seek 都要从关键帧重新解一遍，
    批量只把整片解一遍。

    select 的容差不能卡太死：区间宽 < 1 帧会漏（实测漏 17%），> 1 帧会重复命中
    （VFR / 重复帧）。所以这里故意放宽，再用 -frame_pts 输出的 pts 反算时间，
    给每条台词挑最近的一张，多余的丢掉。任一段挂了就自动回落到 grab_shot。
    """
    n = min(len(times), len(outs))
    res = [False] * n
    if n == 0:
        return res
    w = width or C.SHOT_WIDTH
    wk = max(1, int(workers or getattr(C, "SHOT_WORKERS", 8) or 8))
    if hwaccel is None:
        hwaccel = bool(getattr(C, "SHOT_HWACCEL", True))

    def fallback(idxs):
        with ThreadPoolExecutor(max_workers=min(wk, max(1, len(idxs)))) as ex:
            for i, ok in zip(idxs, ex.map(
                    lambda i: grab_shot(video, times[i], outs[i], w), idxs)):
                res[i] = ok

    if n < 3 or wk < 2:
        fallback(list(range(n)))
        return res

    order = sorted(range(n), key=lambda i: times[i])
    try:
        fps, spt = stream_rate(video)
    except Exception:
        fps, spt = 24.0, 0.001
    tol = 0.6 / fps                 # 区间宽 1.2 帧 -> 保证至少命中一张
    wnd = max(0.08, 1.5 / fps)      # 匹配窗口

    # 按“时间上最大的空隙”切段：没台词的长段（片头曲、空镜）直接跳过不解码
    k = min(wk, n)
    cuts = []
    if k > 1:
        cuts = sorted(sorted(range(1, n),
                             key=lambda j: times[order[j]] - times[order[j - 1]],
                             reverse=True)[:k - 1])
    bounds = [0] + cuts + [n]

    tmp = os.path.join(os.path.dirname(os.path.abspath(outs[order[0]])), "_fstmp")
    os.makedirs(tmp, exist_ok=True)

    def one_seg(kk):
        idxs = order[bounds[kk]:bounds[kk + 1]]
        if not idxs:
            return []
        ss = max(0.0, times[idxs[0]] - 2.0)
        to = times[idxs[-1]] + 2.0
        expr = "+".join(
            f"between(t,{times[i] - ss - tol:.4f},{times[i] - ss + tol:.4f})"
            for i in idxs)
        fs = os.path.join(tmp, f"fg{kk}.txt")
        with open(fs, "w", encoding="utf-8") as f:
            f.write(f"select='{expr}',scale={w}:-2")
        cand = []
        for use_cuda in ([True, False] if hwaccel else [False]):
            cmd = [C.FFMPEG, "-y"]
            if use_cuda:
                cmd += ["-hwaccel", "cuda"]
            cmd += ["-ss", f"{ss:.3f}", "-to", f"{to:.3f}", "-i", video,
                    "-filter_script", fs, "-vsync", "0", "-frame_pts", "1",
                    "-q:v", str(C.SHOT_QUALITY),
                    os.path.join(tmp, f"{kk}_%d.jpg")]
            try:
                run(cmd, timeout=1800)
            except Exception:
                pass
            for f in os.listdir(tmp):
                if f.startswith(f"{kk}_") and f.endswith(".jpg"):
                    try:
                        pts = int(f[len(f"{kk}_"):-4])
                    except Exception:
                        continue
                    cand.append((ss + pts * spt, os.path.join(tmp, f)))
            if cand:
                break
        if not cand:
            return idxs                      # 这段交给回落
        cand.sort()
        missed = []
        ci = 0
        for i in idxs:
            t = times[i]
            while ci < len(cand) and cand[ci][0] < t - wnd:
                ci += 1
            best, bd = -1, None
            j = ci
            while j < len(cand) and cand[j][0] <= t + wnd:
                if cand[j][1] is not None:
                    d = abs(cand[j][0] - t)
                    if bd is None or d < bd:
                        bd, best = d, j
                j += 1
            if best < 0:
                missed.append(i)
                continue
            try:
                os.makedirs(os.path.dirname(outs[i]), exist_ok=True)
                os.replace(cand[best][1], outs[i])
                cand[best] = (cand[best][0], None)
                res[i] = True
            except Exception:
                missed.append(i)
        return missed

    try:
        missed_all = []
        nseg = len(bounds) - 1
        n_done = 0
        # 分段并行时进度只能按段报（段内是一次 ffmpeg 调用，拿不到中间态）。
        # 段数 = 并发数，一般 4~8，所以每完成一段跳一截——比全程黑着强。
        with ThreadPoolExecutor(max_workers=min(wk, nseg)) as ex:
            for kk, m in enumerate(ex.map(one_seg, range(nseg))):
                missed_all.extend(m or [])
                n_done += bounds[kk + 1] - bounds[kk]
                if on_seg:
                    try:
                        on_seg(min(n_done, n), n)
                    except Exception:
                        pass
        if missed_all:
            fallback(sorted(missed_all))
        if on_seg:
            try:
                on_seg(n, n)
            except Exception:
                pass
    except Exception:
        fallback([i for i in range(n) if not res[i]])
    finally:
        # 临时帧目录的清理**绝不能卡住索引**。
        # 踩过：rmtree 一被拦（批量删除保护 / 慢盘 / 文件被占用），整个截图阶段
        # 就永远停在 100%，台词也别想落库。挪到后台线程：删得掉最好，
        # 删不掉就留着（下次会被同名覆盖），索引照常往下走。
        threading.Thread(target=_rmtree_bg, args=(tmp,), daemon=True).start()
    return res


def _rmtree_bg(path: str):
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def export_clip(video: str, start: float, end: float, out: str,
                reencode: bool = True) -> bool:
    """导出片段。默认重编码（帧精确），fast 模式用 -c copy（秒级但只能按关键帧切）"""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    dur = max(0.2, end - start)
    if not reencode:
        p = run([C.FFMPEG, "-y", "-ss", f"{max(0.0,start):.3f}", "-t", f"{dur:.3f}",
                 "-i", video, "-c", "copy", "-avoid_negative_ts", "make_zero", out],
                timeout=300)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return True
    p = run([C.FFMPEG, "-y", "-ss", f"{max(0.0,start):.3f}", "-t", f"{dur:.3f}", "-i", video,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out],
            timeout=900)
    return os.path.exists(out) and os.path.getsize(out) > 0


def extract_audio(video: str, out_wav: str) -> bool:
    os.makedirs(os.path.dirname(out_wav), exist_ok=True)
    p = run([C.FFMPEG, "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", out_wav], timeout=900)
    return os.path.exists(out_wav) and os.path.getsize(out_wav) > 0


def extract_subtitle(video: str, stream_index: int, out_srt: str) -> bool:
    os.makedirs(os.path.dirname(out_srt), exist_ok=True)
    p = run([C.FFMPEG, "-y", "-i", video, "-map", f"0:{stream_index}",
             "-c:s", "srt", out_srt], timeout=180)
    return os.path.exists(out_srt) and os.path.getsize(out_srt) > 0
