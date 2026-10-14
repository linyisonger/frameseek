#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VAE 解码探针 v2：独立进程跑对照组/实验组，互不污染。

用法：_vae_probe.py a   不包 inference_mode（预期 OOM）
      _vae_probe.py b   包 inference_mode（预期 OK）

环境与 h3gen.py 1b 节完全一致（aimdo + cuda_malloc + CoreModelPatcher 动态化）。
"""
import gc
import os
import sys

COMFY_ROOT = r"D:\project\AIProjects\ComfyUI_windows_portable\ComfyUI"
VAE_NAME = "minimax_h3_video_vae_fp16.safetensors"

for p in (COMFY_ROOT, os.path.join(COMFY_ROOT, "comfy_extras")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import logging
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

MODE = (sys.argv[1] if len(sys.argv) > 1 else "b").lower()

# ---- 与 h3gen 一致的 cli 参数 ----
import comfy.cli_args as cli_args
cli_args.args.lowvram = True
cli_args.args.reserve_vram = 0.8
cli_args.args.disable_pinned_memory = True

# ---- 复刻 main.py 58-70：import torch 前先 aimdo.init ----
import comfy_aimdo.control as aimdo
_headroom = int(cli_args.args.reserve_vram * 1024 ** 3)
try:
    ok = aimdo.init(simple_vram_headroom=_headroom, nvml_pressure=True)
except TypeError:
    try:
        ok = aimdo.init(simple_vram_headroom=_headroom)
    except TypeError:
        ok = aimdo.init()

# ---- 复刻 main.py:100：import torch 前启用 cudaMallocAsync ----
try:
    import cuda_malloc  # noqa: F401
except Exception:
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "backend:cudaMallocAsync")

import torch
import comfy.model_management as mm
import comfy.memory_management
import comfy.model_patcher
import nodes

# ---- 复刻 main.py 251-282：init_devices + CoreModelPatcher 换动态版 ----
try:
    ok2 = aimdo.init_devices([(d.index, 0) for d in mm.get_all_torch_devices()])
except TypeError:
    ok2 = aimdo.init_devices([d.index for d in mm.get_all_torch_devices()])
print("[probe:%s] aimdo ok=%s/%s Core=%s aimdo_enabled=%s alloc=%s" % (
    MODE, ok, ok2, comfy.model_patcher.CoreModelPatcher.__name__,
    comfy.memory_management.aimdo_enabled,
    os.environ.get("PYTORCH_CUDA_ALLOC_CONF")))

# ---- 加载视频 VAE ----
vae = nodes.VAELoader().load_vae(VAE_NAME)[0]
print("[probe:%s] vae.patcher=%s dynamic=%s" % (
    MODE, type(vae.patcher).__name__, vae.patcher.is_dynamic()))

mm.unload_all_models()
torch.cuda.empty_cache()

# 全尺寸 latent：124 帧 → latent T=37（736×416 @16x），与真实跑批一致
lat = torch.randn(1, 24, 37, 26, 46)
print("[probe:%s] latent=%s free_mb=%.0f" % (
    MODE, tuple(lat.shape), torch.cuda.mem_get_info()[0] / 1048576))

if MODE == "a":
    try:
        img = vae.decode(lat)
        print("[probe:a] DECODE OK (unexpected!) shape=", tuple(img.shape))
    except Exception as e:
        # 不打印 traceback（帧会拽住大 tensor）；只报类型和首行消息
        print("[probe:a] OOM as expected:", type(e).__name__,
              str(e).split("\n")[0][:110])
        sys.exit(3)
else:
    try:
        with torch.inference_mode():
            img = vae.decode(lat)
        print("[probe:b] DECODE OK shape=", tuple(img.shape),
              "dtype=", img.dtype,
              "free_after=%.0fMB" % (torch.cuda.mem_get_info()[0] / 1048576))
        # 验下游可用性：inference tensor 出块后做功能运算 + 落盘字节
        raw = (img.clamp(0, 1) * 255).to(torch.uint8).cpu().numpy().tobytes()
        print("[probe:b] downstream bytes =", len(raw))
    except Exception as e:
        print("[probe:b] DECODE FAILED:", type(e).__name__,
              str(e).split("\n")[0][:200])
        sys.exit(1)

print("[probe:%s] DONE" % MODE)
