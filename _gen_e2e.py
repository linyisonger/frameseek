# -*- coding: utf-8 -*-
"""MiniMax H3 生成链路 e2e（真实提交，跑完看结果）。"""
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "vendor"))

from core import gen as G  # noqa: E402

T0 = time.time()


def tick(pct, msg):
    p = f"{pct}%" if pct is not None else " ? "
    print(f"[{p:>4}] {msg}  ({time.time() - T0:.0f}s)", flush=True)


if __name__ == "__main__":
    r = G.generate(
        prompt=("Professional digital human news anchor in a modern broadcast "
                "studio, speaking calmly to the camera. Medium shot, soft "
                "studio lighting, clean background."),
        duration=5, aspect="16:9 (Widescreen)", megapixels=0.3, seed=42,
        name="e2e测试短片", index="asr",
        root=os.path.join(BASE, "_testlib"),
        on_tick=tick, timeout=5400)
    print("RESULT:", json.dumps(r, ensure_ascii=False, indent=1))
