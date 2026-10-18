#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FrameSeek · SD1.5 出图引擎（进程内直调 ComfyUI 库，仿 h3gen.py 的启动序列）

不启动 ComfyUI 服务、不走 HTTP——直接 import ComfyUI 的库，按标准 SD1.5
出图节点图执行：

    CheckpointLoaderSimple（UNET+CLIP+VAE 一体）
    → [LoraLoader]（线稿 LoRA，可选）
    → CLIPTextEncode ×2（正向 / 负向）
    → [ControlNetApplyAdvanced]（canny 控制图锁构图，可选）
    → EmptyLatentImage → KSampler → VAEDecode → PIL 存 PNG

canny 控制图由**父进程**用 cv2 预先算好（--control-image 传入 PNG），
本进程不依赖 cv2——ComfyUI 内嵌 python 里没装。

必须用 ComfyUI 便携版的 python_embeded 运行：

    python_embeded\\python.exe sdgen.py --prompt "..." --out x.png

启动序列与 h3gen.py 完全一致（DynamicVRAM / cudaMallocAsync / 动态
ModelPatcher）——直调跳过这一步的后果见 h3gen.py 1b 节注释。

stdout 只输出 JSON 行协议（父进程解析）：
    {"t":"phase","s":"加载模型"}
    {"t":"pct","v":35.0,"s":"采样中"}
    {"t":"done","path":"...","w":512,"h":512,"seed":42}
    {"t":"error","err":"..."}
