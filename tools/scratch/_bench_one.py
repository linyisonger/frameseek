# -*- coding: utf-8 -*-
"""单个像素配置的基准（被 _bench_px.py 用独立子进程调用）

独立进程是必须的：一个配置跑完就整个退出，onnxruntime 的内存和句柄
全部归还。之前在同一进程里连续 spawn 8 次进程池，跑到第 5~6 个配置时
整体卡死（worker 起了 18 分钟不干活），只能靠外部超时兜底。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import config as C          # noqa: E402
from core import ocr                  # noqa: E402

VID = os.path.join(HERE, "_testlib", "test5min.mp4")


def det_px(W, BAND, LIMIT):
    ih, iw = 1080.0, 1920.0
    oh = int(round(W * (ih * BAND) / iw))
    oh -= oh % 2
    w, h = float(W), float(oh)
    m = max(w, h)
    if m > LIMIT:
        r = LIMIT / m
        w, h = w * r, h * r
    return int(w), int(h), int(w * h), oh


def mode_e2e(W, BAND, LIMIT, dur):
    C.OCR_WIDTH = W
    C.OCR_BAND = BAND
    os.environ["FRAMESEEK_DET_LIMIT"] = str(LIMIT)
    step = float(os.environ.get("FRAMESEEK_STEP", C.OCR_STEP))
    C.OCR_STEP = step
    dw, dh, dpx, oh = det_px(W, BAND, LIMIT)
    t0 = time.time()
    rows, err = ocr.ocr_lines(VID, dur=dur)
    el = time.time() - t0
    texts = [r.get("text", "") for r in rows]
    return {
        "mode": "e2e", "width": W, "band": BAND, "limit": LIMIT,
        "step": step,
        "frame_px": W * oh, "det_w": dw, "det_h": dh, "det_px": dpx,
        "sec": round(el, 1), "lines": len(rows),
        "chars": sum(len(t) for t in texts), "err": err, "texts": texts,
    }


def mode_pf(W, BAND, LIMIT, dur):
    """逐帧跑一次，拿到每帧"有没有字幕"的标签 + 像素特征。

    用来评估像素级空帧预筛：能不能用几乎免费的 numpy 统计，把没字幕的帧
    直接跳过，省掉一次完整 OCR。
    """
    import numpy as np
    C.OCR_WIDTH = W
    C.OCR_BAND = BAND
    os.environ["FRAMESEEK_DET_LIMIT"] = str(LIMIT)
    prof = ocr.calibrate(VID)
    ocr._PROFILE = prof
    ocr._worker_init(2, prof)
    eng = ocr.make_engine(2)
    feats, labels, ts = [], [], []
    t0 = time.time()
    for t, buf, w, h in ocr._frame_stream(VID, 0.0, dur):
        img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w).astype(np.float32)
        bright = float((img > 200).mean())                 # 高亮像素占比
        thr = img.mean() + 2.0 * img.std()
        hi = float((img > thr).mean())                     # 显著亮于背景
        gx = np.abs(np.diff(img, axis=1))
        edge = float((gx > 40).mean())                     # 横向梯度密度
        y0, y1 = int(h * 0.2), int(h * 0.8)
        mid = float((img[y0:y1] > 200).mean())             # 带中部高亮
        feats.append([round(bright, 4), round(hi, 4),
                      round(edge, 4), round(mid, 4)])
        try:
            rgb = np.repeat(np.frombuffer(buf, dtype=np.uint8)
                            .reshape(h, w)[:, :, None], 3, axis=2)
            res, _ = eng(rgb)
            txt = ocr.select_subtitle(res, w, h, prof) if res else ""
        except Exception:
            txt = ""
        labels.append(1 if txt else 0)
        ts.append(round(float(t), 2))
    return {
        "mode": "pf", "width": W, "band": BAND, "limit": LIMIT,
        "sec": round(time.time() - t0, 1), "n": len(ts), "ts": ts,
        "labels": labels, "feats": feats,
        "pos_rate": round(sum(labels) / max(1, len(labels)), 3),
    }


def main():
    mode = sys.argv[1]
    W = int(sys.argv[2])
    BAND = float(sys.argv[3])
    LIMIT = int(sys.argv[4])
    dur = float(sys.argv[5]) if len(sys.argv) > 5 else 300.0
    r = mode_e2e(W, BAND, LIMIT, dur) if mode == "e2e" \
        else mode_pf(W, BAND, LIMIT, dur)
    json.dump(r, open(os.path.join(HERE, "_out", f"bench_{mode}_{W}_{BAND}"
                                   f"_{LIMIT}.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    print(json.dumps({k: v for k, v in r.items() if k != "texts"},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
