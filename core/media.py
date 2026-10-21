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
from typing import Callable

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


def _srt_blocks(rows: list, t0: float = 0.0, dur: float = None) -> list:
    """把台词行转成 SRT 文本块。t0 为时间平移量（导出片段烧字幕时＝片段起点，
    库内绝对时间要平移成片段相对时间）；dur 给定时把超出片段末尾的部分裁掉"""
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
        st_ = float(r.get("start") or 0) - t0
        en_ = float(r.get("end") or 0) - t0
        if en_ <= st_:
            en_ = st_ + 1.5
        if dur is not None:
            if st_ >= dur:
                continue
            en_ = min(en_, dur)
        if st_ < 0:
            st_ = 0.0
        blocks.append(f"{len(blocks) + 1}\n{ts(st_)} --> {ts(en_)}\n{txt}\n")
    return blocks


def write_srt_at(out_path: str, rows: list, t0: float = 0.0,
                 dur: float = None) -> str:
    """把台词行平移 t0（可裁剪到 dur 内）写成任意路径的 SRT，返回路径，空行返回 ''"""
    blocks = _srt_blocks(rows, t0=t0, dur=dur)
    if not blocks:
        return ""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    # utf-8-sig：记事本/剪映/libass 打开都不乱码
    with open(out_path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(blocks))
    return out_path


def write_srt(video: str, rows: list) -> str:
    """在视频同目录写同名 .srt（视频名.srt）。

    用户可以直接用记事本/剪映改，改完在页面点"重新导入字幕"就能刷新数据库
    和台词截图，不用重新转写一遍。
    """
    base = os.path.splitext(video)[0]
    out_path = base + ".srt"
    blocks = _srt_blocks(rows)
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


def proxy_dir(cd: str) -> str:
    """1080p 代理目录（放在 .frameseek/proxy/ 下）。

    放缓存目录里的关键原因：indexer.list_video_files 会整体排除 CACHE_DIR，
    所以代理既不会被当素材扫进库，也不会被"重新索引"当成新片子。
    """
    d = os.path.join(cd, getattr(C, "PROXY_DIR", "proxy"))
    os.makedirs(d, exist_ok=True)
    return d


def _proxy_path(cd: str, video: str, meta: dict) -> str:
    """代理文件名：原片 stem + 宽度标记 + 原片 mtime 前缀，避免同名不同片互相覆盖。"""
    stem = os.path.splitext(os.path.basename(video))[0]
    stem = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in stem)[:40]
    try:
        mt = int(os.stat(video).st_mtime)
    except OSError:
        mt = 0
    return os.path.join(proxy_dir(cd), f"{stem}_{meta.get('width') or 0}p_{mt}.mp4")


def _proxy_is_valid(proxy: str, src_duration: float) -> bool:
    """代理是否可用：存在 + 能解码 + 时长对得上 + **体积没失控**。

    时长差超过 PROXY_DUR_TOL 判为过期（片源换了、转码被中断、mtime 变了），
    让调用方重建——宁可重转一次，也不能拿一份时长对不上的代理去抽帧，
    否则末尾台词全抽不到图（抽帧时间点落在代理时长之外）。

    体积上限（PROXY_MAX_MB）是**第二道保险**：码率失控时 ffmpeg 不会报错，
    只会安静地产出一个几GB 的文件（实测踩过：`-b:v 0` + VBR 把 43 分钟片压成
    2.9GB，随后解码峰值把进程压到段错误）。这里直接按字节数判死并重建。
    """
    if not proxy or not os.path.exists(proxy) or os.path.getsize(proxy) <= 0:
        return False
    max_bytes = int(float(getattr(C, "PROXY_MAX_MB", 0)) * 1024 * 1024)
    if max_bytes > 0 and os.path.getsize(proxy) > max_bytes:
        return False
    try:
        pm = video_meta(proxy)
    except Exception:
        return False
    pd = pm.get("duration") or 0.0
    sd = src_duration or 0.0
    if pd <= 0:
        return False
    if sd > 0 and abs(pd - sd) > float(getattr(C, "PROXY_DUR_TOL", 0.5)):
        return False
    return True


