# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - Flask Web 服务

启动： python app.py  （默认 http://127.0.0.1:8771）

两级库：
  hub.db（项目目录）        登记了哪些素材文件夹
  <素材目录>/.frameseek/index.db   该目录自己的视频/台词索引，随素材走
"""
import json
import os
import re
import sys
import threading
import time
import webbrowser
from typing import Any, Dict

from flask import Flask, request, jsonify, Response, abort

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 第三方依赖都装在 vendor 里（RapidOCR / onnxruntime / flask-sock…）
# 必须在 import core 之前插，否则 flask_sock 找不到、实时推送会退化成 SSE
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from core import config as C
from core import indexer as IX
from core import media
from core import store as store_mod
from core import ocr as ocr_mod
from core import events
from core import verify as V
from core.hub import HUB, HUB_DB

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")

app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False

LOCK = threading.Lock()
_stores: dict = {}


@app.after_request
def no_cache(resp):
    """API 一律不缓存：否则删除/索引之后列表还是旧的，看起来像"点了没反应" """
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


# ---------------- 基础 ----------------
def store_of(root: str):
    root = os.path.abspath(root)
    with LOCK:
        if root not in _stores:
            _stores[root] = IX.open_store(root)
        return _stores[root]


def active_root() -> str:
    return HUB.active_path()


def setting_bool(k: str, default: bool) -> bool:
    v = HUB.get_setting(k, "")
    if v == "":
        return default
    return v in ("1", "true", "True")


def resolve(root: str, rel: str) -> str:
    root = os.path.abspath(root)
    p = os.path.abspath(os.path.join(root, rel))
    if os.path.normcase(p) != os.path.normcase(root) and \
       not os.path.normcase(p).startswith(os.path.normcase(root) + os.sep):
        abort(403)
    return p


def cache_file(root: str, rel: str) -> str:
    cd = os.path.abspath(IX.cache_dir(root))
    p = os.path.abspath(os.path.join(cd, rel))
    if os.path.normcase(p) != os.path.normcase(cd) and \
       not os.path.normcase(p).startswith(os.path.normcase(cd) + os.sep):
        abort(403)
    return p


def send_partial(path: str, mimetype: str = None, download: bool = False,
                 name: str = None):
    size = os.path.getsize(path)
    start, end, status = 0, size - 1, 200
    rh = request.headers.get("Range")
    if rh:
        m = re.search(r"bytes=(\d*)-(\d*)", rh)
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            status = 206
    length = max(0, end - start + 1)

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    resp = Response(gen(), status=status, mimetype=mimetype, direct_passthrough=True)
    resp.headers["Content-Length"] = str(length)
    resp.headers["Accept-Ranges"] = "bytes"
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if download:
        resp.headers["Content-Disposition"] = \
            f'attachment; filename="{name or os.path.basename(path)}"'
    return resp


# ---------------- 页面 ----------------
@app.get("/")
def index():
    """给静态资源带上文件 mtime 版本号：老标签页残留旧 JS 时也能被强制刷新"""
    with open(os.path.join(WEB, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    for name, attr in (("app.js", "src"), ("style.css", "href")):
        try:
            v = int(os.path.getmtime(os.path.join(WEB, name)))
        except OSError:
            v = 0
        html = html.replace(f'{attr}="/{name}"', f'{attr}="/{name}?v={v}"')
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@app.get("/app.js")
def js():
    return send_page("app.js", "application/javascript")


@app.get("/style.css")
def css():
    return send_page("style.css", "text/css")


def send_page(name: str, mimetype: str):
    with open(os.path.join(WEB, name), "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype=mimetype,
                        headers={"Cache-Control": "no-store"})


# ---------------- 素材库（表） ----------------
@app.get("/api/libraries")
def api_libs():
    return jsonify({"items": HUB.list(), "active": active_root(), "hub_db": HUB_DB})


@app.post("/api/libraries")
def api_lib_add():
    d = request.get_json(force=True) or {}
    r = HUB.add(d.get("path", ""), d.get("name", ""), d.get("note", ""))
    if not r.get("ok"):
        return jsonify(r), 400
    HUB.set_active(r["id"])
    return jsonify(r)


@app.delete("/api/libraries/<int:lid>")
@app.post("/api/libraries/<int:lid>/delete")
def api_lib_del(lid: int):
    lib = HUB.get(lid)
    HUB.remove(lid)
    # 只摘登记，不动磁盘：素材和该目录下的 .frameseek 索引都留着（重新登记回来索引还在）
    if lib:
        with LOCK:
            _stores.pop(os.path.abspath(lib["path"]), None)
    return jsonify({"ok": True, "id": lid, "name": (lib or {}).get("name", ""),
                    "kept_files": True})


@app.post("/api/libraries/<int:lid>/active")
def api_lib_active(lid: int):
    HUB.set_active(lid)
    lib = HUB.get(lid)
    return jsonify({"ok": True, "active": lib["path"] if lib else ""})


@app.post("/api/libraries/<int:lid>/scan")
def api_lib_scan(lid: int):
    lib = HUB.get(lid)
    if not lib:
        return jsonify({"ok": False, "err": "没有这个素材库"}), 404
    d = request.get_json(force=True) or {}
    # use_asr 这个字段名是历史遗留，现在语义是"允许 OCR 识别画面硬字幕"
    start_scan(lib["path"],
               d.get("use_asr", setting_bool("use_asr", True)),
               d.get("with_gif", setting_bool("with_gif", True)),
               d.get("retry_empty", True))
    return jsonify({"ok": True, "root": lib["path"]})


@app.post("/api/libraries/scan_all")
def api_scan_all():
    d = request.get_json(force=True) or {}
    for lib in HUB.list():
        if lib.get("exists"):
            start_scan(lib["path"],
                       d.get("use_asr", setting_bool("use_asr", True)),
                       d.get("with_gif", setting_bool("with_gif", True)),
                       d.get("retry_empty", True))
    return jsonify({"ok": True})


@app.get("/api/state")
def api_state():
    # libraries 只给用户登记的库（内置 AI 库不占卡片列表）；
    # builtin_libs 单独给素材页筛选下拉用。
    libs = HUB.list()
    builtins = HUB._builtin_ai_rows()
    total = {"videos": sum(l["videos"] for l in libs) +
                      sum(b["videos"] for b in builtins),
             "lines": sum(l["lines"] for l in libs) +
                      sum(b["lines"] for b in builtins),
             "duration": sum(l["duration"] for l in libs) +
                      sum(b["duration"] for b in builtins)}
    return jsonify({
        "libraries": libs,
        "builtin_libs": builtins,
        "active": active_root(),
        "hub_db": HUB_DB,
        "settings": {"use_asr": setting_bool("use_asr", True),
                     "with_gif": setting_bool("with_gif", True)},
        # 前端算"帧支持覆盖率"要用同一个步长（boxMark 里 hits/(span/step+1)）。
        # 以前前端硬编码 0.5，步长改成 0.2 后覆盖率虚高 2.5 倍，误报一片。
        "ocr_step": float(C.OCR_STEP),
        "total": total,
        "ocr": ocr_mod.engine_info(),
        "push": {"ws": events.has_websocket(), "sse": "/api/events"},
    })


@app.get("/api/asr/info")
def api_asr_info():
    """语音识别的设备/模型现状（GPU 挂载探测一次后有缓存，不重复花钱）。

    页面弹窗里显示：走 GPU 还是 CPU、默认模型、首次要不要下载模型。
    模型文件在本地缓存放着（~/.cache/huggingface），第二次起就是纯转写。
    """
    try:
        from core import asr as asr_mod
        return jsonify({"ok": True, **asr_mod.device_info(),
                        "models": list(C.ASR_MODELS),
                        "default_model": C.ASR_MODEL})
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"})


@app.post("/api/settings")
def api_settings():
    d = request.get_json(force=True) or {}
    for k in ("use_asr", "with_gif"):
        if k in d:
            HUB.set_setting(k, "1" if d[k] else "0")
    return jsonify({"ok": True})


# ---------------- AI 视频生成（MiniMax H3，h3gen 直调 ComfyUI 库） ----------------
# GPU 同时只能跑一个生成任务；起了就不许起第二个（排队没意义，用户看不到
# 两个进度条哪个是自己的）。锁只在本进程内有效——MCP 连接器是另一个进程，
# 两端同时发起时 GPU 自己会排队，属于可接受的边缘情况。
_GEN_BUSY = threading.Lock()


@app.get("/api/gen/info")
def api_gen_info():
    """生成面板摸底：h3gen 引擎是否就绪（无 ComfyUI 服务，直调库）、
    宽高比选项、默认像素档、提示词上限、成片落哪个目录。"""
    from core import gen as gen_mod
    try:
        info = dict(gen_mod.engine_info())
        info.update({"aspects": gen_mod.ASPECTS,
                     "default_megapixels": gen_mod.DEFAULT_MEGAPIXELS,
                     "max_prompt": gen_mod.MAX_PROMPT,
                     "out_dir": gen_mod.output_dir()})
        return jsonify(info)
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"})


@app.post("/api/gen/upload")
def api_gen_upload():
    """参考图上传（multipart）。存进项目 uploads 目录，路径交给 /api/generate。

    2026-10-15 起生成链路彻底脱离素材库：参考图/文稿/线稿等中间物都在项目目录下。
    """
    from core import gen as gen_mod
    files = request.files.getlist("file")
    if not files:
        return jsonify({"ok": False, "err": "没有文件"}), 400
    f = files[0]
    fn = os.path.basename(f.filename or "")
    ext = os.path.splitext(fn)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return jsonify({"ok": False, "err": "参考图只支持 png/jpg/webp/bmp"}), 400
    up = os.path.join(gen_mod.output_dir(), "_uploads")
    os.makedirs(up, exist_ok=True)
    p = os.path.join(up, f"ref_{int(time.time())}{ext}")
    f.save(p)
    return jsonify({"ok": True, "path": p, "name": fn})


def _gen_job(root: str, tid: int, params: dict):
    """生成任务：进度同时写 tasks 表（frameseek_task / 轮询可见）和
    实时事件通道（页面顶部进度卡可见）。"""
    from core import gen as gen_mod
    st = store_of(root)
    lib = _lib_name_of(root)

    def tick(pct, msg):
        try:
            st.tick_task(tid, int(pct or 0), msg)
            events.ev_progress(root, lib, int(pct or 0), 100, phase="gen",
                               message=msg, kind="gen")
        except Exception:
            pass

    try:
        res = gen_mod.generate(root=root, on_tick=tick, **params)
        base = os.path.basename(res.get("file") or "")
        # 成片登记进内置 AI 产物库（只登记媒体信息，不做字幕/语音识别）
        if res.get("file"):
            index_gen_file(res["file"])
        st.finish_task(tid, "done",
                       f"完成：{base}（{res.get('seconds')}s，"
                       f"{res.get('w')}×{res.get('h')}）")
        events.ev_task_done(root, lib, {
            "total": 1, "skipped": 0, "failed": [], "gen": True,
            "file": res.get("file"), "lines": 0,
            "seconds": res.get("seconds"), "name": base,
            "w": res.get("w"), "h": res.get("h")})
    except Exception as e:
        print("[generate error]", e)
        st.finish_task(tid, "error", f"{type(e).__name__}: {e}")
        events.ev_task_done(root, lib, {
            "total": 1, "skipped": 0, "failed": [], "gen": True,
            "error": f"{type(e).__name__}: {e}"})
    finally:
        _GEN_BUSY.release()


@app.post("/api/generate")
def api_generate():
    """发起 MiniMax H3 视频生成（异步）。成片落 <项目根>/生成/，不入库。

    root 只用于把任务进度记到某个库的 tasks 表，不影响成片落点。
    """
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False,
                        "err": "先选一个素材库（用于记录任务进度）"}), 400
    from core import gen as gen_mod
    prompt = (d.get("prompt") or "").strip()
    duration = float(d.get("duration") or 5.0)
    aspect = (d.get("aspect") or "16:9 (Widescreen)").strip()
    megapixels = float(d.get("megapixels") or 0.3)
    seed = int(d.get("seed") or 0)
    name = (d.get("name") or "").strip()
    ref_image = (d.get("ref_image") or "").strip()
    index = (d.get("index") or "asr").strip()
    # 和 MCP 工具同一套校验（线程里才报错就太晚了）
    try:
        gen_mod.frames_for(duration)
        if aspect not in gen_mod.ASPECTS:
            raise ValueError(f"不支持的宽高比：{aspect}")
        if not prompt:
            raise ValueError("提示词不能为空")
        if len(prompt) > gen_mod.MAX_PROMPT:
            raise ValueError(f"提示词超长（{len(prompt)} > "
                             f"{gen_mod.MAX_PROMPT} 字符上限）")
        if ref_image and not os.path.isfile(ref_image):
            raise ValueError("参考图不存在：" + ref_image)
        if index not in ("asr", "scan", "none"):
            raise ValueError("index 只支持 asr / scan / none")
    except ValueError as e:
        return jsonify({"ok": False, "err": str(e)}), 400
    if not _GEN_BUSY.acquire(blocking=False):
        return jsonify({"ok": False,
                        "err": "已经有一个生成任务在跑（GPU 同时只能跑一个）"}), 409
    try:
        st = store_of(root)
        tid = st.new_task("gen", root, 100)
    except Exception as e:
        _GEN_BUSY.release()
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500
    params = {"prompt": prompt, "duration": duration, "aspect": aspect,
              "megapixels": megapixels, "seed": seed, "name": name,
              "ref_image": ref_image, "index": index}
    threading.Thread(target=_gen_job, args=(root, tid, params),
                     daemon=True).start()
    return jsonify({"ok": True, "task_id": tid, "root": root,
                    "out_dir": gen_mod.output_dir(),
                    "eta_hint": "子进程冷启动（模型即用即加载）+ 12 步采样，"
                                "5 秒片段通常几分钟起步"})


# ---------------- 白板手绘视频 ----------------
_HD_BUSY = threading.Semaphore(1)   # 本地 CPU 渲染，同时只跑一个


@app.get("/api/handdraw/info")
def api_handdraw_info():
    """手绘面板摸底：引擎是否就绪、缺哪些包、默认音色、纸张底色。"""
    from core import handdraw as hd_mod
    try:
        return jsonify({"ok": True, **hd_mod.engine_info()})
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"})


@app.post("/api/handdraw/upload")
def api_handdraw_upload():
    """线稿/文稿上传（multipart）。存进项目 uploads 目录，返回绝对路径。

    传 .txt 当文稿、.srt 当字幕、.png 当源线稿。
    2026-10-15 起不再存素材库，跟着生成产物一起放项目目录。
    """
    from core import handdraw as hd_mod
    files = request.files.getlist("file")
    if not files:
        return jsonify({"ok": False, "err": "没有文件"}), 400
    f = files[0]
    fn = os.path.basename(f.filename or "")
    ext = os.path.splitext(fn)[1].lower()
    if ext not in (".txt", ".srt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return jsonify({"ok": False,
                        "err": "只支持 txt(文稿) / srt(字幕) / png|jpg|webp(图)"}), 400
    up = os.path.join(os.path.dirname(hd_mod.dest_path()), "_uploads")
    os.makedirs(up, exist_ok=True)
    p = os.path.join(up, f"hd_{int(time.time())}_{fn}")
    f.save(p)
    return jsonify({"ok": True, "path": p, "name": fn, "kind": ext.lstrip(".")})


@app.post("/api/handdraw/init")
def api_handdraw_init():
    """第 1 步：用文稿或字幕建工程（同步，很快）。工程落 <项目根>/手绘工程/。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip()) \
        if (d.get("root") or active_root()) else ""
    try:
        if not hd_mod.engine_ready():
            raise ValueError("手绘引擎未就绪，缺：" +
                             ", ".join(hd_mod.engine_info()["missing"]))
        res = hd_mod.init_project(
            root=root, script=(d.get("script") or "").strip(),
            srt=(d.get("srt") or "").strip(),
            title=(d.get("title") or "").strip(),
            name=(d.get("name") or "").strip())
    except ValueError as e:
        return jsonify({"ok": False, "err": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500
    res["root"] = root
    return jsonify(res)


@app.get("/api/handdraw/status")
def api_handdraw_status():
    """查工程状态：时间轴是否已批准、各幕时长、画面/字幕时长对比。"""
    from core import handdraw as hd_mod
    root = os.path.abspath((request.args.get("root") or active_root() or "").strip())
    project = (request.args.get("project") or "").strip()
    if not project:
        return jsonify({"ok": False, "err": "缺 project"}), 400
    try:
        return jsonify(hd_mod.timeline_status(root, project))
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 400


@app.post("/api/handdraw/scenes")
def api_handdraw_scenes():
    """第 2 步：分幕建议（纯本地解析字幕，不生图）。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    try:
        res = hd_mod.suggest_scenes(root=root, project=d.get("project") or "",
                                    target_sec=int(d.get("target_sec") or 30))
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 400
    res["root"] = root
    return jsonify(res)


@app.post("/api/handdraw/annotate")
def api_handdraw_annotate():
    """第 3 步：写某幕区域标注（同步，带前置校验）。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    try:
        res = hd_mod.write_annotation(
            root=root, project=d.get("project") or "",
            scene_id=d.get("scene_id") or "", image=d.get("image") or "",
            annotation=d.get("annotation") or {},
            source_start_ms=d.get("source_start_ms"))
    except ValueError as e:
        return jsonify({"ok": False, "err": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500
    res["root"] = root
    return jsonify(res)


@app.post("/api/handdraw/preview")
def api_handdraw_preview():
    """第 4 步：编号检查图（同步，快）。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    try:
        res = hd_mod.region_preview(root=root, project=d.get("project") or "",
                                    scene_id=d.get("scene_id") or "",
                                    output=(d.get("output") or "").strip())
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 400
    res["root"] = root
    return jsonify(res)


def _handdraw_job(root: str, tid: int, op: str, fn):
    """手绘长任务（渲染/混流）：进度走 tasks 表 + 实时事件。"""
    from core import handdraw as hd_mod
    st = store_of(root)
    lib = _lib_name_of(root)

    def tick(pct, msg):
        try:
            st.tick_task(tid, int(pct or 0), msg)
            events.ev_progress(root, lib, int(pct or 0), 100, phase="handdraw",
                               message=msg, kind="handdraw")
        except Exception:
            pass

    try:
        res = fn(tick)
        base = os.path.basename(res.get("output") or res.get("file") or "")
        st.finish_task(tid, "done", f"完成：{base}")
        events.ev_task_done(root, lib, {
            "total": 1, "skipped": 0, "failed": [], "handdraw": True,
            "file": res.get("output") or res.get("file"), "name": base,
            "library_file": res.get("library_file")})
    except Exception as e:
        print(f"[handdraw {op} error]", e)
        st.finish_task(tid, "error", f"{type(e).__name__}: {e}")
        events.ev_task_done(root, lib, {
            "total": 1, "skipped": 0, "failed": [], "handdraw": True,
            "error": f"{type(e).__name__}: {e}"})
    finally:
        _HD_BUSY.release()


@app.post("/api/handdraw/render")
def api_handdraw_render():
    """第 5 步：渲染单幕（异步，本地 CPU 逐笔渲染）。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    project = (d.get("project") or "").strip()
    scene_id = (d.get("scene_id") or "").strip()
    preview_ms = int(d.get("preview_ms") or 0)
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False, "err": "先选一个素材库"}), 400
    try:
        hd_mod.project_json(root, project)
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)}), 400
    if not _HD_BUSY.acquire(blocking=False):
        return jsonify({"ok": False, "err": "已经有一个手绘任务在跑了"}), 409
    try:
        st = store_of(root)
        tid = st.new_task("handdraw", root, 100)
    except Exception as e:
        _HD_BUSY.release()
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500

    def fn(tick):
        tick(5, "渲染中（本地 CPU 逐笔）")
        return hd_mod.render_scene(root=root, project=project,
                                   scene_id=scene_id, preview_ms=preview_ms)

    threading.Thread(target=_handdraw_job,
                     args=(root, tid, "render", fn), daemon=True).start()
    return jsonify({"ok": True, "task_id": tid, "root": root,
                    "eta_hint": "本地 CPU 逐笔渲染，单幕十几秒~几分钟"})


