# -*- coding: utf-8 -*-
"""轮询 H3 生成任务直到终态，顺带把 h3gen 日志的关键行打出来。

用法： python _gen_watch.py <task_id> [超时秒]
"""
import sys, os, time, json, sqlite3, glob

BASE = os.path.dirname(os.path.abspath(__file__))


def _latest_task(root, tid):
    """直接从库里读任务表（MCP 每次起子进程太重，这里轻量轮询）。"""
    idx = os.path.join(root, ".frameseek", "index.db")
    if not os.path.isfile(idx):
        return None
    c = sqlite3.connect(idx)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute("select * from tasks where id=?", (tid,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None
    finally:
        c.close()


def _log_tail(n=3):
    p = os.path.join(BASE, "_out", "h3gen_last.log")
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = [x.strip() for x in f.readlines() if x.strip()]
        keep = [x for x in lines
                if not x.startswith("aimdo:")
                and "hostbuf" not in x]
        return " | ".join(keep[-n:])[:400]
    except OSError:
        return ""


if __name__ == "__main__":
    tid = int(sys.argv[1])
    root = sys.argv[2] if len(sys.argv) > 2 else r"E:\视频\电视剧"
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 1500
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        t = _latest_task(root, tid)
        if t:
            cur = (t.get("status"), t.get("done"), t.get("message", "")[:60])
            if cur != last:
                el = int(time.time() - t0)
                print(f"[{el:4d}s] {t.get('status')} "
                      f"{t.get('done')}/{t.get('total')} "
                      f"{t.get('message','')[:80]}", flush=True)
                last = cur
            if t.get("status") in ("done", "error"):
                print("\n=== 终态 ===")
                print(json.dumps({k: t.get(k) for k in
                      ("id", "kind", "status", "done", "total", "message",
                       "error")}, ensure_ascii=False, indent=1))
                print("\n=== 日志尾 ===")
                print(_log_tail(5))
                sys.exit(0)
        else:
            print(f"[{int(time.time()-t0):4d}s] 任务 {tid} 不在 {root} 里")
        time.sleep(15)
    print(f"\n超时 {timeout}s 仍未结束，任务仍在跑")
    print(_log_tail(5))