def _wrap_prog(cb):
    """把 ensure_proxy 的 cb 包成 on_prog(pct) 能安全调用的形式。

    没有 cb 就返回 None（_encode 会走简化路径）。有 cb 时额外保证：① 异常不外泄
    （进度回调绝不能因为前端断开之类的原因打断转码）；② 首帧先报0，让用户立刻
    看到"开始转码"而不是空白等待。
    """
    if not cb:
        return None

    def _on(pct):
        try:
            if pct == 0:
                cb("代理转码 0%")
            else:
                cb(f"代理转码 {pct}%")
        except Exception:
            pass

    return _on


def ensure_proxy(video: str, cd: str, meta: dict = None,
                 cb: Callable[[str], None] = None) -> str:
    """高分辨率片先转一份 1080p 代理，返回**抽帧/OCR 该读的路径**。

    返回值可能还是原片——这是正常且常见的：代理关闭、片子本来就是 1080p
    及以下、或者建代理失败（磁盘满/编码器不可用），都回落读原片。

    为什么值得做（实测依据）：
      · ffmpeg 解码器吐原生分辨率帧，scale 在解码**之后**，滤镜里压不住。
      · OCR 流式管道是全帧解码（-i → -vf crop,scale → rawvideo），Python
        再按 OCR_STEP 网格丢帧 → 4K 片 80% 的帧"解完就扔"。
      · 电视剧库 28 部全是 3840×2880，像素是 1080p 的 5.3 倍。
    而画质基本无损：OCR 送检带宽固定 OCR_WIDTH、台词截图固定 SHOT_WIDTH，
    4K 和 1080p 缩完尺寸完全相同（只是多一次重采样），文本识别不受影响。
    所以只赚解码时间。

    触发只看分辨率：像素数 > PROXY_TRIGGER_PIXELS 才建，1080p 直接跳过。

    性能（实测 3136×2160 HEVC Main10，180 秒片段，4060Ti + 16 核）：
      CPU 解码 + lanczos + nvenc ......... 34.0s（x5.3 实时）
      NVDEC + scale_cuda + nvenc .........  7.2s（x24.9 实时）← 当前默认
    也就是说 45 分钟整集从 8.5 分钟降到 1.8 分钟。慢的那部分是 **CPU 软解码**
    （换 bicubic 只快 6%，证明缩放不是瓶颈），把解码搬上 NVDEC 才是正解。
    """
    if not getattr(C, "PROXY_ENABLED", True):
        return video
    meta = meta or video_meta(video)
    w = int(meta.get("width") or 0)
    h = int(meta.get("height") or 0)
    # 没探到分辨率就不折腾（避免对未知源白转一份）
    if w <= 0 or h <= 0:
        return video
    if w * h <= int(getattr(C, "PROXY_TRIGGER_PIXELS", 1920 * 1080)):
        return video

    dur = float(meta.get("duration") or 0.0)
    dst = _proxy_path(cd, video, meta)
    if _proxy_is_valid(dst, dur):
        return dst

    max_w = int(getattr(C, "PROXY_MAX_W", 1920))
    # 等比缩放，长边封顶 max_w。偶数高度（yuv420p 要求）用 -2 自动对齐。
    scale = f"scale={max_w}:-2:flags=lanczos"
    crf = int(getattr(C, "PROXY_CRF", 23))
    maxrate = str(getattr(C, "PROXY_MAXRATE", "2500k"))
    bufsize = str(getattr(C, "PROXY_BUFSIZE", "5000k"))
    timeout = int(getattr(C, "PROXY_PRESET_TIMEOUT",
                          getattr(C, "PROXY_TIMEOUT", 3600)))

    def _encode(codec: str, preset: str, on_prog=None,
                hwaccel: str = "") -> bool:
        """转码并（可选）回报进度。

        hwaccel 非空时走**GPU 全链路**：解码和缩放都不落 CPU。
        这是代理提速 4.7 倍的关键（详见 config.PROXY_HW_DECODE 处的实测数据）。

        必须开 `-progress pipe:1`：ffmpeg 默认只在 stderr 吐静态统计，
        而我们 `-v error` 把它们全关了——于是转码阶段对调用方**完全静默**。
        踩过的坑：用户扫电视剧库，看到界面几分钟没动静，以为卡死，
        实际 4K 代理正在后台正常转。有了这条管道，
        cb 能拿到 out_time_us，算出真实百分比推给前端。
        """
        cmd = [C.FFMPEG, "-y", "-nostdin", "-v", "error", "-hide_banner",
               "-progress", "pipe:1", "-nostats"]
        # -hwaccel 必须放在 -i 之前，否则被当成输出选项直接报错
        if hwaccel:
            cmd += ["-hwaccel", hwaccel, "-hwaccel_output_format", hwaccel]
        cmd += ["-i", video]
        if hwaccel:
            # GPU 缩放：interp_algo 必须显式给 bicubic(3)。
            # 默认 0 是 nearest，实测会**丢台词**（亮剑 14 条丢 2 条）。
            cmd += ["-vf", f"scale_cuda={max_w}:-2:"
                           f"interp_algo={int(getattr(C, 'PROXY_CUDA_INTERP', 3))}"
                           f":format=nv12"]
        else:
            cmd += ["-vf", scale]
        cmd += ["-c:v", codec]
        if codec == "h264_nvenc":
            # 码率必须封顶。之前写成 `-rc vbr -cq 23 -b:v 0`，以为 CQ 会自己收敛，
            # 实际 -b:v 0 在 VBR 下等于**放弃码率约束**：实测 43 分钟的 3840×2880
            # 代理压到 2.9GB（约 9Mbps），大文件 + 5倍解码量把进程压到段错误。
            # 加 -maxrate/-bufsize 后体积可预测（见 PROXY_MAXRATE），CQ 仍主导画质。
            cmd += ["-rc", "vbr", "-cq", str(crf), "-b:v", maxrate,
                    "-maxrate", maxrate, "-bufsize", bufsize]
            cmd += ["-preset", getattr(C, "PROXY_PRESET", "p1")]
        else:
            cmd += ["-preset", preset, "-crf", str(crf),
                    "-maxrate", maxrate, "-bufsize", bufsize]
        if not hwaccel:
            cmd += ["-pix_fmt", "yuv420p"]
        # GPU 路径**绝对不能加 -pix_fmt**（加 nv12/yuv420p 都不行）：scale_cuda 的
        # format=nv12 已经定了输出格式，再让 -pix_fmt 插手就会触发
        # auto_scale 协商失败 —— "Impossible to convert between the formats
        # supported by Parsed_scale_cuda_0 and auto_scale_0"，整条转码 rc≠0。
        # nv12 本来就是 yuv420p 的平面表示，编码器直接吃，不用再声明。
        cmd += ["-c:a", getattr(C, "PROXY_AUDIO", "copy"), dst]
        try:
            if not on_prog:
                p = run(cmd, timeout=timeout)
                return (os.path.exists(dst) and os.path.getsize(dst) > 0
                        and p.returncode == 0)
            # 有进度回调：走 Popen 逐行读 -progress，边读边回调（实时）
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW)

            def _pump():
                seen_end = False
                last_ot = -1
                buf = b""
                proc.stdout.flush()
                for line in proc.stdout:
                    buf += line
                    if not line.strip():
                        continue
                    s = line.decode("utf-8", "ignore").strip()
                    if s == "progress=end":
                        seen_end = True
                        continue
                    if not s.startswith("out_time_us="):
                        continue
                    try:
                        ot = int(s.split("=", 1)[1])
                    except Exception:
                        continue
                    # 4K 片 2~8 分钟，每 0.5s 报一次会刷屏前端；按 2% 一跳
                    pct = int(min(100, ot / 10000.0 /
                                  max(1.0, dur) * 100))
                    if pct - last_ot >= 2 or pct >= 100:
                        last_ot = pct
                        try:
                            on_prog(pct)
                        except Exception:
                            pass
                return seen_end

            import threading as _th
            box = {}
            t = _th.Thread(target=lambda: box.__setitem__("end", _pump()),
                           daemon=True)
            t.start()
            try:
                proc.wait(timeout=timeout)
            except Exception:
                proc.kill()
                proc.wait(timeout=30)
            t.join(timeout=5)
            seen_end = bool(box.get("end"))
            done_ok = seen_end or (os.path.exists(dst)
                                   and os.path.getsize(dst) > 0)
            return bool(done_ok and os.path.exists(dst)
                        and os.path.getsize(dst) > 0)
        except Exception:
            return False

    ok = False
    prog = _wrap_prog(cb)
    # 依次尝试的编码方案，从快到慢。每一级失败（驱动缺失/滤镜协商不过/
    # 编码器打不开）都自动降级，最终回落 CPU 软解，保证任何环境都能出代理。
    attempts = []
    if getattr(C, "PROXY_USE_GPU", True):
        if getattr(C, "PROXY_HW_DECODE", True):
            # GPU 全链路：解码 + 缩放 + 编码都在 4060Ti 上，45 分钟片约 2 分钟
            attempts.append(("h264_nvenc", "", str(getattr(C, "PROXY_CUDA", "cuda"))))
        # CPU 解码 + nvenc 编码（旧的实现，慢 4.7 倍但兼容性最好）
        attempts.append(("h264_nvenc", "", ""))
    # 兜底：GPU 不可用（驱动/ffmpeg 没编 nvenc）→ CPU ultrafast
    attempts.append(("libx264", "ultrafast", ""))

    for codec, preset, hw in attempts:
        if cb and hw:
            # 只在第一级提示，后面几级是降级路径，安静跑就行
            cb("代理转码 · GPU 全链路")
        ok = _encode(codec, preset, on_prog=prog, hwaccel=hw)
        if ok:
            break
        # 半成品必须清掉，否则下一级 -y 覆盖失败会留下坏文件
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except Exception:
            pass

    if ok and _proxy_is_valid(dst, dur):
        return dst
    # 兜底：建失败/校验不过就删掉半成品，绝不留一个时长对不上的代理在这
    try:
        if os.path.exists(dst):
            os.remove(dst)
    except Exception:
        pass
    return video


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