其余日志一律走 stderr，不污染协议流。
"""

import argparse
import json
import os
import sys
import time

# ComfyUI 便携版根目录（含 comfy/ nodes.py folder_paths.py 的那层）
DEFAULT_COMFY_ROOT = os.environ.get(
    "FRAMESEEK_COMFY_ROOT",
    r"D:\project\AIProjects\ComfyUI_windows_portable\ComfyUI")

# 模型文件名（与 models/ 下实际文件对应；改模型只改这里）
CKPT_NAME = "v1-5-pruned-emaonly.safetensors"
CONTROLNET_NAME = "diffusion_pytorch_model.fp16.safetensors"
LORA_NAME = "simple-lineart_v6.safetensors"

PROMPT_MAX = 2000


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def phase(s):
    emit({"t": "phase", "s": s})


def pct(v, s):
    emit({"t": "pct", "v": round(float(v), 1), "s": s})


def fail(e):
    emit({"t": "error", "err": "%s: %s" % (type(e).__name__, e)})
    sys.exit(1)


def load_image_tensor(path: str):
    """PIL 读图 → [1, H, W, 3] float tensor（ComfyUI IMAGE 格式）。

    不走 nodes.LoadImage——那个节点只认 ComfyUI input/ 目录内的文件，
    传绝对路径会报 "Invalid file path"。h3gen.py 的 load_ref_image 同款。
    """
    import numpy as np
    import torch
    from PIL import Image
    im = Image.open(path).convert("RGB")
    t = torch.from_numpy(np.array(im).astype(np.float32) / 255.0)
    return t[None]


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--negative", default="")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--width", type=int, default=512)
    ap.add_argument("--height", type=int, default=512)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--cfg", type=float, default=7.0)
    ap.add_argument("--sampler", default="euler")
    ap.add_argument("--scheduler", default="normal")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ckpt", default=CKPT_NAME)
    ap.add_argument("--lora", default="", help="LoRA 文件名（models/loras/ 下），空串不用")
    ap.add_argument("--lora-scale", type=float, default=0.8)
    ap.add_argument("--controlnet", default=CONTROLNET_NAME)
    ap.add_argument("--control-image", default="",
                    help="canny 控制图 PNG（父进程用 cv2 预处理好），空串不用 ControlNet")
    ap.add_argument("--controlnet-strength", type=float, default=0.65)
    ap.add_argument("--comfy-root", default=DEFAULT_COMFY_ROOT)
    a = ap.parse_args()

    if not a.prompt.strip():
        fail(ValueError("提示词不能为空"))
    if len(a.prompt) > PROMPT_MAX:
        fail(ValueError("提示词超过 %d 字符上限" % PROMPT_MAX))
    if a.width % 8 or a.height % 8:
        fail(ValueError("宽高必须是 8 的倍数（latent 栅格）：%dx%d" % (a.width, a.height)))
    if not (256 <= a.width <= 1024 and 256 <= a.height <= 1024):
        fail(ValueError("宽高需在 256~1024 之间（8G 卡 SD1.5 的安全区）"))
    if a.control_image and not os.path.isfile(a.control_image):
        fail(ValueError("控制图不存在：%s" % a.control_image))

    # ---------- 1. ComfyUI 库环境（不启服务） ----------
    root = os.path.abspath(a.comfy_root)
    for p in (root, os.path.join(root, "comfy_extras")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    # 与 h3gen 同款低显存策略（SD1.5 fp32 权重 4.3G，流式加载防爆）
    import comfy.cli_args as cli_args
    cli_args.args.lowvram = True
    cli_args.args.reserve_vram = 0.8
    cli_args.args.disable_pinned_memory = True

    # ---------- 1b. DynamicVRAM 启动序列（复刻 main.py，同 h3gen.py） ----------
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

    # import torch 前启用 cudaMallocAsync（同 h3gen.py）
    try:
        import cuda_malloc  # noqa: F401
    except Exception:
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "backend:cudaMallocAsync")

    phase("初始化 ComfyUI 运行环境")
    import torch  # noqa: E402
    import comfy.model_management as mm  # noqa: E402
    import comfy.memory_management  # noqa: E402  （保持与 h3gen 一致的导入面）
    import comfy.model_patcher  # noqa: E402
    import comfy.utils  # noqa: E402
    import nodes  # noqa: E402
    comfy.utils.set_progress_bar_enabled(False)

    try:
        _aimdo_ok = aimdo.init_devices(
            [(d.index, 0) for d in mm.get_all_torch_devices()])
    except TypeError:
        _aimdo_ok = aimdo.init_devices(
            [d.index for d in mm.get_all_torch_devices()])
    if _aimdo_ok:
        comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcherDynamic
        comfy.memory_management.aimdo_enabled = True
        emit({"t": "phase", "s": "DynamicVRAM 已启用"})
    else:
        emit({"t": "phase", "s": "警告：DynamicVRAM 初始化失败，退回静态加载"})

    if not torch.cuda.is_available():
        fail(RuntimeError("没有可用的 CUDA 设备，SD1.5 生成需要 NVIDIA 显卡"))
    gpu = torch.cuda.get_device_name(0)
    emit({"t": "phase", "s": "GPU：%s（%.1fG）" %
          (gpu, torch.cuda.get_device_properties(0).total_memory / 1024 ** 3)})

    seed = a.seed if a.seed else int(time.time()) % (2 ** 32)

    # ---------- 2. 加载模型 ----------
    phase("加载 SD1.5 模型")
    model, clip, vae = nodes.CheckpointLoaderSimple().load_checkpoint(a.ckpt)
    pct(25, "加载文本编码器 / VAE")
    if a.lora:
        pct(32, "挂 LoRA：%s" % a.lora)
        model, clip = nodes.LoraLoader().load_lora(
            model, clip, a.lora, a.lora_scale, a.lora_scale)

    # ---------- 3. 提示词编码 ----------
    pct(40, "编码提示词")
    positive = nodes.CLIPTextEncode().encode(clip, a.prompt)[0]
    negative = nodes.CLIPTextEncode().encode(clip, a.negative or "")[0]

    # ---------- 4. ControlNet（canny 锁构图） ----------
    if a.control_image:
        pct(50, "加载 ControlNet（canny 锁构图）")
        controlnet = nodes.ControlNetLoader().load_controlnet(a.controlnet)[0]
        ctrl_img = load_image_tensor(a.control_image)
        positive, negative = nodes.ControlNetApplyAdvanced().apply_controlnet(
            positive, negative, controlnet, ctrl_img,
            a.controlnet_strength, 0.0, 1.0)

    # ---------- 5. 采样 ----------
    pct(55, "采样生成（%d 步）" % a.steps)
    latent = nodes.EmptyLatentImage().generate(a.width, a.height, 1)[0]
    with torch.inference_mode():
        out_latent = nodes.KSampler().sample(
            model, seed, a.steps, a.cfg, a.sampler, a.scheduler,
            positive, negative, latent)[0]

    # ---------- 6. 解码 + 存 PNG ----------
    pct(90, "解码图像")
    try:
        import comfy.model_management as _mm
        _mm.unload_all_models()
        torch.cuda.empty_cache()
    except Exception:
        pass
    with torch.inference_mode():
        image = nodes.VAEDecode().decode(vae, out_latent)[0]  # [1, H, W, 3] float

    pct(96, "保存 PNG")
    from PIL import Image
    import numpy as np
    arr = (image[0].clamp(0, 1) * 255).to(torch.uint8).cpu().numpy()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    Image.fromarray(arr).save(a.out)

    emit({"t": "done", "path": os.path.abspath(a.out),
          "w": int(arr.shape[1]), "h": int(arr.shape[0]), "seed": seed})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        fail(e)
