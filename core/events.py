# -*- coding: utf-8 -*-
"""进度实时推送（WebSocket 广播）。

以前前端每秒轮询 `/api/tasks`，有两个问题：
1. 延迟最高 1 秒，而且 DB 里存的 message 是一句纯文本，OCR 到底扫了多少帧
   看不到（只能从 "OCR 120/180s" 猜）。
2. 多个库同时索引时，轮询把所有库的进度混在一个响应里。

现在索引器把**结构化事件**推到这儿，任何连接的页面都能实时收到：
    {"type": "progress", "root": ..., "done": 3, "total": 65,
     "phase": "ocr", "frames": 240, "frames_total": 1200, ...}

设计要点：
- 不引额外依赖。Flask 自带 Werkzeug 的 werkzeug.serving 里没有 WSGI WebSocket，
  所以优先用 `flask_sock`（纯 Python 实现，很轻）；没装就退化成 SSE
  （EventSource，前端本来就支持），再不行就沿用旧的轮询。
- 每个连接一个有界队列；队列满了丢**最旧**的消息——进度消息丢一帧无所谓，
  但绝不能因为某个客户端卡住把索引线程堵死。
- 广播是非阻塞的：put 满了直接丢，绝不在索引线程上等IO。
"""
import json
import threading
import time
from collections import deque
from typing import Any, Dict, List

MAX_QUEUE = 64          # 每个连接最多缓存多少条消息
MAX_LOG = 200# 内存里保留最近多少条，供新连接的页面立刻拿到当前状态


class _Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs: List["_Sub"] = []
        self.log: deque = deque(maxlen=MAX_LOG)
        self.seq = 0

    def subscribe(self) -> "_Sub":
        s = _Sub()
        with self.lock:
            self.subs.append(s)
            # 新连接先把最近状态补齐，免得刚打开的页面不知道正在跑什么。
            # 但**一次性通知**（task_done）不能补：补的是一两分钟的日志，
            # 把「已停止 / 识别完成」再弹一遍，用户刷新页面会看到莫名其妙的旧提示。
            # progress / video / lines 这类状态和增量补了才有用。
            backlog = [e for e in self.log if e.get("type") != "task_done"]
        for ev in backlog:
            s.put(ev)
        return s

    def unsubscribe(self, s: "_Sub"):
        with self.lock:
            if s in self.subs:
                self.subs.remove(s)

    def publish(self, ev: Dict[str, Any]):
        """广播一条消息。**绝不能抛异常、绝不能阻塞调用方。**"""
        try:
            with self.lock:
                self.seq += 1
                ev["seq"] = self.seq
                ev["ts"] = time.time()
                self.log.append(ev)
                subs = list(self.subs)
            for s in subs:
                s.put(ev)
        except Exception:
            pass

    def count(self) -> int:
        with self.lock:
            return len(self.subs)


class _Sub:
    """一个连接的发送队列（有界，满了丢最旧的）。"""

    def __init__(self):
        self.q: deque = deque(maxlen=MAX_QUEUE)
        self.cv = threading.Condition()
        self.closed = False

    def put(self, ev: Dict[str, Any]):
        with self.cv:
            if self.closed:
                return
            if len(self.q) == self.q.maxlen:
                self.q.popleft()        # 丢最旧的一条，保证进度消息能进
            self.q.append(ev)
            self.cv.notify()

    def get(self, timeout: float = 20.0):
        """取下一条；超时返回 None（用于心跳）。"""
        with self.cv:
            if not self.q and not self.closed:
                self.cv.wait(timeout)
            if self.q:
                return self.q.popleft()
            return None

    def close(self):
        with self.cv:
            self.closed = True
            self.q.clear()
            self.cv.notify_all()


HUB = _Hub()


# ---------------- 各阶段事件构造 ----------------

def ev_progress(root: str, lib: str, done: int, total: int,
                phase: str = "", message: str = "", **extra):
    pct = int(min(100, round(done / total * 100))) if total else 0
    HUB.publish({"type": "progress", "root": root, "lib": lib,
                 "done": done, "total": total, "pct": pct,
                 "phase": phase, "message": message, **extra})


def ev_video(root: str, lib: str, video: Dict[str, Any]):
    """一个素材入库完成——前端据此实时往列表里加卡片，不用等整库跑完。"""
    HUB.publish({"type": "video", "root": root, "lib": lib, "video": video})


def ev_lines(root: str, lib: str, name: str, count: int, source: str = ""):
    HUB.publish({"type": "lines", "root": root, "lib": lib,
                 "name": name, "count": count, "source": source})


def ev_task_done(root: str, lib: str, summary: Dict[str, Any]):
    HUB.publish({"type": "task_done", "root": root, "lib": lib,
                 "summary": summary})


# ---------------- 传输层 ----------------

def sse_stream(root_filter: str = ""):
    """Server-Sent Events。flask_sock 不可用时的退路，前端 EventSource 直接连。"""
    sub = HUB.subscribe()

    def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                ev = sub.get(20.0)
                if ev is None:
                    yield ": keepalive\n\n"      # 心跳，防代理断连
                    continue
                if root_filter and ev.get("root") != root_filter:
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except GeneratorExit:
            pass
        finally:
            HUB.unsubscribe(sub)
            sub.close()

    return gen


def has_websocket() -> bool:
    try:
        import flask_sock  # noqa: F401
        return True
    except Exception:
        return False