def _sub_vf(srt: str, si: int = None) -> str:
    r"""subtitles 滤镜参数。路径里 Windows 盘符的冒号在 filtergraph 里要转义成
    `\:`，且整个值套单引号，否则 "D:/x.srt" 的冒号会被当成选项分隔符
    （实测报 "Unable to parse option value ... as image size"）

    si 非空 = 烧**片源文件里**的内嵌字幕轨（filename 照旧传文件路径，只是这次
    传的是视频本身）。走 si= 而不是先抽成 SRT，是因为位图字幕（PGS/DVDSUB）
    ffmpeg 根本转不出文本流，但滤镜渲染照样能画出来。

    ⚠ si 是「第几条**字幕流**」的**相对序号**（0 起），**不是 ffprobe 的绝对流
    索引**。实测：mkv 里 0=视频 1=音频 2=字幕（唯一一条字幕），传 si=2 直接报
    `Unable to locate subtitle stream`，传 si=0 才烧得上。多轨片里这两个值几乎
    必然不同，换算由 export_clip 负责，别把绝对索引直接丢进来。
    """
    style = (f"FontName={C.SUB_FONT},FontSize=28,"
             "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
             "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=28")
    p = srt.replace("\\", "/").replace(":", r"\:")
    out = f"subtitles=filename='{p}'"
    if si is not None:
        out += f":si={int(si)}"
    return out + f":force_style='{style}'"


