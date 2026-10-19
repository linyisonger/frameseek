# -*- coding: utf-8 -*-
"""统一生图后端层：Agnes / SD1.5 双后端可插拔（2026-10-16）。

对外只暴露两件事：
- backends()：给面板摸底——每个后端就绪没有、快不快、离不离线；
- gen_image()：一条龙出 PNG，落 <项目根>/AI生成/绘画/，旁边带同名
  sidecar JSON（参数留痕，供「重掷」复现同一组参数只换种子）。

后端优先级：默认 auto = agnes → sd15 依次降级；也可在 UI 里
显式选定某一个（Agnes 优先）。

    agnes   Agnes Image 2.5 Flash（API，秒级，1K/2K，最多 3 张参考图）
    sd15    本地 SD1.5 + 线稿 LoRA + canny ControlNet（进程内直调 ComfyUI，
            2~5 秒/张，种子可复现，构图不漂——重掷主力）

canny 控制图在**父进程**用 cv2 预处理好再传给 sdgen.py——ComfyUI 内嵌
python 没装 cv2，而跑 app 的解释器（envs/default）有。
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------- 路径 / 常量 ----------------
PAINT_SUBDIR = "绘画"          # <项目根>/AI生成/绘画/（所有后端的产物都落这）

# Agnes skill 脚本（API 生图）
AGNES_SCRIPT = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills", "agnes-image-gen",
    "scripts", "agnes_image.py")
AGNES_PY = sys.executable       # app 跑在 envs/default，直接用当前解释器

# 本地 ComfyUI 便携版
COMFY_ROOT = os.environ.get(
    "FRAMESEEK_COMFY_ROOT", r"D:\project\AIProjects\ComfyUI_windows_portable")
COMFY_PY = os.path.join(COMFY_ROOT, "python_embeded", "python.exe")
COMFY_APP_ROOT = (os.path.join(COMFY_ROOT, "ComfyUI")
                  if os.path.isdir(os.path.join(COMFY_ROOT, "ComfyUI"))
                  else COMFY_ROOT)
SDGEN = os.path.join(BASE, "sdgen.py")
SDGEN_LOG = os.path.join(BASE, "_out", "sdgen_last.log")

# SD1.5 模型文件名（与 models/ 下实际文件对应）
CKPT_NAME = "v1-5-pruned-emaonly.safetensors"
CONTROLNET_NAME = "diffusion_pytorch_model.fp16.safetensors"
LORA_DEFAULT = "simple-lineart_v6.safetensors"

BACKEND_ORDER = ("agnes", "sd15")   # auto 模式的降级链
DEFAULT_LORA_SCALE = 0.8
DEFAULT_CONTROL_STRENGTH = 0.65
CANVAS_COLOR = "#F5EBD7"                  # 暖米黄纸底（H3 垫边用）

_AGNES_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")


def _log(msg: str):
    print(f"[frameseek-img] {msg}", file=sys.stderr)


def _which_ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


# ---------------- 落点 ----------------
def paint_dir() -> str:
    """绘画产物目录：<项目根>/AI生成/绘画/（尊重 hub 的 gen_outdir 设置）。"""
    from core import gen as _gen
    d = os.path.join(_gen.output_dir(), PAINT_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _safe_stem(name: str, backend: str) -> str:
    n = os.path.basename((name or "").strip().strip("\"'"))
    n = re.sub(r'[\\/:*?"<>|]+', "_", n).strip(" .")[:60]
    return n or f"绘画_{backend}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _write_sidecar(png_path: str, meta: dict):
    """参数留痕：<名字>.png 旁边写 <名字>.json，重掷时按它复现。"""
    try:
        side = os.path.splitext(png_path)[0] + ".json"
        with open(side, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except Exception as e:
        _log(f"sidecar 写入失败 {png_path}: {e}")


def list_outputs(limit: int = 120) -> list:
    """绘画产物列表（新的在前），带 sidecar 参数；给面板画廊用。"""
    d = paint_dir()
    out = []
    try:
        names = [n for n in os.listdir(d)
                 if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    except OSError:
        return []
    for n in sorted(names, key=lambda x: os.path.getmtime(os.path.join(d, x)),
                    reverse=True)[:limit]:
        p = os.path.join(d, n)
        meta = {}
        side = os.path.splitext(p)[0] + ".json"
        if os.path.isfile(side):
            try:
                with open(side, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        out.append({"file": p, "name": n, "meta": meta,
                    "mtime": os.path.getmtime(p),
                    "size_kb": round(os.path.getsize(p) / 1024)})
    return out


# ---------------- 后端摸底 ----------------
def backends() -> list:
    """每个后端的就绪状态 / 速度 / 离线属性（面板下拉框直接用）。"""
    from core import gen as _gen
    items = []
    # agnes
    items.append({
        "id": "agnes", "name": "Agnes Image 2.5 Flash（API）",
        "ready": os.path.isfile(AGNES_SCRIPT),
        "note": "秒级出图 · 1K/2K · 最多 3 张参考图 · 需联网",
        "eta": "约 10~60 秒", "offline": False})
    # sd15
    ckpt = os.path.join(COMFY_APP_ROOT, "models", "checkpoints", CKPT_NAME)
    cn = os.path.join(COMFY_APP_ROOT, "models", "controlnet", CONTROLNET_NAME)
    sd_ok = (os.path.isfile(COMFY_PY) and os.path.isfile(SDGEN)
             and os.path.isfile(ckpt) and os.path.isfile(cn))
    items.append({
        "id": "sd15", "name": "SD1.5 + 线稿 LoRA + ControlNet（本地）",
        "ready": sd_ok,
        "note": "2~5 秒/张 · 种子可复现 · canny 锁构图不漂 · 重掷主力",
        "eta": "约 5~20 秒（含模型加载）", "offline": True})
    return items


def backend_ready(bid: str) -> bool:
    for b in backends():
        if b["id"] == bid:
            return bool(b["ready"])
    return False


# ---------------- 工具 ----------------
def _datauri(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext, "image/png")
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def _ratio_of(w: int, h: int) -> str:
    """宽高 → 最接近的宽高比标签（Agnes / H3 都用）。"""
    import math
    if not w or not h:
        return "1:1"
    r = w / h
    best, bd = "1:1", 9e9
    for tag in _AGNES_RATIOS:
        a, b = tag.split(":")
        d = abs(math.log(r / (int(a) / int(b))))
        if d < bd:
            best, bd = tag, d
    return best


def _img_size(path: str):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def _run_protocol(cmd: list, log_path: str, on_tick, timeout: int,
                  cwd: str = "") -> dict:
    """跑子进程（sdgen/h3gen 的 JSON 行协议），返回 done 信息 dict。

    与 core/gen.py 同一套解析逻辑；日志落 log_path（追加）。
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "a", encoding="utf-8", errors="replace")
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=cwd or BASE, stdout=subprocess.PIPE,
                            stderr=log, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    done_info, err = None, ""
    try:
        while True:
            if time.time() - t0 > timeout:
                proc.kill()
                raise RuntimeError(f"生成超时（{timeout}s）")
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
            if t == "phase" and on_tick:
                on_tick(None, str(msg.get("s") or ""))
            elif t == "pct" and on_tick:
                on_tick(float(msg.get("v") or 0), str(msg.get("s") or ""))
            elif t == "done":
                done_info = msg
            elif t == "error":
                err = str(msg.get("err") or "未知错误")
        rc = proc.wait(timeout=30)
    finally:
        try:
            log.close()
        except Exception:
            pass
    if err:
        raise RuntimeError("生成失败：" + err)
    if not done_info:
        tail = ""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-400:]
        except Exception:
            pass
        raise RuntimeError(f"引擎异常退出（rc={rc}）。日志尾部：{tail}")
    return done_info


