# -*- coding: utf-8 -*-
"""后台索引驱动：只处理《潜伏》，跳过《亮剑》，写入顶层库。

库位置（用户指定）：E:/视频/电视剧/.frameseek/index.db
- root 指向 E:/视频/电视剧/（这样 Indexer 的缓存目录就是这个顶层库）
- 但只挑 潜伏/ 子目录下的视频索引，亮剑/ 与其它一律跳过
- 每集记录：耗时、台词来源、台词条数、是否跳过/失败
- 日志写到 .workbuddy/index_luqqian.log（可随时 tail 看进度）
- 已索引的集（md5 相同）自动跳过，断点续跑安全
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import indexer as IX

ROOT = r"E:/视频/电视剧"            # 顶层库根（DB = ROOT/.frameseek/index.db）
ONLY_SUBDIR = "潜伏"              # 只索引这个子目录
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   ".workbuddy", "index_luqqian.log")


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    open(LOG, "a", encoding="utf-8").close()
    log(f"=== 开始索引《潜伏》→ 顶层库 root={ROOT} ===")

    ix = IX.Indexer(ROOT, use_asr=False, with_gif=True, retry_empty=False)
    ix._lib_name = "电视剧"
    log(f"缓存/库目录 = {ix.cd}")

    all_files = IX.list_video_files(ROOT)
    # 只挑 潜伏/ 子目录，亮剑/ 与其余全部跳过
    files = [f for f in all_files
             if ("\\" + ONLY_SUBDIR + "\\") in f.replace("/", "\\")
             or f.replace("/", "\\").endswith("\\" + ONLY_SUBDIR)]
    skipped_dirs = len(all_files) - len(files)
    total = len(files)
    log(f"ROOT 下扫到 {len(all_files)} 个视频；仅保留《潜伏》{total} 个（跳过 {skipped_dirs} 个其它目录，含亮剑）")

    done = 0
    skipped = 0
    failed = []
    t0 = time.time()

    for i, f in enumerate(files, 1):
        name = os.path.basename(f)
        rel = ix._cur_file = os.path.relpath(f, ROOT)
        log(f"--- [{i}/{total}] 开始 {rel} ---")
        t1 = time.time()
        try:
            r = ix.index_file(
                f,
                cb=lambda m, n=name: log(f"    · {n}: {m}"),
            )
            dt = time.time() - t1
            if r.get("skipped"):
                skipped += 1
                log(f"    跳过（已完成/已忽略） id={r.get('id','')}  用时 {dt:.1f}s")
            elif r.get("err"):
                failed.append((rel, r["err"]))
                log(f"    ❌ 失败：{r['err']}  用时 {dt:.1f}s")
            else:
                src = r.get("source", "?")
                n = r.get("line_count", 0) or r.get("lines", 0)
                log(f"    ✅ 完成 来源={src} 台词={n} 用时 {dt:.1f}s")
        except Exception as e:
            dt = time.time() - t1
            failed.append((rel, str(e)))
            log(f"    ❌ 异常：{e}\n{traceback.format_exc()}")
        done += 1
        log(f"    进度 {done}/{total}（累计 {time.time()-t0:.0f}s）")

    el = time.time() - t0
    log("=== 结束 ===")
    log(f"总计 {total} 集 | 实际处理 {done} | 跳过 {skipped} | 失败 {len(failed)}")
    log(f"总耗时 {el:.0f}s（{el/60:.1f}分钟）")
    for n, e in failed:
        log(f"  失败集：{n} -> {e}")
    log("DONE")


if __name__ == "__main__":
    main()