# ---------------- 烧字幕策略：谁来决定要不要烧 ----------------
#
# 为什么要按台词来源分流：台词来源 ocr = 画面上本来就印着字幕（我们 OCR 认出
# 来的），这时再烧一层会在画面下方压出一道重影，还得为此强制重编码、慢好几倍。
# 来源是 external/embedded/asr 的片子画面上没字幕，burn 才真的有用。
#
# 三档而不是开关的原因：auto 是「按来源自动判断」，off 是「明确不烧」，
# force 是「我知道画面有字幕，但这条我就是要烧一份修正过的」——
# OCR 识别难免有错字，纠完字想重新烧一份时需要这个出口。
SUB_MODES = ("auto", "off", "force")


def decide_burn(mode, line_src: str = ""):
    """决定这次导出要不要烧字幕。返回 (burn: bool, note: str)。

    mode 为 None/空 = auto；也接受布尔（True=force、False=off）以兼容旧调用。
    note 是给调用方看的说明，Web 端用来提示用户为什么没烧。
    """
    if mode is None or mode == "":
        mode = "auto"
    if isinstance(mode, bool):          # 旧接口传的是布尔
        mode = "force" if mode else "off"
    mode = str(mode).strip().lower()
    if mode not in SUB_MODES:
        mode = "auto"
    src = (line_src or "").strip().lower()
    if mode == "off":
        return False, ""
    if mode == "force":
        return True, ""
    # auto：OCR 来源（画面本来有字幕）不烧，其余照旧按需求烧
    if src == "ocr":
        return False, "画面已有字幕（OCR 识别），已自动跳过烧录；要烧请选「强制」"
    return True, ""


