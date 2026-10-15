# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - 扫描索引：md5 去重 -> 元信息 -> 封面/动图 -> 台词 -> 台词截图"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Callable

from core import config as C
from core import media, asr, store as store_mod
from core import events


def cache_dir(root: str) -> str:
    return os.path.join(root, C.CACHE_DIR)


def ensure_layout(root: str) -> str:
    d = cache_dir(root)
    for sub in (C.SHOT_DIR, C.GIF_DIR, C.COVER_DIR, C.CLIP_DIR, C.UPLOAD_DIR):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


def db_path(root: str) -> str:
    return os.path.join(ensure_layout(root), C.DB_NAME)


def open_store(root: str) -> store_mod.Store:
    return store_mod.Store(db_path(root))


def list_video_files(root: str) -> List[str]:
    out = []
    cd = os.path.abspath(cache_dir(root))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and os.path.abspath(os.path.join(dirpath, d)) != cd]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in C.VIDEO_EXT:
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


class Indexer:
    def __init__(self, root: str, use_asr: bool = None, with_gif: bool = True,
                 retry_empty: bool = True, write_srt: bool = True,
                 no_lines: bool = False):
        self.root = os.path.abspath(root)
        self.store = open_store(self.root)
        self.cd = ensure_layout(self.root)
        # AI 生成产物库专用（no_lines=True）：只登记媒体信息，**完全不碰台词**——
        # 不读同名 .srt、不读内嵌字幕、不 OCR 画面、不跑语音识别，也不落 .srt。
        # 生成物本来就没有台词这回事，让它们出现在台词体系里只会污染检索和复查。
        # 副作用：已入库的老记录若带着台词，会被清掉并置 line_source='none'。
        self.no_lines = no_lines
        # 字段名是历史遗留（当时是"用不用语音转写"）。现在语义是
        # **允不允许对没字幕的素材跑 OCR/ASR**；None = 走配置默认值。
        self.use_asr = use_asr
        self.with_gif = with_gif
        # OCR/ASR 生成的台词顺手在视频同目录落一个同名 .srt，
        # 用户手改完点“重新导入字幕”，不用重跑识别
        self.write_srt = write_srt
        # 已入库但没有台词的素材，再索引时重试一次（上次可能只是识别失败）
        self.retry_empty = retry_empty
        self.tid = 0
        self._done = 0
        self._total = 0
        self._lib_name = ""
        # scan=整库扫描（进度分母是"第几个素材"）；video=单素材重识别
        # （分母只有 1，主进度条改跟 OCR 帧完成度走）。前端据此换文案。
        self._kind = "scan"
        self._cur_file = ""
        # 用户中途按「停止」：置位后，扫描循环不再开下一条素材，
        # 正在跑的那条也会在 OCR/ASR 的下一个检查点提前收工。
        # 已经跑完的素材都已经落库了，下次索引按 md5 直接跳过它们继续往下跑。
        self._cancel = threading.Event()
        self.stopped = False
        # 从「还没识别」被中断的素材数（run() 里统计，给摘要用）
        self._cancelled_files = 0

    def request_stop(self) -> bool:
        """请求停止。线程安全，随时可调（幂等）。"""
        self._cancel.set()
        return True

    def _ck(self):
        """传给 OCR/ASR 的 cancel 回调。"""
        return self._cancel.is_set

    def _tick(self, msg: str, done: int = None):
        """把进度/阶段消息写进 tasks 表，前端每秒轮询能看到"""
        if not self.tid:
            return
        self.store.tick_task(self.tid,
                             self._done if done is None else done, msg)

    _EV_ERR_ONCE = False

    def _ev(self, phase: str, message: str, **extra):
        """推一条实时进度事件（WebSocket/SSE）。失败绝不影响索引。

        坑（踩过一次，整整一周进度栏都是空的）：`lib` 已经按位置传了，
        下面 **不能再写 lib=**，否则 Python 直接 TypeError:
        got multiple values for argument 'lib'。而这里原本是裸 except，
        异常被吞得干干净净——界面上表现为"进度永远不动"，日志里什么都没有。
        所以现在第一次出错时打一行到 stderr，别再让 bug 隐形。
        """
        try:
            # 坑（同一类踩了两次）：下面这些键 **不能在 extra 里重复出现**，
            # 否则 Python 直接 TypeError: got multiple values for argument。
            # 所以先组好默认值，再让 extra 覆盖——而不是两处都硬传。
            payload = {"phase": phase, "message": message,
                       "kind": self._kind, "file": self._cur_file}
            payload.update(extra)
            events.ev_progress(self.root, self._lib_name, self._done,
                               self._total, **payload)
        except Exception as e:
            if not Indexer._EV_ERR_ONCE:
                Indexer._EV_ERR_ONCE = True
                import sys
                print(f"[events] 推进度事件失败（只报一次）："
                      f"{type(e).__name__}: {e}", file=sys.stderr)

    def _emit_progress(self, msg):
        """OCR/ASR 的阶段进度。msg 可能是 dict（OCR，带帧级完成度）。"""
        if isinstance(msg, dict):
            # 标定 / 收尾也走这条通道，给个自己的阶段名，页面上才好区分
            # （都是 OCR 的一环，但人关心的是"现在到底在干嘛"）
            ph = "ocr"
            if msg.get("calib"):
                ph = "calib"
            elif msg.get("finished"):
                ph = "ocr"
            self._ev(ph, msg.get("text", ""),
                     file=msg.get("file", ""),
                     frames=msg.get("frames", 0),
                     frames_total=msg.get("frames_total", 0),
                     frames_pct=(int(min(100, round(
                         msg["frames"] / msg["frames_total"] * 100)))
                         if msg.get("frames_total") else 0),
                     pos=round(msg.get("pos", 0), 1),
                     span=round(msg.get("total", 0), 1),
                     lines=msg.get("lines", 0),
                     eta_min=round(msg.get("eta_min", 0), 1),
                     batches=msg.get("batches", 0))
        else:
            self._ev("", str(msg))

    def _mk_prog(self, name: str, cb=None, single: bool = False,
                 label: str = None):
        """造一个进度回调。**必须兼容两种调用签名**，这是踩过的坑：

        · OCR 走 `ocr.structured(fn)` 包装，包装器是 `def wrapper(msg)`，
          只传 **一个** 参数（dict 或 str）；
        · ASR 的老调用是 `progress(n, msg)`，两个参数。

        以前统一写成 `def _prog(n, msg="")`，结果 structured 那一路传进来的
        dict 落在了 `n` 上、`msg` 永远是空串，被 `if not msg: return` 原地吞掉——
        界面表现就是"OCR 进度永远不动/不显示"。现在用 *a 接，取最后一个实参。
        """
        def _prog(*a):
            msg = a[-1] if a else ""
            if not msg:
                return
            shown = label or name          # 二级目录下的片子带目录，同名片才分得清
            if isinstance(msg, dict):
                msg = dict(msg)
                msg["text"] = f"{msg.get('text', '')} · {shown}"
                msg["file"] = shown
                if single:
                    # 单素材没有"第几个文件"的概念，主进度条直接跟 OCR 帧完成度走
                    ft = msg.get("frames_total") or 0
                    if ft:
                        self._done = round(
                            min(1.0, (msg.get("frames") or 0) / ft), 3)
                text = msg["text"]
            else:
                msg = f"{msg} · {name}"
                text = msg
            if cb:
                cb(text)
            self._tick(f"{name}：{text}")
            self._emit_progress(msg)
        return _prog

    # ---- 单个文件 ----
    def index_file(self, path: str, cb: Callable[[str], None] = None) -> Dict[str, Any]:
        path = os.path.abspath(path)
        rel = os.path.relpath(path, self.root)
        name = os.path.basename(path)
        st = os.stat(path)
        md5 = media.file_md5(path)
        # 进度事件里的"当前视频"一律用相对路径：二级目录下的片子只看 basename
        # 满屏 01.mp4，进度浮窗上根本认不出在跑哪一部。
        self._cur_file = rel

        if self.store.is_ignored(md5):
            # 用户从库里移除过（文件还在）→ 以后索引都不再收进来
            return {"skipped": True, "ignored": True, "name": name}

        old = self.store.find_by_md5(md5)
        if old:
            # 同一个 md5 = 同一个素材，不重复入库；只有原路径文件没了才把记录迁到新位置
            old_abs = os.path.join(self.root, old["rel"])
            if os.path.exists(old_abs):
                if self.no_lines:
                    # 生成产物库：历史残留的台词清掉（比如早先按普通库索引过一遍）
                    if (old.get("line_count") or 0):
                        self.store.clear_lines(old["id"])
                        self.store.update_video(
                            old["id"], line_source="none", line_count=0,
                            status="ok", err="")
                    elif (old.get("line_source") or "") != "none":
                        self.store.update_video(old["id"], line_source="none")
                    return {"id": old["id"], "skipped": True, "name": name,
                            "no_lines": True}
                # 上次没拿到台词（可能只是转写失败）→ 这次补一遍
                if self.retry_empty and self.use_asr and not (old.get("line_count") or 0):
                    return self._fill_lines(old, path, cb)
                return {"id": old["id"], "skipped": True, "name": name}
            self.store.update_video(old["id"], rel=rel, name=name)
            return {"id": old["id"], "skipped": True, "moved": True, "name": name}

        meta = media.video_meta(path)
        safe = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in os.path.splitext(name)[0])
        tag = f"{safe[:40]}_{md5[:8]}"

        cover_rel = f"{C.COVER_DIR}/{tag}.jpg"
        gif_rel = f"{C.GIF_DIR}/{tag}.gif"
        if cb:
            cb(f"封面 {name}")
            self._ev("cover", f"封面 {name}")
        media.make_cover(path, os.path.join(self.cd, cover_rel))
        gif_ok = False
        if self.with_gif:
            if cb:
                cb(f"动图 {name}")
            self._ev("gif", f"动图 {name}")
            gif_ok = media.make_gif(path, os.path.join(self.cd, gif_rel))

        vid = self.store.add_video({
            "rel": rel, "name": name, "md5": md5, "size": st.st_size,
            "mtime": st.st_mtime, "duration": meta["duration"],
            "width": meta["width"], "height": meta["height"],
            "fps": meta.get("fps") or 0.0,
            "vcodec": meta.get("vcodec") or "",
            "acodec": meta.get("acodec") or "",
            "bitrate": meta.get("bitrate") or 0,
            "cover": cover_rel, "gif": gif_rel if gif_ok else "",
            "line_count": 0, "line_source": "pending", "status": "pending",
            "err": "", "created_at": time.time(),
        })

        if self.no_lines:
            # 生成产物：到此为止，不再往下走台词链路（OCR / ASR / SRT 全跳过）
            self.store.update_video(vid, status="ok", line_source="none",
                                    line_count=0, err="")
            return {"id": vid, "skipped": False, "name": name, "lines": 0,
                    "source": "none", "err": "", "no_lines": True}

        if cb:
            cb(f"台词 {name}")
            self._ev("lines", f"台词 {name}")

        _prog = self._mk_prog(name, cb, label=self._cur_file)

        rows, source, err = asr.get_lines(path, self.use_asr, progress=_prog,
                                          cancel=self._ck())
        if err == "__cancelled__":
            return self._mark_cancelled(vid, name)
        self._maybe_srt(path, rows, source, name, cb)
        if rows:
            self._write_shots(vid, path, rows, tag, cb, src=source)
        self.store.update_video(vid, status="ok" if not err else "error",
                                line_source=source, line_count=len(rows),
                                err=err or "")
        return {"id": vid, "skipped": False, "name": name, "lines": len(rows),
                "source": source, "err": err}

    def _mark_cancelled(self, vid: int, name: str) -> Dict[str, Any]:
        """被用户中途停掉的那条素材：留成「还没识别」。

        **不能**把扫了一半的字幕落库（下次再索引就永远只剩前半句了）。
        这里只把记录置回 pending，别的什么都不写——下次索引会重新识别它
        （retry_empty 那条分支），已经跑完的素材则按 md5 直接跳过。
        """
        self._cancelled_files += 1
        try:
            self.store.update_video(vid, status="pending", line_source="pending",
                                    line_count=0, err="")
        except Exception:
            pass
        return {"id": vid, "skipped": False, "name": name, "lines": 0,
                "source": "none", "err": "", "cancelled": True}

    def _maybe_srt(self, path: str, rows, source: str, name: str, cb=None) -> str:
        """机器生成的台词（OCR / ASR），在视频同目录写同名 .srt。

        这样用户可以用记事本/剪映直接改错字、调时间，改完在页面点"重新导入字幕"，
        数据库和台词截图都会跟着刷新，不用重跑一遍（OCR 一部要半小时）。
        external（用户自己放的）不写——本来就有。
        """
        if not rows or not self.write_srt:
            return ""
        if source not in ("ocr", "asr"):
            return ""
        try:
            p = media.write_srt(path, rows)
            if p and cb:
                cb(f"SRT {os.path.basename(p)}")
            return p
        except Exception as e:
            if cb:
                cb(f"SRT 写入失败 {type(e).__name__}")
            return ""

    def reimport_lines(self, vid: int, cb=None) -> Dict[str, Any]:
        """重新读取同名 .srt：用户在外部改完字幕后调用，重建台词 + 重抽截图"""
        old = self.store.get_video(vid)
        if not old:
            return {"ok": False, "err": "素材不存在"}
        path = os.path.join(self.root, old["rel"])
        if not os.path.exists(path):
            return {"ok": False, "err": "视频文件不存在"}
        side = media.srt_sidecar(path)
        if not side:
            return {"ok": False, "err": "同目录没有 .srt/.ass 字幕文件"}
        rows = asr._try_external_srt(path)
        if not rows:
            return {"ok": False, "err": f"{os.path.basename(side)} 里没读到有效字幕"}
        tag = os.path.splitext(os.path.basename(old["cover"] or ""))[0] \
            or f"{os.path.splitext(old['name'])[0][:40]}_{(old['md5'] or '')[:8]}"
        if cb:
            cb(f"重新导入 {old['name']}")
            self._ev("lines", f"台词 {old['name']}")
        self._write_shots(vid, path, rows, tag, cb, src="external")
        self.store.update_video(vid, line_source="external", line_count=len(rows),
                                status="ok", err="")
        return {"ok": True, "name": old["name"], "lines": len(rows),
                "srt": os.path.basename(side)}

    # 给已入库但没台词的素材补台词
    def _fill_lines(self, old: Dict[str, Any], path: str, cb) -> Dict[str, Any]:
        if self.no_lines:      # 生成产物库：永远不补台词
            return {"id": old["id"], "skipped": True, "name": old["name"],
                    "no_lines": True}
        name = old["name"]
        self._cur_file = old.get("rel") or name
        tag = os.path.splitext(os.path.basename(old["cover"] or name))[0] \
            or f"{os.path.splitext(name)[0][:40]}_{(old['md5'] or '')[:8]}"
        if cb:
            cb(f"补台词 {name}")
            self._ev("lines", f"台词 {name}")

        _prog = self._mk_prog(name, cb, label=self._cur_file)

        rows, source, err = asr.get_lines(path, self.use_asr, progress=_prog,
                                          cancel=self._ck())
        if err == "__cancelled__":
            r = self._mark_cancelled(old["id"], name)
            r["refilled"] = True
            return r
        self._maybe_srt(path, rows, source, name, cb)
        if rows:
            self._write_shots(old["id"], path, rows, tag, cb, src=source)
        self.store.update_video(old["id"], status="ok" if not err else "error",
                                line_source=source, line_count=len(rows),
                                err=err or "")
        return {"id": old["id"], "skipped": False, "refilled": True, "name": name,
                "lines": len(rows), "source": source, "err": err}

    def _write_shots(self, vid: int, path: str, rows: List[Dict[str, Any]],
                     tag: str, cb=None, src: str = ""):
        """每条台词抓一帧。

        逐条 seek 实测 1.14s/帧（2309 条 = 43 分钟/部），8 并发 0.156s/帧（6 分钟），
        改成“分段顺序解码 + 段内 select 批量命中”后 2309 条只要 85 秒——因为单独
        seek 每帧都要从关键帧重解一遍，批量只解一遍。详见 media.grab_shots_batch。

        `src` 传给 store.replace_lines 存进 lines.src：verify.check_line 要靠它
        区分"外挂SRT本来就没有画面框"和"OCR 跑了却没给方框凭证"。
        """
        if not rows:
            self.store.replace_lines(vid, rows, src=src)
            return

        def job(i_r_rel_t):
            i, r, rel, t = i_r_rel_t
            ok = media.grab_shot(path, t, os.path.join(self.cd, rel))
            r["shot"] = rel if ok else ""
            return i

        tasks = []
        for i, r in enumerate(rows):
            rel = f"{C.SHOT_DIR}/{tag}_{i:04d}.jpg"
            # 截图时刻：取本句中点，但至少往后 0.15s（避开 start 边界、越过剪辑点）、
            # 至多往后 1.2s（长 segment 常是 whisper 幻觉，中点会漂到无关画面）。
            # 最后再夹一次不越过 end——否则极短字幕（<0.15s）会切到下一句。
            d = (r.get("end") or r["start"]) - r["start"]
            off = min(1.2, max(0.15, d / 2))
            if d > 0:
                off = min(off, max(0.02, d * 0.85))
            tasks.append((i, r, rel, r["start"] + off))

        w = max(1, int(getattr(C, "SHOT_WORKERS", 1)))
        use_batch = (w > 1 and len(tasks) >= max(1, int(
            getattr(C, "SHOT_BATCH_MIN", 24))))

        if use_batch:
            times = [tk[3] for tk in tasks]
            outs = [os.path.join(self.cd, tk[2]) for tk in tasks]
            abs_outs = [os.path.abspath(o) for o in outs]
            nm = os.path.basename(path)
            n_all = len(tasks)

            def _seg_prog(done, total):
                """分片截图的进度：批量路径一次解一段，只能按段报，
                但总比整段黑着强——2309 条台词这段要跑一分多钟。"""
                self._ev("shots", f"截图 {nm} {done}/{total}",
                         frames=done, frames_total=total,
                         frames_pct=int(done / total * 100) if total else 0,
                         file=nm)
                self._tick(f"{nm}：截图 {done}/{total}")
                if cb:
                    cb(f"截图 {nm} {done}/{total}")

            try:
                oks = media.grab_shots_batch(path, times, abs_outs, workers=w,
                                             on_seg=_seg_prog)
            except Exception:
                oks = [False] * len(tasks)
            for (i, r, rel, t), ok in zip(tasks, oks):
                f = os.path.join(self.cd, rel)
                if not ok and not os.path.exists(f):
                    ok = media.grab_shot(path, t, f)
                # 坑：以前写成 `r["shot"] = rel if ok else ""`，
                # 批量路径偶尔会把已落盘的文件报成 False（分段返回码不可靠），
                # 结果图明明在磁盘上，库里却记成"没有截图"——检索页整片空白缩略图。
                # 更糟的是下面算孤儿时 keep 变成空集，102 个截图全被当成孤儿要删。
                # 所以：文件在，就算成功。
                r["shot"] = rel if (ok or os.path.exists(f)) else ""
            if cb:
                cb(f"截图 {os.path.basename(path)} {len(tasks)}/{len(tasks)}")
            self.store.replace_lines(vid, rows, src=src)
            return

        nm = os.path.basename(path)
        done_n = 0
        n_all = len(tasks)

        def _shot_prog():
            """截图也是耗时大头（2300 条约 85 秒），按帧进度推事件。"""
            self._ev("shots", f"截图 {nm} {done_n}/{n_all}",
                     frames=done_n, frames_total=n_all,
                     frames_pct=int(done_n / n_all * 100) if n_all else 0)

        if w > 1 and len(tasks) > 4:
            with ThreadPoolExecutor(max_workers=min(w, len(tasks))) as ex:
                for _ in ex.map(job, tasks):
                    done_n += 1
                    if cb and done_n % 20 == 0:
                        cb(f"截图 {nm} {done_n}/{n_all}")
                    _shot_prog()
        else:
            for tk in tasks:
                job(tk)
                done_n += 1
                if cb and done_n % 20 == 0:
                    cb(f"截图 {nm} {done_n}/{n_all}")
                _shot_prog()
        self.store.replace_lines(vid, rows, src=src)

    # ---- 整个目录 ----
    @staticmethod
    def _done_msg(done: int, skipped: int, ignored: int, asr_n: int,
                  refilled: int, failed: list) -> str:
        msg = f"完成 {done} 个，跳过 {skipped} 个重复"
        if ignored:
            msg += f"，忽略 {ignored} 个（已移除）"
        if asr_n:
            msg += f"，语音转写 {asr_n} 个"
        if refilled:
            msg += f"，补台词 {refilled} 个"
        if failed:
            msg += f"，失败 {len(failed)}"
        return msg

    def run(self, cb: Callable[[int, int, str], None] = None) -> Dict[str, Any]:
        files = list_video_files(self.root)
        self.tid = self.store.new_task("scan", self.root, len(files))
        self._total = len(files)
        self._done = 0
        done = 0
        skipped = 0
        ignored = 0
        asr_n = 0
        refilled = 0
        failed = []
        # 先报一次"开始"，用户点完按钮立刻就能看到卡片，不用等第一个文件处理完
        self._ev("", f"开始扫描，共 {len(files)} 个文件")
        try:
            for f in files:
                # 用户按了停止：不再开下一条。已经在跑的那条会在 OCR/ASR
                # 的检查点自己收工（见 _ck → ocr.ocr_lines(cancel=)）。
                if self._cancel.is_set():
                    self.stopped = True
                    break
                # 进度事件里的"当前视频"一律用相对路径：二级目录下的片子只看
                # basename 满屏 01.mp4，浮窗上根本认不出在跑哪一部。
                # 这条事件在开跑**之前**发，浮窗立刻就显示"现在弄的是哪个"；
                # frames/lines 一并清零，免得挂着上一个素材的帧数。
                self._cur_file = os.path.relpath(f, self.root)
                self._ev("", f"{done + 1}/{len(files)} {self._cur_file}",
                         file=self._cur_file, frames=0, frames_total=0,
                         frames_pct=0, lines=0)
                r = {}
                try:
                    r = self.index_file(
                        f, cb=lambda m: cb(done, len(files), m) if cb else None)
                    if r.get("ignored"):
                        ignored += 1
                    elif r.get("skipped"):
                        skipped += 1
                    if r.get("refilled"):
                        refilled += 1
                    if r.get("source") == "asr":
                        asr_n += 1
                    if r.get("err"):
                        failed.append({"file": f, "err": r["err"]})
                except Exception as e:
                    failed.append({"file": f, "err": str(e)})
                done += 1
                self._done = done
                if cb:
                    cb(done, len(files), os.path.basename(f))
                self._tick(f"{done}/{len(files)}")
                # 实时推送：整库进度 + 这个素材的入库结果，页面不用等跑完才刷新
                if r.get("id") and not r.get("skipped") and not r.get("cancelled"):
                    try:
                        vv = self.store.get_video(r["id"]) or {}
                        vv["root"] = self.root
                        vv["lib"] = self._lib_name
                        vv["duration_str"] = store_mod.fmt_ts(vv.get("duration") or 0)
                        events.ev_video(self.root, self._lib_name, vv)
                    except Exception:
                        pass
            if self.stopped:
                # 中断：已完成的部分**全部保留在库里**（每条素材处理完就落库了），
                # 下次索引靠 md5 直接跳过它们，从断点接着往下跑。
                left = max(0, len(files) - done)
                msg = (f"已停止：完成 {done} 个，剩 {left} 个。"
                       f"下次点「索引」会跳过已完成的，接着往下跑")
                if skipped:
                    msg += f"（本轮跳过 {skipped} 个已完成的）"
                if self._cancelled_files:
                    msg += f"（{self._cancelled_files} 个识别到一半，下次重跑）"
                self.store.finish_task(self.tid, "stopped", msg)
            else:
                msg = self._done_msg(done, skipped, ignored, asr_n, refilled, failed)
                self.store.finish_task(self.tid, "done", msg)
        except Exception as e:
            self.store.finish_task(self.tid, "error", str(e))
            raise
        summary = {"task_id": self.tid, "total": len(files), "skipped": skipped,
                   "ignored": ignored, "asr": asr_n, "refilled": refilled,
                   "failed": failed, "root": self.root, "message": msg,
                   "stopped": bool(self.stopped), "done": done,
                   "remain": max(0, len(files) - done),
                   "cancelled": self._cancelled_files}
        try:
            events.ev_task_done(self.root, self._lib_name, summary)
        except Exception:
            pass
        return summary

    # ---- 单个素材重转（「素材」页卡片的菜单用） ----
    def retranscribe(self, vid: int, cb: Callable[[str], None] = None,
                     force: bool = False) -> Dict[str, Any]:
        """对这一个素材重新识别台词。

        和首次索引的区别就两点：
        1. **先清干净再识别**。旧台词（连同旧截图、全文索引）全部删掉，
           再按当前逻辑跑一遍。不清的话，上次识别 100 条、这次逻辑改完只出
           80 条，剩下 20 条旧台词会原样留在库里，看起来像"识别没生效"。
        2. **忽略我们自己上次生成的 .srt**。OCR 跑完会在视频旁边落同名 .srt
           （见 _maybe_srt），那是产物不是素材。重新识别时若还按
           "外挂字幕优先"读它，等于把上次的结果原样读回来——改再多 OCR
           逻辑也体现不出来。所以上一轮来源是 ocr、或 force=True 时跳过它，
           让 OCR 真跑一遍并覆盖那个文件（内嵌字幕同理，force 时跳过）。

        代价：识别失败时旧台词已经没了。这是有意的——"重新识别"就是要最新结果，
        留着旧的混在一起更难判断哪条是对的。
        """
        v = self.store.get_video(vid)
        if not v:
            return {"ok": False, "err": "找不到这个素材"}
        path = os.path.join(self.root, v["rel"])
        if not os.path.exists(path):
            return {"ok": False, "err": "文件不在了：" + v["rel"]}

        prev_src = (v.get("line_source") or "").strip()
        # 清旧台词：库的行 + 全文索引一起删；截图等新结果出来再按名字比对清理
        cleared = self.store.clear_lines(vid)
        old_shots = cleared.get("shots") or []
        if cb:
            cb(f"已清空旧台词 {cleared.get('removed', 0)} 条，重新识别中")

        self.tid = self.store.new_task("asr", self.root, 1)
        self._total = 1
        self._done = 0
        self._kind = "video"
        self._cur_file = v["name"]
        try:
            self.store.update_video(vid, status="running", err="",
                                    line_count=0, line_source="pending")
            tag = os.path.splitext(os.path.basename(v["cover"] or v["name"]))[0]
            self._ev("clear", f"已清空旧台词 {cleared.get('removed', 0)} 条")
            if cb:
                cb(f"转写 {v['name']}")

            _prog = self._mk_prog(v["name"], cb, single=True)

            skip_ext = force or prev_src == "ocr"
            skip_emb = bool(force)
            rows, source, err = asr.get_lines(
                path, True, progress=_prog,
                skip_external=skip_ext, skip_embedded=skip_emb,
                cancel=self._ck())
            if err == "__cancelled__":
                return self._stop_single(vid, v, cleared, "重新识别")
            self._maybe_srt(path, rows, source, v["name"], cb)
            if rows:
                self._write_shots(vid, path, rows, tag, cb, src=source)
                self._purge_orphan_shots(old_shots, rows)
            self.store.update_video(vid, status="ok" if not err else "error",
                                    line_source=source, line_count=len(rows),
                                    err=err or "")
            self._done = 1
            self.store.finish_task(
                self.tid, "done" if not err else "error",
                f"{v['name']}：{len(rows)} 条（{source}）" + (f" · {err}" if err else ""))
            # 收尾：不推这个，浮窗上的进度卡片会一直挂在 100% 不消失
            # （单素材重识别不走整库扫描那条 task_done 分支）。
            events.ev_task_done(self.root, self._lib_name, {
                "total": 1, "skipped": 0,
                "failed": [] if not err else [v["name"]],
                "single": True, "name": v["name"],
                "lines": len(rows), "source": source,
                "cleared": cleared.get("removed", 0),
            })
            return {"ok": True, "id": vid, "lines": len(rows), "source": source,
                    "err": err, "cleared": cleared.get("removed", 0)}
        except Exception as e:
            self.store.finish_task(self.tid, "error", str(e))
            events.ev_task_done(self.root, self._lib_name, {
                "total": 1, "skipped": 0, "failed": [v["name"]],
                "single": True, "name": v["name"], "lines": 0,
                "source": "", "error": str(e),
            })
            return {"ok": False, "err": str(e)}

    def transcribe_asr(self, vid: int, cb: Callable[[str], None] = None,
                       model: str = None, lang: str = None) -> Dict[str, Any]:
        """对这个素材跑**语音识别**（faster-whisper），主要给没有硬字幕的电影用。

        和 retranscribe 的区别：完全不碰优先级链——外挂 SRT、内嵌字幕、
        OCR 画面识别一律不读，直接听声音。适用场景就是用户点的这个按钮想
        解决的：画面上没有字幕（或字幕是图片压制的花字/外语原版无字幕），
        OCR 一条都认不出来，但音轨里有对白。

        流程与 retranscribe 一致：先清空旧台词（含旧截图、全文索引），
        再转写、写同名 .srt、逐条截图。清空的理由也一样——混在一起分不清
        哪条是旧的。

        模型/设备走 config 的 ASR_*（GPU 挂载见 core/asr._mount_cuda_dir）；
        时长警告由调用方（页面弹窗）负责，这里只管跑。
        """
        v = self.store.get_video(vid)
        if not v:
            return {"ok": False, "err": "找不到这个素材"}
        path = os.path.join(self.root, v["rel"])
        if not os.path.exists(path):
            return {"ok": False, "err": "文件不在了：" + v["rel"]}

        cleared = self.store.clear_lines(vid)
        old_shots = cleared.get("shots") or []
        if cb:
            cb(f"已清空旧台词 {cleared.get('removed', 0)} 条，开始语音识别")

        self.tid = self.store.new_task("asr", self.root, 1)
        self._total = 1
        self._done = 0
        self._kind = "video"
        self._cur_file = v["name"]
        try:
            self.store.update_video(vid, status="running", err="",
                                    line_count=0, line_source="pending")
            tag = os.path.splitext(os.path.basename(v["cover"] or v["name"]))[0]
            self._ev("clear", f"已清空旧台词 {cleared.get('removed', 0)} 条")
            if cb:
                cb(f"语音识别 {v['name']}")

            _prog = self._mk_prog(v["name"], cb, single=True)
            rows, err = asr.transcribe(path, model_size=model, lang=lang,
                                       progress=_prog, cancel=self._ck())
            if err == "__cancelled__":
                return self._stop_single(vid, v, cleared, "语音识别")
            self._maybe_srt(path, rows, "asr", v["name"], cb)
            if rows:
                self._write_shots(vid, path, rows, tag, cb, src="asr")
                self._purge_orphan_shots(old_shots, rows)
            self.store.update_video(vid, status="ok" if not err else "error",
                                    line_source="asr", line_count=len(rows),
                                    err=err or "")
            self._done = 1
            self.store.finish_task(
                self.tid, "done" if not err else "error",
                f"{v['name']}：语音识别 {len(rows)} 条" + (f" · {err}" if err else ""))
            events.ev_task_done(self.root, self._lib_name, {
                "total": 1, "skipped": 0,
                "failed": [] if not err else [v["name"]],
                "single": True, "name": v["name"],
                "lines": len(rows), "source": "asr",
                "cleared": cleared.get("removed", 0),
            })
            return {"ok": True, "id": vid, "lines": len(rows), "source": "asr",
                    "err": err, "cleared": cleared.get("removed", 0)}
        except Exception as e:
            self.store.finish_task(self.tid, "error", str(e))
            events.ev_task_done(self.root, self._lib_name, {
                "total": 1, "skipped": 0, "failed": [v["name"]],
                "single": True, "name": v["name"], "lines": 0,
                "source": "", "error": str(e),
            })
            return {"ok": False, "err": str(e)}

    def _stop_single(self, vid: int, v: Dict[str, Any], cleared: Dict[str, Any],
                     what: str) -> Dict[str, Any]:
        """单素材识别被中途停掉。旧台词在建任务时就已经清空了（重新识别/语音
        识别都是"先清空再跑"），所以这里只能把这条留成 pending——
        下次再点一次或整库索引时会重新识别它，不会留下半截结果。
        """
        try:
            self.store.update_video(vid, status="pending", line_source="pending",
                                    line_count=0, err="")
        except Exception:
            pass
        self.store.finish_task(self.tid, "stopped",
                              f"{v['name']}：已停止（{what}），下次会重跑")
        try:
            events.ev_task_done(self.root, self._lib_name, {
                "total": 1, "skipped": 0, "failed": [], "single": True,
                "name": v["name"], "lines": 0, "source": "",
                "cleared": cleared.get("removed", 0), "stopped": True,
            })
        except Exception:
            pass
        return {"ok": True, "stopped": True, "id": vid, "lines": 0,
                "cleared": cleared.get("removed", 0)}

    def _purge_orphan_shots(self, old_shots: List[str],
                            rows: List[Dict[str, Any]]):
        """删掉重识别后没人引用的旧截图。

        截图名是 `{tag}_{序号}.jpg`，新结果会覆盖前 N 个；只有"这次条数变少"
        才会多出孤儿。所以这里按名字做差集——实际要删的通常是个位数，
        不会像一把全删那样动辄上百个文件（那种量级会撞上系统的批量删除保护，
        删到一半被掐断，反而留下更乱的残局）。
        """
        keep = {r.get("shot") for r in rows if r.get("shot")}
        gone = [p for p in old_shots if p and p not in keep]
        # 保险：正常情况这个差集是个位数（只有"这次条数变少"才多出孤儿）。
        # 一旦算出几百个，多半是上面 shot 字段没写上——那时删下去就是把
        # 刚截好的图全删光。宁可不删（留几个孤儿 jpg 无害），也不能删错。
        if len(gone) > 30:
            return
        if not gone:
            return
        # 删文件这件事绝不能卡住索引：放到后台线程，慢/被拦都无所谓，
        # 索引线程继续往下走。
        threading.Thread(target=self._unlink, args=(list(gone),),
                         daemon=True).start()

    def _unlink(self, rels: List[str]):
        for rel in rels:
            try:
                p = os.path.join(self.cd, rel)
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                return      # 被拦就停手，别硬来