@app.post("/api/handdraw/mix")
def api_handdraw_mix():
    """第 7 步：配音+伴奏混流合成（异步，联网 TTS）。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    project = (d.get("project") or "").strip()
    index = (d.get("index") or "none").strip()   # 保留兼容，实际已停用
    try:
        hd_mod.project_json(root, project)
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)}), 400
    if not _HD_BUSY.acquire(blocking=False):
        return jsonify({"ok": False, "err": "已经有一个手绘任务在跑了"}), 409
    try:
        st = store_of(root)
        tid = st.new_task("handdraw", root, 100)
    except Exception as e:
        _HD_BUSY.release()
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500

    def fn(tick):
        tick(5, "合成中（配音 + 伴奏）")
        return hd_mod.mix(root=root, project=project, index=index)

    threading.Thread(target=_handdraw_job,
                     args=(root, tid, "mix", fn), daemon=True).start()
    return jsonify({"ok": True, "task_id": tid, "root": root,
                    "eta_hint": "Edge TTS 逐条配音 + 伴奏合成 + 编码"})


@app.post("/api/handdraw/voice")
def api_handdraw_voice():
    """音色试听（联网合成，需 allow_network=true 显式许可）。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    if not d.get("allow_network"):
        return jsonify({"ok": False,
                        "err": "配音是联网合成，需 allow_network=true 显式许可"}), 400
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    try:
        res = hd_mod.audition_voice(
            text=(d.get("text") or "").strip(),
            voices=d.get("voices") or None, allow_network=True,
            root=root if d.get("project") else "",
            project=(d.get("project") or "").strip())
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 400
    return jsonify({"ok": True, **res})