def _track_label(kind: str, r: dict) -> str:
    """轨道的人话标签，例：`音轨 #2 · chi · 国语 · 2声道 · aac · 默认`"""
    bits = [f"{kind} #{r['index']}"]
    if r.get("lang"):
        bits.append(r["lang"])
    if r.get("title"):
        bits.append(r["title"])
    if kind == "音轨" and r.get("channels"):
        bits.append(f"{r['channels']}声道")
    if r.get("codec"):
        bits.append(r["codec"])
    tail = [x for x in ("默认" if r.get("default") else "",
                        "强制" if r.get("forced") else "") if x]
    if tail:
        bits.append("/".join(tail))
    return " · ".join(bits)


def list_tracks(path: str) -> dict:
    """探出片源的**音轨 / 字幕轨清单**，给「导出时挑一条」用。

    index 是 ffprobe 的**绝对流索引**（不是 a:0 / s:0 那种相对序号）——
    ffmpeg 的 `-map 0:<index>` 和 subtitles 滤镜的 `si=` 都直接吃这个值，
    所以探一次就能同时喂给两边，不用再换算。

    探测失败 / 片子没有对应轨道时返回空列表，调用方按「单轨」处理即可
    （前端据此决定弹不弹选择框）。
    """
    info = ffprobe_json(path)
    audio, subs = [], []
    for s in info.get("streams", []):
        ct = s.get("codec_type")
        if ct not in ("audio", "subtitle"):
            continue
        try:
            idx = int(s.get("index"))
        except (TypeError, ValueError):
            continue
        tags = s.get("tags") or {}
        dis = s.get("disposition") or {}
        row = {
            "index": idx,
            "codec": str(s.get("codec_name") or ""),
            "lang": str(tags.get("language") or "").strip(),
            "title": str(tags.get("title") or "").strip(),
            "default": bool(dis.get("default")),
            "forced": bool(dis.get("forced")),
        }
        if ct == "audio":
            try:
                row["channels"] = int(s.get("channels") or 0)
            except (TypeError, ValueError):
                row["channels"] = 0
            row["label"] = _track_label("音轨", row)
            audio.append(row)
        else:
            row["label"] = _track_label("字幕轨", row)
            subs.append(row)
    return {"audio": audio, "subtitle": subs}


