# -*- coding: utf-8 -*-
"""视频生成引擎，**双后端可插拔**（2026-10-18 加 Agnes，与生图 img_backend 同构）。

    agnes   Agnes Video 2.5 Flash（云端 API，默认优先）
            1280x704、4~12 秒、**成片自带音轨**、数十秒~几分钟、需联网。
            免费账号高峰可能报 503 video_queue_full（skill 脚本内部会退避重试）。
            提示词直接写中文即可。
    h3     本地 MiniMax H3（h3gen 子进程直调 ComfyUI 库，离线兜底）
            736x416（随 megapixels 档变）、3~15 秒、12 步采样。
            慢（5 秒约 10 分钟、10 秒约 40 分钟），**独占 GPU**，两个 H3 串行。
            提示词英文效果最好。

H3 架构（2026-10-14 重构，取代旧的 ComfyUI HTTP 方案）：
不启动 ComfyUI 服务、不走 HTTP/WebSocket、不加载工作流 JSON。生成时直接拉起
`项目根/h3gen.py` 子进程（用 ComfyUI 便携版的 python_embeded），它 import
ComfyUI 的库函数，按参考工作流 minimax_h3_textonly_4060ti.json 的同样顺序
执行：UNETLoader → CLIPLoader(minimax) → VAELoader×2 →
MiniMaxH3ReferenceToVideo → res_multistep 采样 12 步 → 解码 → ffmpeg 合成 mp4。

对外只保留三件事：
- generate()：一条龙（生成 → 落 <项目根>/AI生成/），进度走 on_tick 回调；
- video_backends() / backend_ready()：给面板摸底各后端就绪状态；
- 参数硬约束：prompt < 2000 字符；H3 帧数 ≡ 5 (mod 17) 且 124~362 帧
  （约 5.2~15.1 秒 @24fps，训练覆盖范围）；参考图用 `<Picture 1>` 引用。

落点：成片写 <项目根>/AI生成/，**不进素材库**（2026-10-15 改）——
素材库只放用户自己的原始素材，生成物单独一档，互不污染。

子进程协议（h3gen.py stdout，一行一个 JSON）：
    {"t":"phase","s":"..."} / {"t":"pct","v":35,"s":"采样 5/12 步"}
    {"t":"done","path":...} / {"t":"error","err":...}
其余日志全在子进程 stderr，落到 _out/h3gen_last.log。
（Agnes 那条线不走这个协议——skill 脚本是提交+轮询+下载一体，直接 run 阻塞。）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 生成产物目录名（项目根下）。2026-10-15 从「生成」改成「AI生成」，
# 老目录由 _migrate_legacy_dir() 自动并入，见 output_dir()。
OUT_DIRNAME = "AI生成"

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

# Agnes 侧的宽高比（自己那套字符串，跟 H3 的带括号写法不一样）
AGNES_ASPECTS = {"16:9 (Widescreen)": "16:9",
                 "9:16 (Portrait Widescreen)": "9:16",
                 "1:1 (Square)": "1:1",
                 "4:3 (Standard)": "4:3",
                 "3:4 (Portrait Standard)": "3:4",
                 "21:9 (Ultrawide)": "21:9"}
# 2:3 / 3:2 Agnes 不支持，传过去会 400
AGNES_UNSUPPORTED = ("2:3 (Portrait Photo)", "3:2 (Photo)")

# Agnes Video 2.5 Flash skill 脚本（API 生视频）
AGNES_SCRIPT = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills", "agnes-video-gen",
    "scripts", "agnes_video.py")

MAX_PROMPT = 2000          # 官方上限
FPS = 24
FRAME_MIN, FRAME_MAX = 124, 362   # 训练覆盖帧数范围（17k+5 栅格）
DEFAULT_MEGAPIXELS = 0.3   # 模板值：16:9 约 736×416，8G 卡跑得动
DEFAULT_DURATION = 5.0

# 后端优先级：auto = agnes → h3 依次降级（与生图 img_backend 同构）
BACKEND_ORDER = ("agnes", "h3")
AGNES_SECONDS_MIN, AGNES_SECONDS_MAX = 4, 12


def _log(msg: str):
    """只写 stderr——MCP stdio 的 stdout 归 JSON-RPC 协议，不能污染。"""
    print(f"[frameseek-gen] {msg}", file=sys.stderr)


# H3 独占 GPU，同一时刻只能跑一个。两个后端可并发，但两个 H3 必须串行，
# 所以锁放在后端内部而不是 MCP 层（云端 agnes 不占 GPU，不该被它挡住）。
_H3_LOCK = threading.Lock()


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


def agnes_ready() -> bool:
    """Agnes Video 2.5 后端是否就绪：脚本在 + 有 API Key。"""
    if not os.path.isfile(AGNES_SCRIPT):
        return False
    if (os.environ.get("AGNES_API_KEY") or "").strip():
        return True
    try:
        cfg = os.path.join(os.path.expanduser("~"), ".workbuddy", "models.json")
        if not os.path.isfile(cfg):
            return False
        with open(cfg, encoding="utf-8") as f:
            raw = json.load(f)
        items = raw if isinstance(raw, list) else (raw or {}).get("models", [])
        for m in items:
            if isinstance(m, dict) and m.get("apiKey") and \
                    "agnes" in f"{m.get('id', '')}{m.get('url', '')}".lower():
                return True
    except Exception:
        return False
    return False


def video_backends() -> list:
    """视频后端列表（面板下拉框直接用）。与 img_backend.backends() 同构。"""
    items = [{
        "id": "agnes", "name": "Agnes Video 2.5 Flash（API）",
        "ready": agnes_ready(),
        "note": "1280x704 · 4~12 秒 · 数十秒~几分钟 · 需联网 · "
                "免费账号高峰可能排队（video_queue_full）",
        "eta": "约 1~5 分钟", "offline": False},
        {"id": "h3", "name": "MiniMax H3（本地）",
        "ready": engine_ready(),
        "note": "736x416 · 3~15 秒 · 12 步采样 · 纯离线 · 慢（10 秒片约 40 分钟）",
        "eta": "5 秒约 10 分钟，长片更久", "offline": True}]
    return items


def engine_info() -> dict:
    """给 MCP / Web 展示的引擎现状（不再有"服务起没起"的概念）。"""
    missing = [p for p in (COMFY_PY, H3GEN) if not os.path.isfile(p)]
    if not os.path.isdir(COMFY_APP_ROOT):
        missing.append(COMFY_APP_ROOT)
    return {
        "engine": "multi-backend",
        "note": "视频生成双后端：Agnes Video 2.5 Flash（云端，默认）"
                " / 本地 MiniMax H3（离线兜底）",
        "ready": engine_ready() or agnes_ready(),
        "missing": missing,
        "backends": video_backends(),
        "backend_order": list(BACKEND_ORDER),
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


def _migrate_legacy_dir() -> None:
    """把老的「生成」目录并进「AI生成」。

    两种情形：
    1. 老目录在、新目录不在 → 整目录改名（索引里存的是相对路径，跟着搬，不用改库）；
    2. 两个都在（老目录被资源管理器之类占着改不了名，内容已先搬走）→
       把残留文件挪过去，再试着删掉老目录；删不掉（还有句柄）就留着，
       下次调用再试——自愈，不报错。
    """
    old = os.path.join(BASE, "生成")
    new = os.path.join(BASE, OUT_DIRNAME)
    if not os.path.isdir(old) or os.path.normcase(old) == os.path.normcase(new):
        return
    try:
        if not os.path.exists(new):
            os.rename(old, new)
            return
    except Exception:
        pass
    try:
        for fn in os.listdir(old):
            if fn == ".frameseek":        # 索引缓存：搬不动就跳过，删的时候一起走
                continue
            s, d = os.path.join(old, fn), os.path.join(new, fn)
            if os.path.exists(d):
                continue
            try:
                shutil.rename(s, d)
            except Exception:
                pass
        shutil.rmtree(old, ignore_errors=True)   # 只剩 .frameseek 时整目录删掉
    except Exception:
        pass


def output_dir() -> str:
    """AI 生成成片的落点：<项目根>/AI生成/。

    2026-10-15 改：产物不再落素材库，改落 FrameSeek 自己的项目目录下——
    素材库只放用户自己的原始素材，生成物单独一档，互不污染。
    目录名从「生成」改成「AI生成」：项目目录下一堆中文文件夹，
    「AI生成」一眼知道是放什么的。
    目录可用 hub 设置 gen_outdir 改（绝对路径）。
    """
    _migrate_legacy_dir()
    d = ""
    try:
        from core.hub import HUB
        d = HUB.get_setting("gen_outdir", "").strip()
    except Exception:
        d = ""
    d = os.path.abspath(d) if d else os.path.join(BASE, OUT_DIRNAME)
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
def _gen_h3(prompt: str, duration: float = DEFAULT_DURATION,
            aspect: str = "16:9 (Widescreen)",
            megapixels: float = DEFAULT_MEGAPIXELS, seed: int = 0,
            name: str = "", ref_image: str = "", index: str = "none",
            root: str = "", on_tick=None, timeout: int = 5400) -> dict:
    """H3 入口：拿 GPU 锁后跑 _gen_h3_run()。

    锁是必需的——H3 独占显存，两个同时起会 OOM。放在后端内部而非 MCP 层，
    这样云端 agnes 任务可以跟 H3 并行跑，不被它挡住。
    """
    if not _H3_LOCK.acquire(timeout=3):
        raise RuntimeError("本地 H3 已有生成任务在跑（GPU 独占），"
                           "稍后再试；或改用 backend=agnes 走云端")
    try:
        return _gen_h3_run(prompt=prompt, duration=duration, aspect=aspect,
                           megapixels=megapixels, seed=seed, name=name,
                           ref_image=ref_image, index=index, root=root,
                           on_tick=on_tick, timeout=timeout)
    finally:
        _H3_LOCK.release()


def _gen_h3_run(prompt: str, duration: float = DEFAULT_DURATION,
                aspect: str = "16:9 (Widescreen)",
                megapixels: float = DEFAULT_MEGAPIXELS, seed: int = 0,
                name: str = "", ref_image: str = "", index: str = "none",
                root: str = "", on_tick=None, timeout: int = 5400) -> dict:
    """MiniMax H3 一条龙：h3gen 子进程生成 → 落 <项目根>/AI生成/。

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