@app.post("/api/handdraw/approve")
def api_handdraw_approve():
    """批准时间轴（不批准的话正式渲染会被引擎拦下）。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    try:
        return jsonify(hd_mod.approve_timeline(root, (d.get("project") or "").strip()))
    except ValueError as e:
        return jsonify({"ok": False, "err": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 400


# ---------------- 扫描 ----------------
def start_scan(path: str, use_asr: bool, with_gif: bool, retry_empty: bool = True):
    def job():
        try:
            ix = IX.Indexer(path, use_asr=use_asr, with_gif=with_gif,
                            retry_empty=retry_empty)
            # 把库名喂给索引器，进度事件里才带得上是哪个库
            ix._lib_name = _lib_name_of(path)
            ix.run()
            HUB.touch_scan(path)
        except Exception as e:
            print("[scan error]", e)
    threading.Thread(target=job, daemon=True).start()


def _lib_name_of(path: str) -> str:
    try:
        for l in HUB.list():
            if os.path.normcase(os.path.abspath(l["path"])) == \
               os.path.normcase(os.path.abspath(path)):
                return l.get("name") or ""
    except Exception:
        pass
    return os.path.basename(path.rstrip("\\/"))


def is_ai_root(root: str) -> bool:
    """这个路径是不是内置 AI 生成产物库。

    生成物不参与台词体系（不 OCR / 不 ASR / 不导字幕 / 不复查），
    前端入口已经藏掉，这里是后端兜底——防止 MCP 或旧书签绕过界面直接打接口。
    """
    try:
        from core import gen as _gen
        p = os.path.abspath(root or "")
        return os.path.normcase(p) == os.path.normcase(
            os.path.abspath(_gen.output_dir()))
    except Exception:
        return False


def ai_root_guard(root: str):
    """台词类接口的统一拦截：命中 AI 库就返回一个 400 响应，否则 None。"""
    if is_ai_root(root):
        return jsonify({"ok": False,
                        "err": "AI 生成产物不做字幕/语音识别，这条只对素材库生效"}), 400
    return None


def scan_ai_dir(path: str):
    """后台把 AI 生成产物目录增量索引一遍（只登记媒体信息，不做字幕/语音识别）。"""
    def job():
        try:
            ix = IX.Indexer(path, use_asr=False, with_gif=True, no_lines=True)
            ix._lib_name = "AI 生成"
            n = 0
            for f in IX.list_video_files(path):
                try:
                    ix.index_file(f)
                    n += 1
                except Exception as e:
                    print(f"内置 AI 库索引失败 {f}: {e}", file=sys.stderr)
            if n:
                print(f"内置 AI 库：索引 {n} 个生成产物 → {path}")
        except Exception as e:
            print("内置 AI 库索引失败：", e)
    threading.Thread(target=job, daemon=True).start()


def index_gen_file(path: str) -> bool:
    """把一条新生成的成片登记进内置 AI 库（只登记媒体信息，不碰台词）。

    生成完立刻索引一次，素材页马上就能看到新片，不用等下次启动。
    """
    try:
        from core import gen as _gen
        root = _gen.output_dir()
        ix = IX.Indexer(root, use_asr=False, with_gif=True, no_lines=True)
        ix._lib_name = "AI 生成"
        ix.index_file(path)
        return True
    except Exception as e:
        print(f"[gen index] {path}: {e}", file=sys.stderr)
        return False


@app.post("/api/video/retranscribe")
def api_video_retranscribe():
    """只对这一个素材重新识别台词（忽略已有结果，强制重跑一遍优先级链）"""
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or "").strip())
    vid = int(d.get("video_id") or 0)
    if not root or not vid:
        return jsonify({"ok": False, "err": "参数不全"}), 400
    g = ai_root_guard(root)
    if g:
        return g
    st = store_of(root)
    v = st.get_video(vid)
    if not v:
        return jsonify({"ok": False, "err": "找不到这个素材"}), 404

    def job():
        try:
            ix = IX.Indexer(root, use_asr=True, with_gif=False)
            ix._lib_name = _lib_name_of(root)
            # 旧台词已在 retranscribe 里清空：库里的行、全文索引、磁盘上的旧截图
            # 一起删，再按当前优先级链重新识别。
            # force=True 时连同内嵌字幕也跳过，一律走 OCR 重跑。
            ix.retranscribe(vid, force=bool(d.get("force")))
        except Exception as e:
            print("[retranscribe error]", e)

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True, "name": v["name"], "root": root})


@app.post("/api/video/transcribe_asr")
def api_video_transcribe_asr():
    """对这个素材跑语音识别（faster-whisper），主要处理没有硬字幕的电影。

    和 /api/video/retranscribe 的区别：不读外挂 SRT / 内嵌字幕 / OCR，
    直接听音轨。旧台词同样先清空再写。
    """
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or "").strip())
    vid = int(d.get("video_id") or 0)
    if not root or not vid:
        return jsonify({"ok": False, "err": "参数不全"}), 400
    g = ai_root_guard(root)
    if g:
        return g
    st = store_of(root)
    v = st.get_video(vid)
    if not v:
        return jsonify({"ok": False, "err": "找不到这个素材"}), 404
    model = (d.get("model") or "").strip() or None
    if model and model not in ("tiny", "base", "small", "medium", "large-v3"):
        return jsonify({"ok": False, "err": f"不支持的模型：{model}"}), 400

    def job():
        try:
            ix = IX.Indexer(root, use_asr=True, with_gif=False)
            ix._lib_name = _lib_name_of(root)
            ix.transcribe_asr(vid, model=model)
        except Exception as e:
            print("[transcribe_asr error]", e)

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True, "name": v["name"], "root": root,
                    "model": model or ""})


@app.post("/api/video/reimport")
def api_video_reimport():
    """用户在外面改完同名 .srt，点这个重新导入台词 + 重抽截图（不重转写）"""
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or "").strip())
    vid = int(d.get("video_id") or 0)
    if not root or not vid:
        return jsonify({"ok": False, "err": "参数不全"}), 400
    g = ai_root_guard(root)
    if g:
        return g
    st = store_of(root)
    v = st.get_video(vid)
    if not v:
        return jsonify({"ok": False, "err": "找不到这个素材"}), 404
    try:
        r = IX.Indexer(root, use_asr=False, with_gif=False).reimport_lines(vid)
    except Exception as e:
        print("[reimport error]", e)
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500
    if not r.get("ok"):
        return jsonify(r), 400
    v2 = st.get_video(vid)
    return jsonify({**r, "video": dict(v2) if v2 else None})


@app.post("/api/scan")
def api_scan():
    d = request.get_json(force=True) or {}
    path = (d.get("path") or active_root() or "").strip()
    if not os.path.isdir(path):
        return jsonify({"ok": False, "err": "目录不存在"}), 400
    path = os.path.abspath(path)
    if is_ai_root(path):
        # AI 生成产物目录：不登记成素材库、不进台词链路，只登记媒体信息
        scan_ai_dir(path)
        return jsonify({"ok": True, "root": path, "cache": IX.cache_dir(path),
                        "ai": True, "id": 0})
    if d.get("use_asr") is not None:
        HUB.set_setting("use_asr", "1" if d["use_asr"] else "0")
    if d.get("with_gif") is not None:
        HUB.set_setting("with_gif", "1" if d["with_gif"] else "0")
    r = HUB.add(path)
    HUB.touch_scan(path)
    start_scan(path, d.get("use_asr", setting_bool("use_asr", True)),
               d.get("with_gif", setting_bool("with_gif", True)))
    return jsonify({"ok": True, "root": path, "cache": IX.cache_dir(path), **r})


@app.get("/api/task")
def api_task():
    root = request.args.get("root") or active_root()
    if not root:
        return jsonify({})
    return jsonify(store_of(root).latest_task(os.path.abspath(root)) or {})


@app.get("/api/tasks")
def api_tasks():
    """所有正在跑的素材库的进度"""
    out = []
    for lib in HUB.list():
        if not lib.get("exists"):
            continue
        try:
            t = store_of(lib["path"]).latest_task(lib["path"])
        except Exception:
            t = None
        if t and t.get("status") == "running":
            t["lib"] = lib["name"]
            t["root"] = lib["path"]
            out.append(t)
    return jsonify(out)


# ---------------- 实时进度（WebSocket，退路 SSE） ----------------
@app.get("/api/events")
def api_events_sse():
    """SSE 版的进度流。没有 WebSocket 时前端连这个（EventSource）。

    query `root=` 可只看某个库；不传就是全部。
    """
    root = request.args.get("root") or ""
    # 注意要**调用**生成器再传给 Response：直接传函数对象会被 werkzeug
    # 当成可迭代对象，报 "'function' object is not iterable" → 500
    resp = Response(events.sse_stream(root)(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


def _register_ws():
    """有 flask_sock 就挂真正的 WebSocket 路由；没有就只留 SSE。"""
    try:
        from flask_sock import Sock
    except Exception:
        print("[实时推送] 未装 flask_sock，退回 SSE（前端自动降级）")
        return

    sock = Sock(app)

    @sock.route("/ws")
    def ws(ws):
        # 注意：ws 是 simple_websocket.Server，**没有 .request**，
        # query 要从 Flask 的 request 里拿。
        # （曾经写成 ws.request.args，结果 500 + Flask 又往已升级的 socket 上
        #   写了第二个 HTTP 响应，浏览器直接报 "Invalid frame header"。）
        root = (request.args.get("root") or "").strip()
        sub = events.HUB.subscribe()
        try:
            ws.send(json.dumps({"type": "hello",
                               "clients": events.HUB.count(),
                               "protocol": "ws"}, ensure_ascii=False))
            while True:
                ev = sub.get(20.0)
                if ev is None:
                    ws.send(json.dumps({"type": "ping"}))   # 心跳
                    continue
                if root and ev.get("root") != root:
                    continue
                ws.send(json.dumps(ev, ensure_ascii=False))
        except Exception:
            pass                    # 浏览器关页面/断网，正常退出
        finally:
            events.HUB.unsubscribe(sub)
            sub.close()


_register_ws()


# ---------------- 查询 ----------------
@app.get("/api/videos")
def api_videos():
    q = request.args.get("q", "")
    if request.args.get("scope") == "all":
        # libs= 逗号分隔的库 id；0 表示内置 AI 库；不给=全部
        raw = request.args.get("libs", "")
        want = None
        if raw != "":
            want = set()
            for tok in raw.split(","):
                tok = tok.strip()
                if tok.lstrip("-").isdigit():
                    want.add(int(tok))
        out = []
        for lib in HUB.list_all():
            if not lib.get("exists"):
                continue
            if want is not None and int(lib.get("id") or 0) not in want:
                continue
            try:
                rows = store_of(lib["path"]).list_videos(q)
            except Exception:
                continue
            for r in rows:
                r["duration_str"] = store_mod.fmt_ts(r.get("duration") or 0)
                r["root"] = lib["path"]
                r["lib"] = lib["name"]
                r["builtin"] = bool(lib.get("builtin"))
                r["lib_id"] = int(lib.get("id") or 0)
                out.append(r)
        out.sort(key=lambda r: (r.get("builtin", False),
                                r.get("lib", ""), r.get("name", "")))
        return jsonify(out)
    root = request.args.get("root") or active_root()
    if not root:
        return jsonify([])
    rows = store_of(root).list_videos(q)
    for r in rows:
        r["duration_str"] = store_mod.fmt_ts(r.get("duration") or 0)
        r["root"] = os.path.abspath(root)
    return jsonify(rows)


@app.post("/api/video/delete")
def api_video_delete():
    """删除一个素材。delete_file=true 时连磁盘上的原文件一起删（二次确认在前端）"""
    d = request.get_json(force=True) or {}
    root = (d.get("root") or "").strip()
    vid = int(d.get("video_id") or 0)
    if not root or not vid:
        return jsonify({"ok": False, "err": "参数不全"}), 400
    root = os.path.abspath(root)
    st = store_of(root)
    v = st.get_video(vid)
    if not v:
        return jsonify({"ok": False, "err": "这个素材已经不在库里了"}), 404

    r = st.delete_video(vid)
    deleted: list = []

    # 「从库中移除」：文件还在，得记进忽略名单，否则下次索引又收进来了
    if not d.get("delete_file"):
        st.add_ignore(v["md5"], v["name"], v["rel"])

    # 缓存：封面 / 动图 / 每句台词的截图
    for rel in r.get("assets") or []:
        try:
            p = cache_file(root, rel)
            if os.path.exists(p):
                os.remove(p)
                deleted.append(rel)
        except Exception as e:
            print("[del asset]", e)

    # 原文件（只在素材库目录内才允许删）
    if d.get("delete_file"):
        p = resolve(root, v["rel"])
        if os.path.exists(p):
            try:
                os.remove(p)
                deleted.append(v["rel"])
                stem = os.path.splitext(p)[0]
                for ext in C.SUB_EXT:
                    sp = stem + ext
                    if os.path.exists(sp):
                        os.remove(sp)
                        deleted.append(os.path.basename(sp))
            except Exception as e:
                return jsonify({"ok": False, "err": f"文件删除失败：{e}"}), 500

    return jsonify({"ok": True, "name": v["name"], "deleted": deleted, "root": root,
                    "ignored": not d.get("delete_file")})


@app.get("/api/ignored")
def api_ignored_list():
    root = request.args.get("root") or active_root()
    if not root:
        return jsonify([])
    return jsonify(store_of(root).list_ignored())


@app.post("/api/ignored/restore")
def api_ignored_restore():
    """把移除过的素材放回来（下次索引会重新入库）"""
    d = request.get_json(force=True) or {}
    root = os.path.abspath(d.get("root") or "")
    if not root:
        return jsonify({"ok": False, "err": "没指定素材库"}), 400
    n = store_of(root).remove_ignore(d.get("md5", ""), all=bool(d.get("all")))
    return jsonify({"ok": True, "restored": n, "root": root})


@app.get("/api/lines")
def api_lines():
    root = request.args.get("root") or active_root()
    vid = int(request.args.get("video_id") or 0)
    st = store_of(root)
    rows = st.lines_of(vid)
    for r in rows:
        r["start_str"] = store_mod.fmt_ts(r["start"])
        r["end_str"] = store_mod.fmt_ts(r.get("end") or r["start"])
        r["root"] = os.path.abspath(root)
    return jsonify(rows)


@app.get("/api/lines/verify")
def api_lines_verify():
    """方框校验：某部素材的台词有多少条真的有画面上的框支撑。

    这是"只看台词文本"永远回答不了的问题——文本可以来自任何地方，
    方框凭证能证明"这一帧的画面上确实有这样一个框"。

    check=1 时同时返回每条的结论，前端可以标出可疑行。
    """
    from core import verify as VF
    root = request.args.get("root") or active_root()
    vid = int(request.args.get("video_id") or 0)
    if not root:
        return jsonify({"ok": False, "err": "没选中素材库"}), 400
    st = store_of(root)
    verdicts = VF.check_video(st, vid)
    passed = sum(1 for v in verdicts if v.ok and not v.skipped)
    skipped = sum(1 for v in verdicts if v.skipped)
    reasons: dict = {}
    for v in verdicts:
        for r in v.reasons:
            k = r.split(" ")[0]
            reasons[k] = reasons.get(k, 0) + 1
    checked = len(verdicts) - skipped
    out = {
        "ok": True, "total": len(verdicts), "checked": checked,
        "passed": passed, "skipped": skipped,
        "failed": checked - passed,
        "rate": round(passed / checked, 4) if checked else 0.0,
        "reasons": reasons,
    }
    if request.args.get("check"):
        out["lines"] = [{
            "id": i, "text": v.text, "start": round(v.start, 2),
            "ok": v.ok, "skipped": v.skipped, "reasons": v.reasons,
            "box": v.box,
        } for i, v in enumerate(verdicts)]
    return jsonify(out)


@app.get("/api/search")
def api_search():
    q = request.args.get("q", "")
    limit = int(request.args.get("limit", 300))
    scope = request.args.get("scope", "all")      # all | active | root
    if scope == "active" or (scope == "root" and not request.args.get("root")):
        root = request.args.get("root") or active_root()
        rows = store_of(root).search(q, limit) if root else []
        for r in rows:
            r["root"] = os.path.abspath(root)
    elif scope == "root":
        root = request.args["root"]
        rows = store_of(root).search(q, limit)
        for r in rows:
            r["root"] = os.path.abspath(root)
    else:
        rows = HUB.search_all(q, limit)
    return jsonify(rows)


# ---------------- 方框复查（台词帧 / 台词框 / 文字）----------------
def _bx_arg(key: str, default=None):
    """取参数：POST 的 JSON body 优先，其次 query string。

    复查模块一半是 GET（看图、取清单，参数在 URL 上），一半是 POST
    （现场复核、回填，参数在 body 里）。这里统一收口，两种都认，
    免得调用方因为传参位置不对就吃一个 400。
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if key in body and body[key] is not None:
            return body[key]
    return request.args.get(key, default)


