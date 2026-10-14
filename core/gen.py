# -*- coding: utf-8 -*-
"""MiniMax H3 视频生成引擎（h3gen 子进程直调 ComfyUI 库）。

架构（2026-10-14 重构，取代旧的 ComfyUI HTTP 方案）：
不启动 ComfyUI 服务、不走 HTTP/WebSocket、不加载工作流 JSON。生成时直接拉起
`项目根/h3gen.py` 子进程（用 ComfyUI 便携版的 python_embeded），它 import
ComfyUI 的库函数，按参考工作流 minimax_h3_textonly_4060ti.json 的同样顺序
执行：UNETLoader → CLIPLoader(minimax) → VAELoader×2 →
MiniMaxH3ReferenceToVideo → res_multistep 采样 12 步 → 解码 → ffmpeg 合成 mp4。

对外只保留两件事：
- generate()：一条龙（生成 → 落 <项目根>/生成/），进度走 on_tick 回调；
- 参数硬约束：prompt < 2000 字符；帧数 ≡ 5 (mod 17) 且 124~362 帧
  （约 5.2~15.1 秒 @24fps，训练覆盖范围）；参考图用 `<Picture 1>` 引用。

落点：成片写 <项目根>/生成/，**不进素材库**（2026-10-15 改）——
素材库只放用户自己的原始素材，生成物单独一档，互不污染。

子进程协议（h3gen.py stdout，一行一个 JSON）：
    {"t":"phase","s":"..."} / {"t":"pct","v":35,"s":"采样 5/12 步"}
    {"t":"done","path":...} / {"t":"error","err":...}
其余日志全在子进程 stderr，落到 _out/h3gen_last.log。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------- 路径 / 模型 ----------------
COMFY_ROOT = os.environ.get(
    "FRAMESEEK_COMFY_ROOT", r"D:\project\AIProjects\ComfyUI_windows_portable")
# h3gen 的 --comfy-root 要的是含 comfy/ nodes.py 的那层
COMFY_APP_ROOT = (os.path.join(COMFY_ROOT, "ComfyUI")
                  if os.path.isdir(os.path.join(COMFY_ROOT, "ComfyUI"))
                  else COMFY_ROOT)
COMFY_PY = os.path.join(COMFY_ROOT, "python_embeded", "python.exe")
H3GEN = os.path.join(BASE, "h3gen.py")
H3GEN_LOG = os.path.join(BASE, "_out", "h3gen_last.log")

ASPECTS = ["16:9 (Widescreen)", "9:16 (Portrait Widescreen)", "1:1 (Square)",
           "4:3 (Standard)", "3:4 (Portrait Standard)",
           "2:3 (Portrait Photo)", "3:2 (Photo)", "21:9 (Ultrawide)"]

MAX_PROMPT = 2000          # 官方上限
FPS = 24
FRAME_MIN, FRAME_MAX = 124, 362   # 训练覆盖帧数范围（17k+5 栅格）
DEFAULT_MEGAPIXELS = 0.3   # 模板值：16:9 约 736×416，8G 卡跑得动
DEFAULT_DURATION = 5.0


def _log(msg: str):
    """只写 stderr——MCP stdio 的 stdout 归 JSON-RPC 协议，不能污染。"""
    print(f"[frameseek-gen] {msg}", file=sys.stderr)


# ---------------- 参数换算 ----------------
def frames_for(duration: float):
    """秒 → 帧数。与工作流 2021 节点同式：向上对齐到 17k+5，再钳到训练范围。"""
    d = float(duration or DEFAULT_DURATION)
    d = max(1.0, min(20.0, d))
    n = max(5, round(d * FPS))
    n += (5 - n % 17) % 17
    frames = max(FRAME_MIN, min(FRAME_MAX, n))
    return frames, round(frames / FPS, 2)


def engine_ready() -> bool:
    """引擎三件套是否就位（embeded python / h3gen.py / ComfyUI 库目录）。"""
    return (os.path.isfile(COMFY_PY) and os.path.isfile(H3GEN)
            and os.path.isdir(COMFY_APP_ROOT))


def engine_info() -> dict:
    """给 MCP / Web 展示的引擎现状（不再有"服务起没起"的概念）。"""
    missing = [p for p in (COMFY_PY, H3GEN) if not os.path.isfile(p)]
    if not os.path.isdir(COMFY_APP_ROOT):
        missing.append(COMFY_APP_ROOT)
    return {
        "engine": "h3gen-direct",
        "note": "无 ComfyUI 服务；生成时直调 ComfyUI 库（子进程），跑完即退",
        "ready": not missing,
        "missing": missing,
        "interpreter": COMFY_PY,
        "script": H3GEN,
        "comfy_root": COMFY_APP_ROOT,
        "log": H3GEN_LOG,
    }


# ---------------- 生成后处理 ----------------
def safe_name(name: str) -> str:
    """文件名安全化：去路径、去非法字符、限长。"""
    n = os.path.basename((name or "").strip().strip("\"'"))
    n = re.sub(r"[\\/:*?\"<>|]+", "_", n).strip(" .")
    return n[:80]


def output_dir() -> str:
    """AI 生成成片的落点：<项目根>/生成/。

    2026-10-15 改：产物不再落素材库，改落 FrameSeek 自己的项目目录下——
    素材库只放用户自己的原始素材，生成物单独一档，互不污染。
    目录可用 hub 设置 gen_outdir 改（绝对路径）。
    """
    d = ""
    try:
        from core.hub import HUB
        d = HUB.get_setting("gen_outdir", "").strip()
    except Exception:
        d = ""
    d = os.path.abspath(d) if d else os.path.join(BASE, "生成")
    os.makedirs(d, exist_ok=True)
    return d


def dest_path(root: str = "", name: str = "") -> str:
    """成片落点：output_dir() 下；同名自动 _1/_2 递增，不覆盖。

    root 参数保留只是为了兼容旧调用（现在不再用它定位落点）。
    """
    d = output_dir()
    fn = safe_name(name) or time.strftime("H3_%Y%m%d_%H%M%S")
    if not fn.lower().endswith(".mp4"):
        fn += ".mp4"
    p = os.path.join(d, fn)
    i = 1
    while os.path.exists(p):
        stem = fn[:-4]
        p = os.path.join(d, f"{stem}_{i}.mp4")
        i += 1
    return p


# ---------------- 一体化入口 ----------------
def generate(prompt: str, duration: float = DEFAULT_DURATION,
             aspect: str = "16:9 (Widescreen)",
             megapixels: float = DEFAULT_MEGAPIXELS, seed: int = 0,
             name: str = "", ref_image: str = "", index: str = "none",
             root: str = "", on_tick=None, timeout: int = 5400) -> dict:
    """一条龙：h3gen 子进程生成 → 落 <项目根>/生成/。

    on_tick(done:int, message:str)：进度回调（done 0~100 是百分比）。
    root：素材库路径，仅用于日志/统计归属，成片不落库（2026-10-15 起）。
    index：保留兼容参数，现已停用——产物在项目目录、不进素材库索引，
           传什么都只出文件。
    返回 {ok, file, root, frames, seconds, seed, ...}。
    """

    def tick(msg: str, pct_: int = None):
        if on_tick:
            try:
                on_tick(pct_, msg)
            except Exception:
                pass

    root = os.path.abspath(root) if root else ""
    if index not in ("asr", "scan", "none"):
        raise ValueError("index 只支持 asr / scan / none")
    if not engine_ready():
        raise RuntimeError("生成引擎未就绪，缺：" +
                           ", ".join(engine_info()["missing"]))
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    if len(prompt) > MAX_PROMPT:
        raise ValueError(f"prompt 超长（{len(prompt)} > {MAX_PROMPT} 字符上限）")
    if aspect not in ASPECTS:
        raise ValueError(f"不支持的宽高比：{aspect}（可选 {', '.join(ASPECTS)}）")
    if ref_image:
        ref_image = os.path.abspath(ref_image)
        if not os.path.isfile(ref_image):
            raise ValueError("参考图不存在：" + ref_image)
        if "<Picture" not in prompt:
            prompt = (prompt.rstrip() + " Use <Picture 1> as reference frame, "
                      "keeping character identity and scene style consistent.")

    t0 = time.time()
    frames, seconds = frames_for(duration)
    seed = int(seed or 0) or uuid.uuid4().int % (2 ** 31)

    # 成片直接生成到项目产物目录，省一次拷贝
    dst = dest_path(name=name)

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [COMFY_PY, "-u", H3GEN,
           "--prompt", prompt,
           "--duration", str(float(duration)),
           "--aspect", aspect.split(" ")[0],
           "--megapixels", str(float(megapixels)),
           "--steps", "12",
           "--seed", str(seed),
           "--out", dst,
           "--ffmpeg", ffmpeg,
           "--comfy-root", COMFY_APP_ROOT]
    if ref_image:
        cmd += ["--ref", ref_image]

    os.makedirs(os.path.dirname(H3GEN_LOG), exist_ok=True)
    log = open(H3GEN_LOG, "a", encoding="utf-8", errors="replace")
    _log("启动 h3gen: " + " ".join(repr(c) if " " in c else c for c in cmd[:12]) + " ...")
    proc = subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE,
                            stderr=log, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    tick("启动生成引擎", 1)

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
            if t == "phase":
                tick(str(msg.get("s") or ""))
            elif t == "pct":
                # 引擎自身 0~100，压到 0~92，尾部留给收尾
                v = float(msg.get("v") or 0)
                tick(str(msg.get("s") or "生成中"), int(v * 0.92))
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
            with open(H3GEN_LOG, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-400:]
        except Exception:
            pass
        raise RuntimeError(f"引擎异常退出（rc={rc}）。日志尾部：{tail}")
    if not os.path.isfile(dst):
        raise RuntimeError("引擎说存了，但文件不在：" + dst)
    size_mb = round(os.path.getsize(dst) / 1048576, 2)
    tick("成片已生成", 92)

    # 不入库：产物在项目目录 <BASE>/生成/，素材库索引不收（2026-10-15 起）
    tick("完成", 100)
    return {"ok": True, "file": dst, "root": root,
            "video_id": 0, "lines": 0,
            "frames": int(done_info.get("frames") or frames),
            "seconds": float(done_info.get("duration") or seconds),
            "w": int(done_info.get("w") or 0),
            "h": int(done_info.get("h") or 0),
            "seed": seed, "aspect": aspect,
            "megapixels": float(megapixels), "size_mb": size_mb,
            "prompt": prompt, "ref_image": ref_image or "",
            "out_dir": os.path.dirname(dst),
            "index": "none", "elapsed": round(time.time() - t0, 1)}