def _probe_video(path: str):
    """ffprobe 读成片时长/分辨率，失败返回 (0, 0, 0)。"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration:stream=width,height",
             "-of", "default=noprint_wrappers=1:nokey=0", path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
        w = h = 0
        sec = 0.0
        for ln in (p.stdout or "").splitlines():
            ln = ln.strip()
            if ln.startswith("width="):
                w = int(ln.split("=", 1)[1] or 0)
            elif ln.startswith("height="):
                h = int(ln.split("=", 1)[1] or 0)
            elif ln.startswith("duration="):
                sec = float(ln.split("=", 1)[1] or 0)
        return sec, w, h
    except Exception:
        return 0.0, 0, 0


def _gen_agnes(prompt: str, duration: float = DEFAULT_DURATION,
               aspect: str = "16:9 (Widescreen)",
               megapixels: float = 0, seed: int = 0, name: str = "",
               ref_image: str = "", index: str = "none", root: str = "",
               on_tick=None, timeout: int = 900) -> dict:
    """Agnes Video 2.5 Flash 一条龙（云端 API，走 skill 脚本子进程）。

    与 H3 的差异：成片自带音轨、1280x704、4~12 秒、需联网；
    不吃 megapixels/seed（seed 只作记录）。免费账号高峰可能 503
    video_queue_full——脚本内部会退避重试 6 次，仍失败才抛错。
    """
    if not agnes_ready():
        raise RuntimeError("Agnes Video 2.5 未就绪：缺脚本或 API Key"
                           "（AGNES_API_KEY 或 ~/.workbuddy/models.json）")
    ratio = AGNES_ASPECTS.get(aspect)
    if not ratio:
        raise ValueError(f"Agnes 不支持该宽高比：{aspect}"
                         f"（可用 {', '.join(AGNES_ASPECTS.values())}）")
    secs = int(round(float(duration or DEFAULT_DURATION)))
    secs = max(AGNES_SECONDS_MIN, min(AGNES_SECONDS_MAX, secs))

    stem = safe_name(name) or f"视频_{time.strftime('%Y%m%d_%H%M%S')}"
    dst = dest_path(name=stem)          # dest_path 统一补 .mp4 + 同名递增

    mode, extra = "text", []
    if ref_image:
        ref_image = os.path.abspath(ref_image)
        if not os.path.isfile(ref_image):
            raise ValueError("参考图不存在：" + ref_image)
        mode = "reference"
        extra = ["--image", ref_image]

    cmd = [sys.executable, AGNES_SCRIPT,
           "--prompt", prompt,
           "--mode", mode,
           "--seconds", str(secs),
           "--aspect-ratio", ratio,
           "--out", os.path.dirname(dst),
           "--filename", os.path.basename(dst),
           "--timeout", str(int(timeout))] + extra
    if seed:
        cmd += ["--seed", str(int(seed))]

    _log("启动 Agnes Video 2.5: " + " ".join(
        c if " " not in c else repr(c) for c in cmd[:8]) + " ...")
    t0 = time.time()
    # Agnes 脚本是「提交+轮询+下载」一体、输出走 stdout，没有 JSON 行协议，
    # 所以用 run 阻塞 + 伪进度：提交后按时间轴推到 90，再收尾。
    if on_tick:
        on_tick(3, "提交 Agnes 任务（排队中，服务繁忙时会自动退避重试）")
    try:
        p = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout + 120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Agnes 生成超时（{timeout}s）")
    if on_tick:
        on_tick(92, "下载成片")
    if p.returncode != 0 or not os.path.isfile(dst):
        tail = ((p.stderr or "") + (p.stdout or ""))[-500:]
        raise RuntimeError(f"Agnes 生成失败（rc={p.returncode}）：{tail.strip()}")

    sec, w, h = _probe_video(dst)
    size_mb = round(os.path.getsize(dst) / 1048576, 2)
    tick = on_tick
    if tick:
        tick("完成", 100)
    return {"ok": True, "file": dst, "root": root,
            "video_id": 0, "lines": 0,
            "frames": int(sec * FPS) if sec else 0,
            "seconds": sec or float(secs),
            "w": w, "h": h,
            "seed": int(seed or 0), "aspect": aspect,
            "megapixels": 0, "size_mb": size_mb,
            "prompt": prompt, "ref_image": ref_image or "",
            "out_dir": os.path.dirname(dst),
            "index": "none", "elapsed": round(time.time() - t0, 1),
            "backend": "agnes",
            "note": f"Agnes Video 2.5 Flash · {ratio} · {secs}s · {w}x{h}"}


_IMPLS = {"agnes": _gen_agnes, "h3": _gen_h3}


def backend_ready(bid: str) -> bool:
    """后端是否就绪。未知 id 一律 False——**不能默认落到 h3 分支**，
    否则面板会显示一个不存在的后端「就绪」。"""
    if bid == "agnes":
        return agnes_ready()
    if bid == "h3":
        return engine_ready()
    return False


def generate(prompt: str, duration: float = DEFAULT_DURATION,
             aspect: str = "16:9 (Widescreen)",
             megapixels: float = DEFAULT_MEGAPIXELS, seed: int = 0,
             name: str = "", ref_image: str = "", index: str = "none",
             root: str = "", backend: str = "auto", on_tick=None,
             timeout: int = 0) -> dict:
    """出视频一条龙。backend: agnes / h3 / auto（按 BACKEND_ORDER 降级）。

    backend=auto 时优先云端 Agnes（快、清晰），失败才回落到本地 H3；
    显式指定 backend 则只跑那一个，不达标直接报错（不静默换引擎）。
    返回 {ok, file, backend, seconds, w, h, ...}。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    if len(prompt) > MAX_PROMPT:
        raise ValueError(f"prompt 超长（{len(prompt)} > {MAX_PROMPT} 字符上限）")
    if aspect not in ASPECTS:
        raise ValueError(f"不支持的宽高比：{aspect}（可选 {', '.join(ASPECTS)}）")
    if ref_image and not os.path.isfile(ref_image):
        # 提前挡：不然 auto 模式会白跑一遍 agnes 才发现图不存在
        raise ValueError("参考图不存在：" + ref_image)
    backend = (backend or "auto").strip().lower()
    if backend not in _IMPLS and backend != "auto":
        raise ValueError(f"未知后端：{backend}"
                         f"（可选 {', '.join(_IMPLS)} / auto）")

    chain = BACKEND_ORDER if backend == "auto" else (backend,)
    last_err = None
    for bid in chain:
        if not backend_ready(bid):
            last_err = RuntimeError(f"后端 {bid} 未就绪")
            _log(str(last_err))
            continue
        kw = {"timeout": timeout} if timeout else {}
        try:
            res = _IMPLS[bid](prompt=prompt, duration=duration, aspect=aspect,
                              megapixels=megapixels, seed=seed, name=name,
                              ref_image=ref_image, index=index, root=root,
                              on_tick=on_tick, **kw)
            res.setdefault("backend", bid)
            if bid == "h3":
                res["note"] = f"MiniMax H3 · {aspect} · {megapixels}MP"
            return res
        except Exception as e:
            last_err = e
            _log(f"视频后端 {bid} 失败，"
                 f"{'降级' if backend == 'auto' else '报错'}：{e}")
            if backend != "auto":
                raise
    raise RuntimeError(f"所有视频后端都失败了。最后错误：{last_err}")