def _boxaudit_ctx():
    """取复查要用的三样东西：store / 素材行 / 台词行。任一不成立就 abort。"""
    from core import boxaudit as BA
    root = os.path.abspath(str(_bx_arg("root") or active_root() or ""))
    try:
        vid = int(_bx_arg("video_id") or 0)
    except (TypeError, ValueError):
        vid = 0
    if not root or not vid or not os.path.isdir(root):
        abort(400)
    st = store_of(root)
    v = st.get_video(vid)
    if not v:
        abort(404)
    return BA, st, v, root, vid


@app.get("/api/boxaudit/list")
def api_boxaudit_list():
    """一个素材的复查清单：每条台词的文本 + 框凭证指标 + 通过与否。"""
    BA, st, v, root, vid = _boxaudit_ctx()
    rep = BA.audit_video(st, vid)
    rep["root"] = root
    return jsonify(rep)


@app.get("/api/boxaudit/frame")
def api_boxaudit_frame():
    """画好框的台词帧（image/jpeg）。

    query:
      line_id  要看哪条台词；不给就按 t 取任意时刻
      t        时刻（秒），不给取该句中点
      live=1   把现场重检的框也叠上去（会跑一次 OCR，约 1~3 秒）
      fresh=1  忽略缓存重抽这一帧
    """
    BA, st, v, root, vid = _boxaudit_ctx()
    line_id = int(request.args.get("line_id") or 0)
    row = st.get_line(line_id) if line_id else None
    if row is not None and int(row.get("video_id") or 0) != vid:
        row = None

    t = request.args.get("t")
    if t is not None:
        t = float(t)
    elif row is not None:
        s = float(row.get("start") or 0)
        e = float(row.get("end") or 0)
        t = s + max(0.0, e - s) / 2 if e > s else s + 0.2
    else:
        t = 0.0

    video = resolve(root, v["rel"])
    if not os.path.exists(video):
        abort(404)
    width = int(C.BOX_FRAME_WIDTH)

    src, w, h = BA.frame_jpg(video, root, t, width, tag=v["name"],
                             fresh=request.args.get("fresh") == "1")
    if not src:
        abort(500)

    saved = V._load_box(row) if row is not None else {}
    live = None
    if request.args.get("live") == "1" and row is not None:
        r = BA.probe(video, row, n=1,
                     tol=request.args.get("tol") or None)
        live = None
        for rec in r.get("frames") or []:
            if rec.get("box"):
                live = dict(rec["box"])
                live["__match__"] = rec.get("match") is not False
                break
    img = BA.annotate(src, saved, live,
                      stamp=(f"{t:.2f}s "
                             + ((row.get("text") or "")[:20] if row else "")).strip())
    resp = Response(img, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-T"] = f"{t:.3f}"
    return resp


@app.post("/api/boxaudit/probe")
def api_boxaudit_probe():
    """重新 OCR 识别：回画面重抽首/中/尾三帧，逐帧比对库里那个框。

    这个动作的实质就是"把这一句重新用 OCR 认一遍"——所以前端按钮写的是
    「重新 OCR 识别」而不是"现场复核"：复核是在说流程，这个才是用户
    心里想做的事（这行字对不对、框在哪）。库里有框时会顺带比一次位置差。
    """
    BA, st, v, root, vid = _boxaudit_ctx()
    d = request.get_json(silent=True) or {}
    try:
        line_id = int(d.get("line_id") or 0)
        n = int(d.get("n") or C.BOX_PROBE_FRAMES)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "err": "参数不对"}), 400
    row = st.get_line(line_id)
    if not row or int(row.get("video_id") or 0) != vid:
        return jsonify({"ok": False, "err": "找不到这条台词"}), 404
    video = resolve(root, v["rel"])
    if not os.path.exists(video):
        return jsonify({"ok": False, "err": "视频文件不在了"}), 404
    r = BA.probe(video, row, n=n,
                 tol=d.get("tol"), fresh_profile=bool(d.get("fresh_profile")))
    r["root"] = root
    return jsonify(r)