# mp4 容器塞得下的**文本**字幕编码。位图字幕（PGS/DVDSUB/DVB）mp4 不认，
# `-c:s mov_text` 转不动会让整条 ffmpeg 失败，所以只挑这批带进产物。
SUB_TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "text", "webvtt",
                   "sami", "realtext", "subviewer", "subviewer1", "pjs", "mpl2"}


def keepable_subs(path: str) -> list:
    """片源里能安全塞进 mp4 的字幕流（**绝对流索引**列表）。

    不加这个过滤会出事：蓝光 remux 常见 PGS 位图字幕，mp4 根本装不下，
    硬 map 过去 ffmpeg 直接失败、整个片段导不出来。所以只带文本字幕。
    """
    try:
        subs = list_tracks(path)["subtitle"]
    except Exception:
        return []
    return [s["index"] for s in subs
            if (s.get("codec") or "").lower() in SUB_TEXT_CODECS]


def first_video_stream(path: str):
    """第一条**真**视频流的绝对索引；探不出来返回 None。

    mp4 常带一条 mjpeg 封面流（disposition.attached_pic）而且往往排在最前面，
    直接 `-map 0:v:0` 会选中它、导出一堆静帧。所以这里跳过 attached_pic。
    """
    try:
        for s in ffprobe_json(path).get("streams", []):
            if s.get("codec_type") != "video":
                continue
            if (s.get("disposition") or {}).get("attached_pic"):
                continue
            return int(s.get("index"))
    except (TypeError, ValueError):
        pass
    return None


def auto_embedded_sub(path: str, line_src: str = ""):
    """auto 档的内嵌字幕回退：片源自带字幕轨就烧第一条，别让原片字幕凭空消失。

    · 台词来源是 ocr（说明画面本来就印着字幕）→ 返回 None，再烧会压出重影
    · 片源没有任何字幕轨 → 返回 None，交给台词库那条路
    返回的仍是 **ffprobe 绝对流索引**，可直接喂给 export_clip 的 sub_track。
    """
    if (line_src or "").strip().lower() == "ocr":
        return None
    try:
        subs = list_tracks(path)["subtitle"]
    except Exception:
        return None
    return subs[0]["index"] if subs else None


