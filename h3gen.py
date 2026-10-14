#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FrameSeek · MiniMax H3 视频生成引擎（从 ComfyUI 提取的直调版）

不启动 ComfyUI 服务、不加载工作流 JSON、不走 HTTP——直接 import ComfyUI 的库，
按参考工作流 minimax_h3_textonly_4060ti.json 的同样顺序执行：

    UNETLoader → CLIPLoader(minimax) → VAELoader×2
    → MiniMaxH3ReferenceToVideo（提示词 → 条件 + 音视频联合 latent）
    → RandomNoise → KSamplerSelect(res_multistep) → BasicScheduler(simple, 12 步)
    → BasicGuider → SamplerCustomAdvanced
    → VAEDecode / VAEDecodeAudio → ffmpeg 合成 mp4

必须用 ComfyUI 便携版的 python_embeded 运行（自带 CUDA torch 与全部依赖）：

    python_embeded\\python.exe h3gen.py --prompt "..." --duration 5.2 --out x.mp4

关键环境复刻：服务端 main.py 在 import torch 前 aimdo.control.init()，加载模型前
init_devices() 并把 CoreModelPatcher 换成 ModelPatcherDynamic（DynamicVRAM）。
直调若跳过这一步，VAE/CLIP 落到静态 ModelPatcher：低显存下只部分上卡，
comfy.ops 的逐 op 搬运覆盖不到 ViT3DDecoder.register_tokens（裸 torch.cat 使用），
VAE 解码必崩 "Expected all tensors to be on same device ... cpu vs cuda:0"。
另外 main.py 还会在 import torch 前 import cuda_malloc（设
PYTORCH_CUDA_ALLOC_CONF=backend:cudaMallocAsync）——缺这一步，流式加载 + VAE
解码的大块激活会把原生分配器打碎，reserved 几个 G 却连 30MB 都分不出，直接 OOM。

stdout 只输出 JSON 行协议（父进程解析）：
    {"t":"phase","s":"加载扩散模型"}
    {"t":"pct","v":35.0,"s":"采样 5/12 步"}
    {"t":"done","path":"...","frames":124,"w":736,"h":416,"duration":5.17}
    {"t":"error","err":"..."}
其余日志一律走 stderr，不污染协议流。
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time

# ComfyUI 便携版根目录（含 comfy/ nodes.py folder_paths.py 的那层）
DEFAULT_COMFY_ROOT = os.environ.get(
    "FRAMESEEK_COMFY_ROOT",
    r"D:\project\AIProjects\ComfyUI_windows_portable\ComfyUI")

# 模型文件名（与参考工作流一致；改模型只改这里）
UNET_NAME = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
CLIP_NAME = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE_NAME = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE_NAME = "minimax_h3_audio_vae_fp32.safetensors"

# 参考工作流 ResolutionSelector 的宽高比表（16:9 / 0.3MP / 倍数 32）
ASPECTS = {
    "16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:3": (4, 3),
    "3:4": (3, 4), "2:3": (2, 3), "3:2": (3, 2), "21:9": (21, 9),
}
CANVAS_MULTIPLE = 32
FPS = 24
FRAMES_MIN, FRAMES_MAX = 124, 362   # 模型训练覆盖范围（≈5.2s ~ 15.1s）
PROMPT_MAX = 2000                    # MiniMax 官方提示词上限


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def phase(s):
    emit({"t": "phase", "s": s})


def pct(v, s):
    emit({"t": "pct", "v": round(float(v), 1), "s": s})


def fail(e):
    emit({"t": "error", "err": "%s: %s" % (type(e).__name__, e)})
    sys.exit(1)