@app.post("/api/boxaudit/asr")
def api_boxaudit_asr():
    """只对这一条台词跑语音识别（faster-whisper），不落库。

    为什么不直接用整片的 /api/video/transcribe_asr：那个要先清空全部台词
    再重跑整部片（一部电影十几分钟起），而人工改一句的场景只需要听这
    2~5 秒。这里按台词区间前后各留一点余量抽音频，单句通常 1~3 秒出结果。

    返回 {"text": "...", "dur": 秒数, "device": "..."}，由用户决定采不采纳
    ——识别结果**不自动写进库**，避免把人工校对过的台词覆盖掉。
    """
    from core import asr as A
    BA, st, v, root, vid = _boxaudit_ctx()
    d = request.get_json(silent=True) or {}
    try:
        line_id = int(d.get("line_id") or 0)
        pad = float(d.get("pad") or 0.6)      # 前后各留一点，防切掉字头字尾
    except (TypeError, ValueError):
        return jsonify({"ok": False, "err": "参数不对"}), 400
    row = st.get_line(line_id)
    if not row or int(row.get("video_id") or 0) != vid:
        return jsonify({"ok": False, "err": "找不到这条台词"}), 404
    video = resolve(root, v["rel"])
    if not os.path.exists(video):
        return jsonify({"ok": False, "err": "视频文件不在了"}), 404

    s = max(0.0, float(row.get("start") or 0) - pad)
    e = float(row.get("end") or 0) + pad
    span = max(0.3, e - s)
    try:
        audio = A._decode_16k(video, s, span)
    except Exception as ex:
        return jsonify({"ok": False, "err": f"抽音频失败：{ex}"}), 400
    if audio is None or audio.size == 0:
        return jsonify({"ok": False, "err": "这一段没有音频"}), 400

    model, used, err = A.get_model(None)
    if model is None:
        return jsonify({"ok": False, "err": "语音模型加载失败：" + (err or "")}), 503
    try:
        segs, _info = model.transcribe(
            audio, language=d.get("lang") or C.ASR_LANG,
            vad_filter=C.ASR_VAD,
            vad_parameters=dict(min_silence_duration_ms=300))
        text = "".join((g.text or "") for g in segs).strip()
    except Exception as ex:
        return jsonify({"ok": False, "err": f"识别失败：{type(ex).__name__} {ex}"}), 500
    return jsonify({"ok": True, "text": text, "start": round(s, 3),
                    "dur": round(span, 3), "device": used,
                    "model": d.get("model") or C.ASR_MODEL})