# ---------------- canny 预处理（父进程 cv2） ----------------
def canny_ref(ref_path: str, w: int, h: int, low: int = 100, high: int = 200,
              tmp_dir: str = "") -> str:
    """参考图 → canny 控制图 PNG（白底黑线，lllyasviel 训练格式）。

    在父进程用 cv2 算（app 解释器有 cv2 5.0；ComfyUI 内嵌 python 没有）。
    读图走 PIL——cv2.imread 对中文路径不稳（Windows 代码页问题）。
    返回 PNG 路径；cv2 不可用时返回空串（调用方退化为无 ControlNet）。
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except Exception as e:
        _log(f"cv2 不可用，跳过 canny 预处理：{e}")
        return ""
    ref_path = os.path.abspath(ref_path)
    if not os.path.isfile(ref_path):
        raise ValueError("参考图不存在：" + ref_path)
    # PIL 读（Unicode 路径安全）→ RGB numpy → cv2 处理
    with Image.open(ref_path) as im:
        img = np.array(im.convert("RGB"))
    img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)          # 去噪点，少出杂边
    edges = cv2.Canny(gray, low, high)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))  # 控制网对细线不敏感
    out = 255 - edges                                  # 白底黑线
    tmp_dir = tmp_dir or os.path.join(BASE, "_out", "aidraw_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    p = os.path.join(tmp_dir, f"canny_{uuid.uuid4().hex[:8]}.png")
    cv2.imwrite(p, out)
    return p


# ---------------- 三个后端实现 ----------------
def _gen_agnes(prompt: str, negative: str = "", refs=None, seed: int = 0,
               w: int = 0, h: int = 0, out_dir: str = "", name: str = "",
               on_tick=None, timeout: int = 300, **kw) -> dict:
    """Agnes Image 2.5 Flash（API）。无种子参数——seed 只作记录。"""
    refs = [r for r in (refs or []) if r and os.path.isfile(r)][:3]
    stem = _safe_stem(name, "agnes")
    dst = os.path.join(out_dir, stem + ".png")
    size = "2K" if max(w or 0, h or 0) >= 1400 else "1K"
    ratio = _ratio_of(w, h)
    cmd = [AGNES_PY, AGNES_SCRIPT, "--prompt", prompt,
           "--size", size, "--ratio", ratio,
           "--out", out_dir, "--filename", stem + ".png"]
    for r in refs:
        cmd += ["--image", _datauri(r)]
    if on_tick:
        on_tick(5, "提交 Agnes 生成")
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    out = (p.stdout or "")
    if p.returncode != 0 or not os.path.isfile(dst):
        tail = (p.stderr or out)[-400:]
        raise RuntimeError(f"Agnes 生成失败（rc={p.returncode}）：{tail}")
    aw, ah = _img_size(dst)
    if on_tick:
        on_tick(95, "图片已保存")
    return {"ok": True, "file": dst, "backend": "agnes", "seed": seed or 0,
            "w": aw, "h": ah, "elapsed": round(time.time() - t0, 1),
            "note": f"Agnes Image 2.5 Flash · {size} · {ratio}"}


def _gen_sd15(prompt: str, negative: str = "", refs=None, seed: int = 0,
              w: int = 0, h: int = 0, out_dir: str = "", name: str = "",
              lora: str = LORA_DEFAULT, lora_scale: float = DEFAULT_LORA_SCALE,
              control_strength: float = DEFAULT_CONTROL_STRENGTH,
              steps: int = 20, cfg: float = 7.0, on_tick=None,
              timeout: int = 900, **kw) -> dict:
    """本地 SD1.5 + LoRA + canny ControlNet（进程内直调 ComfyUI）。"""
    refs = [r for r in (refs or []) if r and os.path.isfile(r)]
    w = int(w or 512) // 8 * 8 or 512
    h = int(h or 512) // 8 * 8 or 512
    w = max(256, min(1024, w))
    h = max(256, min(1024, h))
    stem = _safe_stem(name, "sd15")
    dst = os.path.join(out_dir, stem + ".png")
    # canny 控制图：父进程预处理（首张参考图）
    ctrl = canny_ref(refs[0], w, h) if refs else ""
    cmd = [COMFY_PY, "-u", SDGEN,
           "--prompt", prompt, "--negative", negative or "",
           "--seed", str(seed or 0),
           "--width", str(w), "--height", str(h),
           "--steps", str(int(steps or 20)), "--cfg", str(float(cfg or 7.0)),
           "--out", dst, "--ckpt", CKPT_NAME,
           "--comfy-root", COMFY_APP_ROOT]
    if lora:
        cmd += ["--lora", lora, "--lora-scale", str(float(lora_scale or 0.8))]
    if ctrl:
        cmd += ["--control-image", ctrl, "--controlnet", CONTROLNET_NAME,
                "--controlnet-strength", str(float(control_strength or 0.65))]
    info = _run_protocol(cmd, SDGEN_LOG, on_tick, timeout)
    if not os.path.isfile(dst):
        raise RuntimeError("引擎说存了，但文件不在：" + dst)
    aw, ah = _img_size(dst)
    return {"ok": True, "file": dst, "backend": "sd15",
            "seed": int(info.get("seed") or seed or 0),
            "w": aw or w, "h": ah or h, "elapsed": 0,
            "note": f"SD1.5 · {w}×{h} · {steps} 步" +
                    (f" · LoRA {lora}" if lora else "") +
                    (" · canny 锁构图" if ctrl else "")}


_IMPLS = {"agnes": _gen_agnes, "sd15": _gen_sd15}


# ---------------- 统一入口 ----------------
def gen_image(backend: str = "auto", prompt: str = "", negative: str = "",
              refs=None, seed: int = 0, w: int = 0, h: int = 0,
              out_dir: str = "", name: str = "", lora: str = LORA_DEFAULT,
              lora_scale: float = DEFAULT_LORA_SCALE,
              control_strength: float = DEFAULT_CONTROL_STRENGTH,
              steps: int = 20, cfg: float = 7.0, on_tick=None,
              timeout: int = 0) -> dict:
    """出图一条龙。backend: agnes / sd15 / auto（按优先级降级）。

    refs: 参考图路径列表（sd15 用首张算 canny；agnes 最多传 3 张）。
    产物落 paint_dir()（或指定 out_dir），旁边写 sidecar JSON 留痕。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    backend = (backend or "auto").strip().lower()
    if backend not in _IMPLS and backend != "auto":
        raise ValueError(f"未知后端：{backend}（可选 {', '.join(_IMPLS)} / auto）")
    out_dir = out_dir or paint_dir()
    os.makedirs(out_dir, exist_ok=True)

    chain = BACKEND_ORDER if backend == "auto" else (backend,)
    last_err = None
    for bid in chain:
        if not backend_ready(bid):
            last_err = RuntimeError(f"后端 {bid} 未就绪")
            _log(str(last_err))
            continue
        kw = {}
        if timeout:
            kw["timeout"] = timeout      # 0 = 用各后端自己的默认超时
        try:
            res = _IMPLS[bid](prompt=prompt, negative=negative, refs=refs,
                              seed=seed, w=w, h=h, out_dir=out_dir, name=name,
                              lora=lora, lora_scale=lora_scale,
                              control_strength=control_strength,
                              steps=steps, cfg=cfg, on_tick=on_tick, **kw)
            meta = {"backend": bid, "prompt": prompt, "negative": negative,
                    "seed": res.get("seed"), "w": res.get("w"), "h": res.get("h"),
                    "refs": [os.path.abspath(r) for r in (refs or [])],
                    "lora": lora if bid == "sd15" else "",
                    "lora_scale": lora_scale if bid == "sd15" else 0,
                    "control_strength": control_strength if bid == "sd15" else 0,
                    "steps": steps, "cfg": cfg,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "note": res.get("note", "")}
            _write_sidecar(res["file"], meta)
            res["meta"] = meta
            return res
        except Exception as e:  # 换下一个后端
            last_err = e
            _log(f"后端 {bid} 失败，{'降级' if backend == 'auto' else '报错'}：{e}")
            if backend != "auto":
                raise
    raise RuntimeError(f"所有后端都失败了。最后错误：{last_err}")
