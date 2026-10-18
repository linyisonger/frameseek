# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - 台词获取

优先级：外挂字幕 -> 内嵌字幕流 -> faster-whisper 语音转写（兜底）

两个本机坑，都在这里绕开了：
1. faster-whisper 1.2.1 用 av.open(..., metadata_errors=...)，而本机 av 19.x 已移除该参数，
   一转写就 TypeError。→ 不传文件路径，改用 ffmpeg 抽成 16k 单声道 ndarray 再喂给模型。
2. huggingface.co 直连会 502。→ 走 HF_ENDPOINT 镜像（config.ASR_ENDPOINT）。
"""
import os
import re
import subprocess
import threading
from typing import List, Dict, Any, Tuple, Optional

# 必须在 import huggingface_hub 之前设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import numpy as np  # noqa: E402

from core import config as C  # noqa: E402
from core import media  # noqa: E402

_TS = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")

_MODEL = None          # 模型单例（加载一次，别每个视频都重新载入）
_MODEL_SIZE = ""
_DEV_USED = ""         # 实际生效的 "device/compute"
_LOCK = threading.Lock()   # 多库并发索引时，别让两个线程各建一份模型撑爆显存
_CUDA_READY = None     # None=未探测 / True / False
_CUDA_DIR = ""


def _to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(text: str) -> List[Dict[str, Any]]:
    out = []
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    for b in blocks:
        m = _TS.search(b)
        if not m:
            continue
        start = _to_sec(*m.groups()[:4])
        end = _to_sec(*m.groups()[4:])
        body = b[m.end():].strip()
        body = re.sub(r"<[^>]+>", "", body)          # 去 ass 标签
        body = re.sub(r"\{[^}]*\}", "", body)
        body = body.replace("\\N", " ").replace("\\n", " ")
        body = re.sub(r"\s+", " ", body).strip()
        if not body:
            continue
        out.append({"start": start, "end": end, "text": body})
    return out


def from_embedded(video: str) -> List[Dict[str, Any]]:
    """抽取内嵌字幕流；没有就返回空列表"""
    meta = media.video_meta(video)
    idxs = meta.get("sub_streams") or []
    if not idxs:
        return []
    tmp = os.path.join(os.path.dirname(video), ".__frameseek_sub.srt")
    try:
        for si in idxs[:3]:
            if media.extract_subtitle(video, si, tmp):
                try:
                    with open(tmp, "r", encoding="utf-8", errors="ignore") as f:
                        rows = parse_srt(f.read())
                except Exception:
                    rows = []
                if rows:
                    return rows
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return []


def _try_external_srt(video: str) -> List[Dict[str, Any]]:
    """同目录下同名 .srt / .ass 外挂字幕"""
    base = os.path.splitext(video)[0]
    for ext in C.SUB_EXT:
        p = base + ext
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    rows = parse_srt(f.read())
            except Exception:
                rows = []
            if rows:
                return rows
    return []


# ---------------- 语音转写 ----------------
def _decode_16k(path: str, start: float = 0.0, dur: float = 0.0) -> np.ndarray:
    """ffmpeg 抽 16k 单声道 float32（不经过 av，绕开 metadata_errors 报错）"""
    cmd = [C.FFMPEG, "-nostdin", "-v", "error", "-hide_banner"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path, "-vn"]
    if dur > 0:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not p.stdout:
        raise RuntimeError("没有音轨，或 ffmpeg 抽音频失败：" +
                           (p.stderr.decode("utf-8", "ignore")[-200:]))
    return np.frombuffer(p.stdout, dtype=np.float32)


# ---------------- CUDA 运行时挂载 ----------------
def _mount_cuda_dir() -> str:
    """把 CUDA 12 运行时挂进搜索路径。

    本机没装 CUDA Toolkit，但 Ollama 自带一套（cublas64_12.dll / cublasLt64_12.dll /
    cudart64_12.dll）。三层保险，缺一不可：
      1. PATH 前置（ct2 内部用的是不带标志的 LoadLibrary，add_dll_directory 对它无效）
      2. os.add_dll_directory（给走 LoadLibraryEx 的路径用）
      3. ctypes 按绝对路径预加载（最可靠：同名 DLL 一旦在进程内，后续按名查找直接命中）
    """
    global _CUDA_DIR
    if _CUDA_DIR:
        return _CUDA_DIR
    for d in C.ASR_CUDA_DLL_DIRS:
        if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "cublas64_12.dll")):
            continue
        try:
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
            except Exception:
                pass
            import ctypes
            for dll in ("cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll"):
                p = os.path.join(d, dll)
                if os.path.exists(p):
                    ctypes.CDLL(p)
            _CUDA_DIR = d
            return d
        except Exception:
            continue
    return ""


def cuda_available() -> bool:
    global _CUDA_READY
    if _CUDA_READY is not None:
        return _CUDA_READY
    if str(C.ASR_DEVICE).lower() == "cpu":
        _CUDA_READY = False
        return False
    try:
        _mount_cuda_dir()
        import ctranslate2 as ct
        _CUDA_READY = ct.get_cuda_device_count() > 0
    except Exception:
        _CUDA_READY = False
    return _CUDA_READY


def _device_candidates() -> List[Tuple[str, str]]:
    """按优先级排出 (device, compute_type)，前面失败就往后退"""
    want = str(C.ASR_DEVICE or "auto").lower()
    comp = str(C.ASR_COMPUTE or "auto").lower()
    if want == "cuda" or (want == "auto" and cuda_available()):
        if comp in ("float16", "int8_float16", "int8", "float32"):
            first = comp
        else:
            first = "float16"
        chain = [("cuda", first), ("cuda", "float16"), ("cuda", "int8_float16")]
        return chain + [("cpu", "int8")]
    if want == "cuda":          # 明确要 GPU 但不可用 → 退回 CPU
        return [("cpu", "int8")]
    return [("cpu", comp if comp in ("int8", "int8_float32", "float32") else "int8")]


def device_info() -> Dict[str, Any]:
    """给页面显示：当前能不能用 GPU、实际用的哪个设备"""
    return {
        "cuda": cuda_available(),
        "cuda_dir": _CUDA_DIR,
        "device": (_DEV_USED.split("/")[0] if _DEV_USED else ""),
        "compute": (_DEV_USED.split("/")[1] if _DEV_USED else ""),
        "model": _MODEL_SIZE,
        "want": str(C.ASR_DEVICE),
    }


def get_model(size: str = None):
    """加载模型（单例）。GPU 失败自动退 CPU，模型按 config.ASR_MODELS 顺序降级"""
    global _MODEL, _MODEL_SIZE, _DEV_USED
    from faster_whisper import WhisperModel
    chain = [size] if size else list(C.ASR_MODELS)
    with _LOCK:
        if size is None and _MODEL is not None:
            return _MODEL, _MODEL_SIZE, ""
        cands = _device_candidates()
        last_err = ""
        for s in chain:
            if _MODEL is not None and _MODEL_SIZE == s:
                return _MODEL, s, ""
            for dev, comp in cands:
                try:
                    _MODEL = WhisperModel(s, device=dev, compute_type=comp)
                    _MODEL_SIZE = s
                    _DEV_USED = f"{dev}/{comp}"
                    return _MODEL, s, ""
                except Exception as e:
                    last_err = f"{s}@{dev}/{comp}: {type(e).__name__} {e}"
    return None, "", last_err or "模型加载失败"


def transcribe(video: str, model_size: str = None, lang: str = None,
               progress=None, cancel=None) -> Tuple[List[Dict[str, Any]], str]:
    """返回 (台词, 错误信息)。分块处理：内存可控，也能报真实进度

    cancel 是无参可调用对象，返回 True 就收工；此时返回空 + "__cancelled__"，
    调用方据此把这条素材留成"还没识别"，下次接着跑（不能把半截台词当完整结果）。
    """
    model, used, err = get_model(model_size)
    if model is None:
        return [], err or "语音模型加载失败"

    duration = media.video_meta(video).get("duration") or 0.0
    chunk = max(60.0, float(C.ASR_CHUNK))
    n_chunk = max(1, int(duration // chunk) + (1 if duration % chunk else 0)) \
        if duration > 0 else 1

    out: List[Dict[str, Any]] = []
    gap = max(0.0, float(C.ASR_CHUNK_OVERLAP))
    for i in range(n_chunk):
        if cancel and cancel():
            return [], "__cancelled__"
        st = i * chunk
        dur = chunk + gap
        if progress:
            progress(len(out), f"转写 {i + 1}/{n_chunk} 段")
        try:
            audio = _decode_16k(video, st if n_chunk > 1 else 0.0,
                                dur if n_chunk > 1 else 0.0)
        except Exception as e:
            return out, f"第 {i + 1} 段抽音频失败：{e}"
        if audio.size == 0:
            continue
        try:
            segs, _info = model.transcribe(
                audio, language=lang or C.ASR_LANG, vad_filter=C.ASR_VAD,
                vad_parameters=dict(min_silence_duration_ms=400),
            )
            for seg in segs:
                t = (seg.text or "").strip()
                if not t:
                    continue
                out.append({"start": st + float(seg.start),
                            "end": st + float(seg.end), "text": t})
        except Exception as e:
            return out, f"第 {i + 1} 段识别失败：{type(e).__name__} {e}"
        if progress:
            progress(len(out), f"转写 {i + 1}/{n_chunk} 段 · 已 {len(out)} 条")
    return out, ""


def get_lines(video: str, use_asr: bool = None, progress=None,
              skip_external: bool = False, skip_embedded: bool = False,
              cancel=None, ocr_video: str = None) -> Tuple[List[Dict[str, Any]], str, str]:
    """返回 (台词列表, 来源, 错误信息)。

    来源取值：external(外挂SRT) / embedded(内嵌) / ocr(画面硬字幕) /
              asr(语音转写，已默认关闭) / none

    优先级：外挂 SRT -> 内嵌字幕 -> OCR 硬字幕识别 -> 语音转写（已停用）
    排这个顺序的理由：外挂是你自己校对过的最可信；内嵌是官方字幕，准确且零成本；
    OCR 纯看画面，能处理方言/听不清/音乐盖过人声的片子，但慢且有错字，所以靠后。

    `use_asr` 是历史遗留的字段名，现在语义是"允不允许做机器识别"
    （OCR + ASR 都算）；None 时按 config 默认。

    skip_external / skip_embedded 是给「重新识别台词」用的：
    OCR 跑完会在视频旁边落一个同名 .srt（我们自己的产物），下次再点重新识别
    如果还按优先级先读它，等于把上一次的旧结果又读了一遍——改再多的 OCR
    逻辑也体现不出来。所以重新识别时跳过它，让 OCR 真跑一遍并覆盖那个 .srt。

    cancel 是无参可调用对象（返回 True 表示用户要停）。中断时返回空台词 +
    错误码 "__cancelled__"，**不会**落库也不会写 .srt。

    `ocr_video` = 4K 片先压好的 1080p 代理（media.ensure_proxy）。**只给 OCR 分支
    用**：外挂/内嵌字幕流和语音转写都必须读原片（外挂 srt 就贴在原片旁边，
    代理里没有字幕流）。没给就一路读原片。
    """
    # 外挂和内嵌字幕是"白捡的"，任何情况下都先读
    if not skip_external:
        rows = _try_external_srt(video)
        if rows:
            return rows, "external", ""
    if not skip_embedded:
        rows = from_embedded(video)
        if rows:
            return rows, "embedded", ""

    if use_asr is None:
        allow_machine = C.OCR_ENABLED or C.ASR_ENABLED
    else:
        allow_machine = bool(use_asr)
    if not allow_machine:
        return [], "none", ""

    ocr_err = ""
    if C.OCR_ENABLED:
        try:
            from core import ocr as ocr_mod
            # OCR 分支一律发结构化 dict（带帧级完成度）；下游 _prog /
            # ocr_lines 的 str 分支仍兼容普通调用方。
            ocr_prog = ocr_mod.structured(progress) if progress else None
            # 抽帧源优先用 1080p 代理（4K 片省 5 倍解码量，画质无损——
            # OCR 送检带宽固定 OCR_WIDTH，缩完是同一张图）
            ocr_src = ocr_video or video
            rows, oerr = ocr_mod.ocr_lines(ocr_src, progress=ocr_prog,
                                           cancel=cancel)
            if rows:
                if C.SUB_SEARCH_SAVE:
                    try:
                        media.write_srt(video, rows)
                    except Exception:
                        pass
                return rows, "ocr", ""
            ocr_err = oerr
            if oerr == "__cancelled__":
                # 用户按了停止：不落库、不写 srt，直接上报中断
                return [], "none", "__cancelled__"
        except Exception as e:
            ocr_err = f"{type(e).__name__}: {e}"

    err = ocr_err

    # 语音转写（已停用，代码留作最后的兜底）
    if C.ASR_ENABLED:
        try:
            rows, e2 = transcribe(video, progress=progress, cancel=cancel)
            if rows:
                return rows, "asr", ""
            if e2 == "__cancelled__":
                return [], "none", "__cancelled__"
            err = "；".join(x for x in (err, e2) if x)
        except Exception as e:
            err = "；".join(x for x in (err, f"{type(e).__name__}: {e}") if x)

    return [], "none", err