@app.post("/api/boxaudit/edit")
def api_boxaudit_edit():
    """人工校对：改台词文字、起止时间，可选同时写入画面方框。

    与 backfill 的关系：backfill 只能写框（那批语音转写的老库台词本来
    没框）；这里是"这句话说得对不对、出现在哪"——两件事都能在一次
    保存里做完，不用先回填再改字。`save_box=1` 时行为等价于 backfill。
    """
    BA, st, v, root, vid = _boxaudit_ctx()
    d = request.get_json(silent=True) or {}
    try:
        line_id = int(d.get("line_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "err": "参数不对"}), 400
    row = st.get_line(line_id)
    if not row or int(row.get("video_id") or 0) != vid:
        return jsonify({"ok": False, "err": "找不到这条台词"}), 404

    text = d.get("text")
    if text is not None:
        text = str(text).strip()
    start = d.get("start")
    end = d.get("end")
    try:
        start = float(start) if start not in (None, "") else None
        end = float(end) if end not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "err": "时间格式不对"}), 400
    if start is not None and end is not None and end <= start:
        return jsonify({"ok": False, "err": "结束时间要晚于开始时间"}), 400

    out: Dict[str, Any] = {"ok": True, "line_id": line_id}
    if d.get("save_box"):
        video = resolve(root, v["rel"])
        if not os.path.exists(video):
            return jsonify({"ok": False, "err": "视频文件不在了，框没法写"}), 404
        r = BA.probe(video, row, n=int(d.get("n") or C.BOX_PROBE_FRAMES),
                     tol=d.get("tol"))
        out["probe"] = r
        if not r.get("ok"):
            out["ok"] = False
            out["err"] = "这次重新识别没通过，方框没写入：" + (r.get("detail") or "")
            return jsonify(out), 400
        box = BA.fold_probe(r.get("frames") or [])
        if not box:
            out["ok"] = False
            out["err"] = "没拿到可用的框"
            return jsonify(out), 400
        st.set_box(line_id, box)
        out["box"] = box

    if text is not None or start is not None or end is not None:
        r = st.update_line(line_id, text=text, start=start, end=end)
        if not r.get("ok"):
            return jsonify({"ok": False, "err": r.get("err") or "写入失败"}), 400
        out["line"] = r.get("line")

    # 改完立刻拿新状态自检一次，页面直接显示"存完之后过不过"
    v2 = st.get_line(line_id)
    chk = V.check_line({**(v2 or {}), "src": (v2 or {}).get("src")
                        or v.get("line_source") or ""})
    out["verify"] = chk.to_dict()
    out["audit"] = BA.audit_line(st, vid, v2)
    return jsonify(out)