def resolution(aspect, megapixels):
    """复刻 ResolutionSelector：按宽高比 + 目标像素数，取 32 的倍数。"""
    wr, hr = ASPECTS[aspect]
    scale = math.sqrt(megapixels * 1024 * 1024 / (wr * hr))
    w = round(wr * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE
    h = round(hr * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE
    return max(CANVAS_MULTIPLE, w), max(CANVAS_MULTIPLE, h)


def frame_count(duration):
    """复刻工作流 2021 节点的算式：秒 → 帧，向上对齐到 17k+5 栅格。"""
    n = max(5, round(duration * FPS))
    n += (5 - n % 17) % 17
    return max(FRAMES_MIN, min(FRAMES_MAX, n))


def load_ref_image(path):
    import numpy as np
    import torch
    from PIL import Image
    im = Image.open(path).convert("RGB")
    t = torch.from_numpy(np.array(im).astype(np.float32) / 255.0)
    return t[None]  # [1, H, W, 3]


def prompt_scope(patchers):
    """（保留备用）执行器 PromptModelTracker 的 pin 标记。

    历史误诊记录（2026-10-14）：曾以为直调时打这个 pin 会挡住
    unload_all_models 清场导致 VAE 解码崩——错。静态 patcher 上
    is_dynamic() 为假，pin 根本没打上。真正根因是没复刻 DynamicVRAM
    启动序列（见 main() 1b 节），VAE 落在静态 ModelPatcher 上，
    register_tokens 不在 comfy.ops 搬运范围。现在 CoreModelPatcher 已是
    动态版，整个跑批相当于一个 prompt，流式系统自行按需换入换出，
    不需要手动打 pin。
    """
    import comfy.model_patcher as MP
    marked = []
    for p in patchers:
        mp = getattr(p, "patcher", p)
        try:
            if isinstance(mp, MP.ModelPatcher) and mp.is_dynamic():
                mp.set_in_use_by_current_prompt(True)
                marked.append(mp)
        except Exception:
            pass
    return marked


def _release_gpu():
    """卸掉全部模型并清 CUDA 缓存，把整张卡留给下一阶段（VAE 解码）。"""
    import comfy.model_management
    import torch
    comfy.model_management.unload_all_models()
    torch.cuda.empty_cache()


def dbg_devices(vae, tag):
    """诊断：VAE 各参数落在哪、显存余量、已加载模型（--debug 时输出）。"""
    import comfy.memory_management
    import comfy.model_management
    import torch
    devs = {}
    for _, p in vae.first_stage_model.named_parameters():
        devs[str(p.device)] = devs.get(str(p.device), 0) + 1
    rt = getattr(getattr(vae.first_stage_model, "decoder", None),
                 "register_tokens", None)
    emit({"t": "dbg", "s": tag, "param_devices": devs,
          "register_tokens": str(rt.device) if rt is not None else "n/a",
          "patcher": type(vae.patcher).__name__,
          "dynamic": vae.patcher.is_dynamic(),
          "aimdo": comfy.memory_management.aimdo_enabled,
          "free_mb": round(torch.cuda.mem_get_info()[0] / 1048576, 1),
          "loaded": [type(x.model.model).__name__
                     for x in comfy.model_management.current_loaded_models]})


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--duration", type=float, default=5.2)
    ap.add_argument("--aspect", default="16:9", choices=sorted(ASPECTS))
    ap.add_argument("--megapixels", type=float, default=0.3)
    ap.add_argument("--steps", type=int, default=12)
    ap.add_argument("--sampler", default="res_multistep")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--comfy-root", default=DEFAULT_COMFY_ROOT)
    ap.add_argument("--debug", action="store_true", help="输出设备/显存诊断")
    a = ap.parse_args()
    DEBUG = a.debug

    if not a.prompt.strip():
        fail(ValueError("提示词不能为空"))
    if len(a.prompt) > PROMPT_MAX:
        fail(ValueError("提示词超过 %d 字符上限（MiniMax 官方限制）" % PROMPT_MAX))
    if a.aspect not in ASPECTS:
        fail(ValueError("不支持的宽高比：%s" % a.aspect))
    for rp in a.ref:
        if not os.path.isfile(rp):
            fail(ValueError("参考图不存在：%s" % rp))

    # ---------- 1. ComfyUI 库环境（不启服务） ----------
    root = os.path.abspath(a.comfy_root)
    for p in (root, os.path.join(root, "comfy_extras")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    # 复刻 4060Ti 8G 专用启动参数（文本编码器流式上卡、卸载不锁内存）
    import comfy.cli_args as cli_args
    cli_args.args.lowvram = True
    cli_args.args.reserve_vram = 0.8
    cli_args.args.disable_pinned_memory = True

    # ---------- 1b. DynamicVRAM 启动序列（复刻 main.py 58-70 + 251-282） ----------
    # 服务端在 import torch 之前就 aimdo.control.init()，加载模型前再
    # init_devices() 并把 CoreModelPatcher 换成 ModelPatcherDynamic。
    # 直调跳过这一步的后果：VAE/CLIP 用静态 ModelPatcher，低显存下只部分上卡，
    # comfy.ops 逐 op 搬运覆盖不到 ViT3DDecoder.register_tokens（裸 torch.cat），
    # VAE 解码必崩 "tensors is on cpu, different from other tensors on cuda:0"。
    import comfy_aimdo.control as aimdo  # noqa: E402
    _headroom = (int(cli_args.args.reserve_vram * 1024 ** 3)
                 if cli_args.args.reserve_vram else None)
    try:
        aimdo.init(simple_vram_headroom=_headroom, nvml_pressure=True)
    except TypeError:
        try:
            aimdo.init(simple_vram_headroom=_headroom)
        except TypeError:
            aimdo.init()

    # 复刻 main.py:100 —— import torch 前启用 cudaMallocAsync 分配器。
    # 缺这一步的后果：动态流式加载 + VAE 解码的大块激活把原生分配器打碎，
    # reserved 3.6G 却连 30MB 都分不出，直接 OOM（2026-10-14 踩过）。
    # cuda_malloc 模块会自行判定（torch2.x+CUDA+卡不在黑名单 → 设环境变量）。
    try:
        import cuda_malloc  # noqa: F401
    except Exception:
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "backend:cudaMallocAsync")

    phase("初始化 ComfyUI 运行环境")
    import torch  # noqa: E402
    import comfy.model_management as mm  # noqa: E402
    import comfy.memory_management  # noqa: E402
    import comfy.model_patcher  # noqa: E402
    import comfy.samplers  # noqa: E402
    import comfy.utils  # noqa: E402
    import nodes  # noqa: E402  （加载器 / 解码节点）
    import nodes_custom_sampler as CS  # noqa: E402
    import nodes_minimax_h3 as H3  # noqa: E402
    from nodes_audio import vae_decode_audio  # noqa: E402
    comfy.utils.set_progress_bar_enabled(False)

    # CoreModelPatcher 换动态版（main.py 251-282 行）：之后创建的
    # UNET/CLIP/VAE 全部走 ModelPatcherDynamic，与服务端同一条加载路径。
    try:
        _aimdo_ok = aimdo.init_devices(
            [(d.index, 0) for d in mm.get_all_torch_devices()])
    except TypeError:
        _aimdo_ok = aimdo.init_devices(
            [d.index for d in mm.get_all_torch_devices()])
    if _aimdo_ok:
        comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcherDynamic
        comfy.memory_management.aimdo_enabled = True
        emit({"t": "phase", "s": "DynamicVRAM 已启用（复刻服务端加载路径）"})
    else:
        emit({"t": "phase", "s": "警告：DynamicVRAM 初始化失败，退回静态加载"})

    if not torch.cuda.is_available():
        fail(RuntimeError("没有可用的 CUDA 设备，H3 生成需要 NVIDIA 显卡"))
    gpu = torch.cuda.get_device_name(0)
    emit({"t": "phase", "s": "GPU：%s（%.1fG）" % (gpu, torch.cuda.get_device_properties(0).total_memory / 1024 ** 3)})
    if mm.vram_state != mm.VRAMState.LOW_VRAM:
        emit({"t": "phase", "s": "提示：当前显存策略 %s" % mm.vram_state.name})

    w, h = resolution(a.aspect, a.megapixels)
    frames = frame_count(a.duration)

    # ---------- 2. 加载模型 ----------
    phase("加载扩散模型 MiniMax H3")
    model = nodes.UNETLoader().load_unet(UNET_NAME, "default")[0]
    pct(10, "加载文本编码器 Qwen3-VL")
    clip = nodes.CLIPLoader().load_clip(CLIP_NAME, "minimax", "default")[0]
    pct(14, "加载视频 / 音频 VAE")
    vae = nodes.VAELoader().load_vae(VAE_NAME)[0]
    audio_vae = nodes.VAELoader().load_vae(AUDIO_VAE_NAME)[0]

    # 注：此前怀疑过执行器的 set_in_use_by_current_prompt pin 挡住清场——
    # 误诊（静态 patcher 上 is_dynamic() 为假，pin 根本没打上）。
    # 真正根因是没复刻 DynamicVRAM 启动序列，见 1b 节。现在三个模型
    # （UNET/CLIP/VAE）都是动态 patcher，整个跑批相当于一个 prompt，
    # 流式系统自行按需换入换出，不需要手动打 pin。

    # ---------- 3. 提示词 → 条件 + 音视频 latent ----------
    ref_images = {}
    for i, rp in enumerate(a.ref[:9]):
        ref_images["ref_image_%d" % i] = load_ref_image(rp)
    pct(18, "编码提示词")
    res = H3.MiniMaxH3ReferenceToVideo.execute(
        clip, vae, audio_vae, a.prompt, w, h, frames,
        ref_image_size="match", ref_images=ref_images,
        ref_videos={}, ref_video_audios={}, ref_audios={})
    cond, latent = res.args[0], res.args[1]

    # ---------- 4. 采样（res_multistep / simple / 12 步） ----------
    pct(20, "准备采样")
    seed = a.seed if a.seed else int(time.time()) % (2 ** 32)
    noise = CS.Noise_RandomNoise(seed)
    sampler = comfy.samplers.sampler_object(a.sampler)
    sigmas = CS.BasicScheduler.execute(model, "simple", a.steps, 1.0).args[0]
    guider = CS.BasicGuider.execute(model, cond).args[0]

    # 包一层 guider.sample：拿到逐步回调，同时屏蔽内置预览/进度条
    orig_sample = guider.sample

    def sample_hook(noise_, latent_image, sampler_, sigmas_,
                    denoise_mask=None, callback=None, disable_pbar=False, seed=None):
        def cb(i, x0, x, total):
            v = 20 + 65 * (i / max(1, total - 1))
            pct(v, "采样 %d/%d 步" % (i + 1, total))
        return orig_sample(noise_, latent_image, sampler_, sigmas_,
                           denoise_mask=denoise_mask, callback=cb,
                           disable_pbar=True, seed=seed)

    guider.sample = sample_hook
    pct(20, "采样生成")
    res = CS.SamplerCustomAdvanced.execute(noise, guider, sampler, sigmas, latent)
    out_latent = res.args[0]

    # ---------- 5. 解码 ----------
    # 采样结束后 UNET/CLIP 仍占着显存，先整体卸载 + 清缓存给 VAE 腾地方。
    # 现在 CoreModelPatcher 是动态版（与服务端同路径），VAE 解码时
    # register_tokens 等参数由流式系统按需搬上卡（探针已验证）。
    # inference_mode 复刻执行器（execution.py:751 每个节点都包）——
    # 缺了它 ViT3D 解码器 12 层的中间激活全留在计算图里，显存必爆
    # （2026-10-14：15.29G 虚拟分配打穿 8G 物理，报 OOM）。
    _release_gpu()
    if DEBUG:
        dbg_devices(vae, "视频解码前")
    pct(88, "解码视频画面")
    with torch.inference_mode():
        images = nodes.VAEDecode().decode(vae, out_latent)[0]  # [T, H, W, 3] float
    _release_gpu()
    if DEBUG:
        dbg_devices(audio_vae, "音频解码前")
    pct(93, "解码音频")
    with torch.inference_mode():
        audio = vae_decode_audio(audio_vae, out_latent)  # {"waveform", "sample_rate"}

    # ---------- 6. ffmpeg 合成 mp4 ----------
    # 坑（2026-10-14）：便携版 python 没装 torchcodec，torchaudio.save 直接
    # ImportError。波形就是 [C, T] float32，用标准库 wave 写 16-bit PCM 即可。
    pct(96, "合成 mp4")
    import wave
    import numpy as np
    T, H, W = images.shape[0], images.shape[1], images.shape[2]
    raw = (images.clamp(0, 1) * 255).to(torch.uint8).cpu().numpy().tobytes()
    wav = os.path.join(tempfile.mkdtemp(prefix="h3gen_"), "audio.wav")
    wf = audio["waveform"][0].cpu().numpy()          # [C, T] 或 [T]
    if wf.ndim == 1:
        wf = wf[None, :]
    pcm = (np.clip(wf.T, -1.0, 1.0) * 32767.0).astype("<i2")  # [T, C]
    with wave.open(wav, "wb") as f:
        f.setnchannels(pcm.shape[1])
        f.setsampwidth(2)
        f.setframerate(int(audio["sample_rate"]))
        f.writeframes(pcm.tobytes())
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    cmd = [a.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(FPS), "-i", "pipe:0",
           "-i", wav, "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
           "-preset", "medium", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", "-shortest", a.out]
    p = subprocess.run(cmd, input=raw, capture_output=True)
    try:
        os.remove(wav)
    except OSError:
        pass
    if p.returncode != 0 or not os.path.isfile(a.out):
        fail(RuntimeError("ffmpeg 合成失败：%s" %
                          (p.stderr.decode("utf-8", "replace")[-500:])))
    emit({"t": "done", "path": os.path.abspath(a.out), "frames": int(T),
          "w": int(W), "h": int(H), "duration": round(T / FPS, 2), "seed": seed})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        fail(e)
