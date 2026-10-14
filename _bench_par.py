# -*- coding: utf-8 -*-
"""测 (进程数 × 每进程线程数) 对 OCR 吞吐的影响。

用法：python _bench_par.py <workers> <threads>
每次都是独立进程，跑完就退出——在同一进程里反复 spawn 进程池会卡死（踩过）。
"""
import json
import multiprocessing as mp
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault(
    "PATH", r"D:\Program Files\ffmpeg-7.0.2-full_build\bin;" +
    os.environ.get("PATH", ""))

from core import config as C          # noqa: E402
from core import ocr                  # noqa: E402

VID = os.path.join(HERE, "_testlib", "test5min.mp4")
DUR = 300.0


def main():
    workers = int(sys.argv[1])
    threads = int(sys.argv[2])
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else DUR
    C.OCR_WORKERS = workers
    C.OCR_THREADS = threads

    prof = ocr.calibrate(VID)
    ocr._PROFILE = prof
    print("profile:", prof.to_dict(), flush=True)

    t_spawn = time.time()
    ctx = mp.get_context("spawn")
    pool = ctx.Pool(processes=workers, initializer=ocr._worker_init,
                    initargs=(threads, prof))
    spawn_s = time.time() - t_spawn

    items = []
    n = 0
    t0 = time.time()
    for ts, buf, w, h in ocr._frame_stream(VID, 0.0, dur):
        items.append((ts, buf, w, h))
        n += 1
        if len(items) >= ocr.CHUNK:
            ocr._run_batch(items, pool, threads)
            items = []
    if items:
        ocr._run_batch(items, pool, threads)
    el = time.time() - t0
    try:
        pool.terminate()
        pool.join()
    except Exception:
        pass
    r = {
        "workers": workers, "threads": threads, "dur": dur, "frames": n,
        "spawn_s": round(spawn_s, 1), "sec": round(el, 1),
        "ms_per_frame": round(el / max(1, n) * 1000, 1),
        "fps": round(n / max(0.001, el), 1),
    }
    print(json.dumps(r, ensure_ascii=False), flush=True)
    json.dump(r, open(os.path.join(HERE, "_out",
                                   f"bench_par_{workers}_{threads}.json"),
                      "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()