@app.post("/api/boxaudit/backfill")
def api_boxaudit_backfill():
    """把现场复核通过的框回填进库。

    保留这个接口是因为 MCP / 脚本可能还在按老口径调它；页面上已经没有
    入口了（改成「编辑台词」里勾一句"同时保存方框"），行为不变。
    """
    BA, st, v, root, vid = _boxaudit_ctx()
    d = request.get_json(silent=True) or {}
    try:
        line_id = int(d.get("line_id") or 0)
        n = int(d.get("n") or C.BOX_PROBE_FRAMES)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "err": "参数不对"}), 400
    row = st.get_line(line_id)
    if not row or int(row.get("video_id") or 0) != vid:
        return jsonify({"ok": False, "err": "找不到这条台词"}), 404
    if d.get("clear"):
        st.set_box(line_id, None)
        return jsonify({"ok": True, "cleared": True, "line_id": line_id})

    video = resolve(root, v["rel"])
    r = BA.probe(video, row, n=n,
                 tol=d.get("tol"))
    if not r.get("ok"):
        return jsonify({"ok": False, "err": "现场复核没通过，不写入",
                        "detail": r.get("detail", ""), "probe": r})
    box = BA.fold_probe(r.get("frames") or [])
    if not box:
        return jsonify({"ok": False, "err": "没拿到可用的框"}), 400
    st.set_box(line_id, box)
    v2 = st.get_line(line_id)
    b2 = v2.get("box")
    if isinstance(b2, str):
        try:
            b2 = json.loads(b2)
        except Exception:
            b2 = None
    # 回填完立刻拿新凭证自检一次，让页面直接显示"补完之后过不过"
    chk = V.check_line({**(v2 or {}), "src": (v2 or {}).get("src")
                        or v.get("line_source") or ""})
    return jsonify({"ok": True, "line_id": line_id, "box": b2,
                    "verify": chk.to_dict(), "probe": r})


@app.get("/api/boxaudit/library")
def api_boxaudit_library():
    """整库的方框体检概览：每个素材有多少条带框、多少条可疑。

    只读 box_stats（一条 SQL 聚合），不跑 verify，所以 65 部片也是
    秒回——点进某个素材才做逐条核算。
    """
    out = []
    for lib in HUB.list():
        if not lib.get("exists"):
            continue
        try:
            st = store_of(lib["path"])
        except Exception:
            continue
        for v in st.list_videos(limit=5000):
            if not (v.get("line_count") or 0):
                continue
            s = st.box_stats(v["id"])
            out.append({"id": v["id"], "name": v["name"], "root": lib["path"],
                        "lib": lib["name"], "lib_id": int(lib["id"]),
                        "lines": s["total"],
                        "boxed": s["boxed"],
                        "no_box": s["total"] - s["boxed"],
                        "duration": v.get("duration") or 0,
                        "line_source": v.get("line_source") or ""})
    out.sort(key=lambda x: (-x["no_box"], -x["lines"]))
    return jsonify(out)


# ---------------- 上传 ----------------
@app.post("/api/upload")
def api_upload():
    root = request.form.get("root") or active_root()
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False, "err": "先选一个素材库"}), 400
    root = os.path.abspath(root)
    files = request.files.getlist("file")
    if not files:
        return jsonify({"ok": False, "err": "没有文件"}), 400
    up = os.path.join(IX.ensure_layout(root), C.UPLOAD_DIR)
    saved = []
    for f in files:
        fn = os.path.basename(f.filename or "")
        if not fn or os.path.splitext(fn)[1].lower() not in C.VIDEO_EXT:
            continue
        p = os.path.join(up, fn)
        i = 1
        while os.path.exists(p):
            stem, ext = os.path.splitext(fn)
            p = os.path.join(up, f"{stem}_{i}{ext}")
            i += 1
        f.save(p)
        saved.append(p)

    def job():
        ix = IX.Indexer(root,
                        use_asr=setting_bool("use_asr", True),
                        with_gif=setting_bool("with_gif", True))
        tid = ix.store.new_task("upload", root, len(saved))
        n = 0
        for p in saved:
            try:
                ix.index_file(p)
            except Exception as e:
                print("[index error]", e)
            n += 1
            ix.store.tick_task(tid, n, f"{n}/{len(saved)}")
        ix.store.finish_task(tid, "done", f"入库 {n} 个")

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True, "saved": [os.path.basename(p) for p in saved],
                    "root": root})