def export_clip(video: str, start: float, end: float, out: str,
                reencode: bool = True, srt: str = None,
                audio_track: int = None, sub_track: int = None,
                keep_subs: bool = True) -> bool:
    """导出片段。默认重编码（帧精确），fast 模式用 -c copy（秒级但只能按关键帧切）。

    字幕二选一（都给时以 sub_track 为准，前端是互斥下拉，正常不会同时给）：
      · srt       = 外挂 SRT 路径（项目台词库按片段窗口生成的那份），烧进画面
      · sub_track = 片源内嵌字幕轨的**绝对流索引**，用 subtitles 滤镜的 si= 直接烧
                    ——位图字幕（PGS/DVDSUB）抽不出文本流，但滤镜照样能渲染，
                    所以选内嵌轨一律走 si=，不先落 SRT。
    给了字幕就必然重编码（流拷贝没法叠加滤镜）。

    audio_track = 片源音轨的绝对流索引；给了就显式 `-map` 只留那一条。
    不给且不需要带字幕时**完全不加 -map**，保持老行为（交给 ffmpeg 挑最佳音轨），
    这样单轨片和以前导出的结果一模一样，不会因为加了 map 反而丢音。

    keep_subs（默认 True）= 把片源的**文本字幕轨**一起带进产物（软字幕）。
    之前不加任何 -map/-c:s，ffmpeg 对字幕的默认流选择根本不生效，原片字幕
    导出后凭空消失；现在默认保住。已烧硬字幕（sub_track/srt）时不再重复带。
    """
    os.makedirs(os.path.dirname(out), exist_ok=True)
    dur = max(0.2, end - start)
    base = [C.FFMPEG, "-y", "-ss", f"{max(0.0, start):.3f}",
            "-t", f"{dur:.3f}", "-i", video]
    # 烧「片源内嵌字幕轨」时必须把 -ss 放到 -i 后面。
    # 放在 -i 前是输入 seek：ffmpeg 会重写 PTS，而 subtitles 滤镜按原文件
    # 时间轴取字幕，结果什么都烧不上（ffmpeg 还返回 0，日志里只是没有字幕帧）。
    # 输出 seek 从文件头解码到切点，略慢，但字幕时间轴是对的。
    base_sub = [C.FFMPEG, "-y", "-i", video,
                "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}"]

    # ---- 轨道映射 ----
    # 烧了硬字幕就别再带软字幕，否则播放时可能叠两遍
    sub_idx = keepable_subs(video) if (keep_subs and sub_track is None) else []
    v_first = (first_video_stream(video)
               if (audio_track is not None or sub_idx) else None)
    vmap = f"0:{v_first}" if v_first is not None else "0:v:0"
    amap = None
    if audio_track is not None:
        try:
            amap = f"0:{int(audio_track)}"
        except (TypeError, ValueError):
            amap = None

    # 不带字幕的映射：只有显式选了音轨才加（其余保持老行为不加 map）
    maps_plain = ["-map", vmap, "-map", amap] if amap else []
    # 带字幕的映射。⚠ 一旦命令行里出现**任何** -map，ffmpeg 的默认流选择就
    # 整体关闭——只保留显式 map 的流。所以带字幕时必须把视频/音频也显式带上，
    # 否则导出来只剩一条字幕流，画面声音全没。
    maps_sub = []
    if sub_idx:
        maps_sub = (["-map", vmap, "-map", amap or "0:a:0?"]
                    + [x for i in sub_idx for x in ("-map", f"0:{i}")])

    vf = None
    if sub_track is not None:
        try:
            st_i = int(sub_track)
            # sub_track 对外统一是 ffprobe 的**绝对流索引**（和 audio_track 一致，
            # 前端拿到的 list_tracks 也是这个值），但 subtitles 滤镜的 si= 要的是
            # 相对序号，所以这里按字幕流在片源里的先后位置换算一次。
            _si = [s["index"] for s in list_tracks(video)["subtitle"]]
            if st_i in _si:
                vf = _sub_vf(video, si=_si.index(st_i))
        except (TypeError, ValueError):
            vf = None
    elif srt and os.path.exists(srt):
        vf = _sub_vf(srt)

    ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    ENC_S = ENC + ["-c:s", "mov_text"]

    def _go(maps, extra, timeout=900):
        # 内嵌字幕烧录走输出 seek，保证 subtitles 滤镜和原字幕时间轴一致
        head = base_sub if sub_track is not None else base
        run(head + maps + extra + [out], timeout=timeout)
        return os.path.exists(out) and os.path.getsize(out) > 0

    # 1) 烧硬字幕（必然重编码）
    if vf:
        if _go(maps_sub or maps_plain, ["-vf", vf] + (ENC_S if maps_sub else ENC)):
            return True
        # 烧失败别静默丢结果：往下退回无字幕重编码
    # 2) 秒切（流拷贝）
    elif not reencode:
        if maps_sub and _go(maps_sub, ["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                                       "-avoid_negative_ts", "make_zero"], 300):
            return True
        if _go(maps_plain, ["-c", "copy", "-avoid_negative_ts", "make_zero"], 300):
            return True
    # 3) 常规 / 兜底重编码：先试带字幕，字幕转不动就去掉再来一次，别整个失败
    if maps_sub and _go(maps_sub, ENC_S):
        return True
    return _go(maps_plain, ENC)


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