# ---------------- 媒体 ----------------
@app.get("/media")
def api_media():
    root = request.args.get("root") or active_root()
    rel = request.args.get("p", "")
    ext = os.path.splitext(rel)[1].lower()
    mt = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".gif": "image/gif", ".webp": "image/webp", ".mp4": "video/mp4"
          }.get(ext, "application/octet-stream")
    p = cache_file(root, rel)
    if not os.path.exists(p):
        abort(404)
    return send_partial(p, mt)


@app.get("/hd/media")
def api_hd_media():
    """手绘产物预览（检查图 / 逐幕 mp4 / 成片）。

    2026-10-15 起这些文件落在**项目目录**下（<BASE>/手绘工程/、<BASE>/生成/_uploads/），
    不在素材库里，也不能走 /media（那里只允许 cache 目录内的相对路径，防穿越）。
    这里限定「必须落在项目目录内」再放行。
    """
    from core import handdraw as hd_mod
    base = os.path.normcase(hd_mod.BASE)
    p = request.args.get("p", "")
    if not p:
        abort(404)
    full = os.path.abspath(p if os.path.isabs(p)
                           else os.path.join(hd_mod.BASE, p))
    fn = os.path.normcase(full)
    if fn != base and not fn.startswith(base + os.sep):
        abort(403)          # 只允许读 FrameSeek 项目目录内的东西
    if not os.path.isfile(full):
        abort(404)
    ext = os.path.splitext(full)[1].lower()
    mt = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".gif": "image/gif", ".webp": "image/webp", ".mp4": "video/mp4",
          ".wav": "audio/wav", ".srt": "text/plain; charset=utf-8"
          }.get(ext, "application/octet-stream")
    return send_partial(full, mt)


@app.get("/stream/<int:vid>")
def api_stream(vid: int):
    root = request.args.get("root") or active_root()
    v = store_of(root).get_video(vid)
    if not v:
        abort(404)
    p = resolve(root, v["rel"])
    if not os.path.exists(p):
        abort(404)
    return send_partial(p, "video/mp4")


@app.get("/clip")
def api_clip_file():
    root = request.args.get("root") or active_root()
    p = cache_file(root, request.args.get("p", ""))
    if not os.path.exists(p):
        abort(404)
    return send_partial(p, "video/mp4", download=request.args.get("dl") == "1",
                        name=os.path.basename(p))


# ---------------- 导出片段 ----------------
@app.post("/api/clip")
def api_clip():
    d = request.get_json(force=True) or {}
    root = os.path.abspath(d.get("root") or active_root())
    if not root:
        return jsonify({"ok": False, "err": "没指定素材库"}), 400
    st = store_of(root)
    pre = float(d.get("pre", C.CLIP_PRE))
    post = float(d.get("post", C.CLIP_POST))
    fast = bool(d.get("fast", True))
    items = []
    for lid in (d.get("line_ids") or ([d["line_id"]] if d.get("line_id") else [])):
        ln = st.get_line(int(lid))
        if ln:
            items.append(ln)
    if not items and d.get("video_id") is not None and d.get("start") is not None:
        items.append({"video_id": int(d["video_id"]), "start": float(d["start"]),
                      "end": float(d.get("end") or float(d["start"]) + 1), "text": ""})
    if not items:
        return jsonify({"ok": False, "err": "没有可导出的台词"}), 400

    out = []
    for ln in items:
        v = st.get_video(ln["video_id"])
        if not v:
            continue
        src = resolve(root, v["rel"])
        s = max(0.0, ln["start"] - pre)
        e = (ln.get("end") or ln["start"]) + post
        tag = "".join(ch if ch not in '\\/:*?"<>| .' else "_"
                      for ch in f"{os.path.splitext(v['name'])[0][:24]}_{ln['start']:.2f}")
        rel = f"{C.CLIP_DIR}/{tag}_{int(time.time()) % 100000}.mp4"
        dst = os.path.join(IX.cache_dir(root), rel)
        if media.export_clip(src, s, e, dst, reencode=not fast):
            out.append({"rel": rel, "name": os.path.basename(dst), "video": v["name"],
                        "text": ln.get("text", ""), "start": s, "end": e, "root": root})
    return jsonify({"ok": True, "clips": out, "root": root,
                    "dir": os.path.join(IX.cache_dir(root), C.CLIP_DIR)})


@app.post("/api/open")
def api_open():
    d = request.get_json(force=True) or {}
    p = d.get("path")
    if not p or not os.path.exists(p):
        return jsonify({"ok": False, "err": "路径不存在"}), 400
    try:
        os.startfile(p)  # noqa
    except Exception:
        if sys.platform == "win32":
            os.system(f'start "" "{p}"')
    return jsonify({"ok": True})


def main():
    HUB.migrate_json()
    port = int(os.environ.get("FRAMESEEK_PORT", C.PORT))
    url = f"http://{C.HOST}:{port}/"
    libs = HUB.list()
    print(f"{C.APP_LABEL}  ->  {url}")
    print(f"项目库 hub.db: {HUB_DB}   （已登记 {len(libs)} 个素材库）")

    # 后台预热：加载 OCR 引擎（第一次约 1~4 秒），顺便确认台词来源可用
    def _probe():
        try:
            info = ocr_mod.engine_info()
            if info.get("ok"):
                eng = ocr_mod.make_engine()
                if eng is None:
                    print(f"OCR 引擎加载失败：{ocr_mod._ENG_ERR}")
                else:
                    print(f"台词来源：同名 .srt → 内嵌字幕 → OCR 识别画面硬字幕"
                          f"（RapidOCR，{C.OCR_WORKERS} 进程并行，"
                          f"{C.OCR_STEP}s 网格）")
            else:
                print(f"OCR 不可用：{info.get('err')}")
        except Exception as e:
            print("OCR 预热失败：", e)
    threading.Thread(target=_probe, daemon=True).start()

    # 内置 AI 库：生成产物目录，素材页要能筛到它，所以启动时顺手增量索引。
    # 只登记媒体信息——不读字幕、不 OCR、不跑语音识别（2026-10-15）。
    def _idx_builtin():
        try:
            from core import gen as _gen
            root = _gen.output_dir()
            if os.path.isdir(root):
                scan_ai_dir(root)
        except Exception as e:
            print("内置 AI 库索引失败：", e)
    threading.Thread(target=_idx_builtin, daemon=True).start()

    for l in libs:
        print(f"  #{l['id']} {l['name']}  {l['path']}  "
              f"[{l['videos']} 个素材 / {l['lines']} 条台词]")
    # 上次进程被 kill/断电留下的 running 任务是假的（线程早没了），
    # 不清会让页面永远挂着一个 0% 的进度卡片，看着像卡住了
    try:
        stale = sum(store_of(l["path"]).reap_stale_tasks() for l in libs
                    if l.get("exists"))
        if stale:
            print(f"清理了 {stale} 个上次未完成的任务标记")
    except Exception as e:
        print("清理残留任务失败：", e)
    if os.environ.get("FRAMESEEK_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=C.HOST, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
