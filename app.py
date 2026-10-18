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
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
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
    # 各功能模块脚本也带上 mtime，避免老标签页残留旧 js 导致功能错乱
    for name in ("state.js", "libs.js", "search.js", "assets.js", "gen.js",
                 "handdraw.js", "box.js", "clips.js"):
        try:
            v = int(os.path.getmtime(os.path.join(WEB, "js", name)))
        except OSError:
            v = 0
        html = html.replace(f'src="/js/{name}"', f'src="/js/{name}?v={v}"')
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@app.get("/app.js")
def js():
    return send_page("app.js", "application/javascript")


@app.get("/style.css")
def css():
    return send_page("style.css", "text/css")


@app.get("/js/<path:name>")
def js_file(name: str):
    """web/js 下的前端功能模块（按功能拆分后的 js/*.js），本地自持、断网可用。"""
    base = os.path.abspath(os.path.join(WEB, "js"))
    p = os.path.abspath(os.path.join(base, name))
    # 防目录穿越
    if not p.startswith(base + os.sep):
        return "forbidden", 403
    if not os.path.isfile(p):
        return "not found", 404
    mime = {".js": "application/javascript", ".mjs": "application/javascript",
            ".css": "text/css"}.get(os.path.splitext(p)[1].lower(),
                                  "application/octet-stream")
    with open(p, "rb") as f:
        return Response(f.read(), mimetype=mime,
                        headers={"Cache-Control": "no-store"})


@app.get("/vendor/<path:name>")
def vendor_file(name: str):
    """web/vendor 下的前端依赖（Vue / Element Plus），本地自持、断网可用。

    注意项目根另有一个 vendor/（Python 依赖），跟这个不是一回事，
    那个目录不通过 HTTP 暴露。"""
    base = os.path.abspath(os.path.join(WEB, "vendor"))
    p = os.path.abspath(os.path.join(base, name))
    # 防目录穿越：../ 拼出来的路径必须还在 base 里面
    if not p.startswith(base + os.sep):
        return "forbidden", 403
    if not os.path.isfile(p):
        return "not found", 404
    mime = {".js": "application/javascript", ".mjs": "application/javascript",
            ".css": "text/css", ".svg": "image/svg+xml",
            ".woff": "font/woff", ".woff2": "font/woff2",
            ".ttf": "font/ttf"}.get(os.path.splitext(p)[1].lower(),
                                    "application/octet-stream")
    with open(p, "rb") as f:
        return Response(f.read(), mimetype=mime,
                        headers={"Cache-Control": "no-store"})


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


@app.post("/api/libraries/<int:lid>/edit")
def api_lib_edit(lid: int):
    """编辑库：备注名 / 路径。空字段不改，只改要改的。"""
    d = request.get_json(force=True) or {}
    # 空串 / 缺失 → 不传，让 hub.update 不去动它
    payload: Dict[str, Any] = {}
    if "name" in d and d["name"] is not None:
        payload["name"] = d["name"]
    if "path" in d and d["path"] is not None and d["path"].strip():
        payload["path"] = d["path"]
    if not payload:
        return jsonify({"ok": False, "err": "至少传一个字段"}), 400
    r = HUB.update(lid, **payload)
    if not r.get("ok"):
        return jsonify(r), 400
    # 路径变了就清掉旧 path 对应的 store 缓存（cache_file 在 app.py 里）
    old = r.pop("old_abs_path", "") or ""
    if old:
        with LOCK:
            _stores.pop(old, None)
    return jsonify(r)


@app.post("/api/libraries/<int:lid>/open")
def api_lib_open(lid: int):
    """在系统文件管理器中打开这个库（Win：explorer / mac：finder / Linux：xdg-open）。"""
    lib = HUB.get(lid)
    if not lib:
        return jsonify({"ok": False, "err": "没有这个素材库"}), 404
    p = lib["path"]
    if not os.path.isdir(p):
        return jsonify({"ok": False, "err": f"目录不存在：{p}"}), 404
    try:
        if sys.platform == "win32":
            # 一定要传 /select 选中最深一级的目录，否则多级嵌套会被 Explorer 折叠
            # 直接用 os.startfile 最稳，传 raw 路径 native 会选合适的入口。
            os.startfile(p)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        return jsonify({"ok": False, "err": f"打开失败：{e}"}), 500
    return jsonify({"ok": True, "path": p})


@app.get("/api/libraries/<int:lid>/videos")
def api_lib_videos(lid: int):
    """枚举这个库里已索引的视频（按 created_at 倒序）。库不存在 / 没索引过 → 空数组。
       每条带：素材统计（台词数、frames、indexed、has_srt）+ OCR 进度。"""
    lib = HUB.get(lid)
    if not lib:
        return jsonify({"items": [], "total": 0, "exists": False}), 404
    root = lib["path"]
    db = os.path.join(root, C.CACHE_DIR, C.DB_NAME)
    out: List[Dict[str, Any]] = []
    if not os.path.isfile(db):
        return jsonify({"items": out, "total": 0, "exists": os.path.isdir(root),
                        "indexed": False, "lib": lib})
    try:
        st = IX.open_store(root)
        rows = st.list_videos("", limit=2000)
    except Exception as e:
        return jsonify({"items": [], "total": 0, "err": str(e), "lib": lib}), 500
    for v in rows:
        # 单片统计：重连一次 store 不划算，直接用 lines 表 COUNT
        try:
            with st._conn() as conn:
                r = conn.execute(
                    "SELECT COUNT(*) AS n, MIN(start) AS st, MAX(\"end\") AS en "
                    "FROM lines WHERE video_id=?", (v["id"],)).fetchone()
                vc = r["n"] or 0
                v["lines"] = vc
                v["lines_span"] = (r["st"] or 0, r["en"] or 0)
                # 该片是否有同名 .srt
                base = os.path.splitext(v.get("rel") or "")[0]
                srts = [r2["shot"] for r2 in conn.execute(
                    "SELECT shot FROM lines WHERE video_id=? AND shot IS NOT NULL AND shot != '' LIMIT 1",
                    (v["id"],)).fetchall()]
                # 同名 .srt 的实际状态
                v["srt_exists"] = bool(base and os.path.isfile(
                    os.path.join(root, base + ".srt")))
        except Exception:
            v["lines"] = v.get("lines", 0)
        v["root"] = root
        v["lib_id"] = lid
        out.append(v)
    return jsonify({"items": out, "total": len(out),
                    "exists": os.path.isdir(root), "indexed": True, "lib": lib})


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
    """生成面板摸底：**双后端**（Agnes Video 2.5 Flash 云端 / 本地 MiniMax H3）
    各自就绪状态、宽高比选项、默认像素档、提示词上限、成片落哪个目录。"""
    from core import gen as gen_mod
    try:
        info = dict(gen_mod.engine_info())
        info.update({"aspects": gen_mod.ASPECTS,
                     "agnes_aspects": sorted(set(gen_mod.AGNES_ASPECTS.values())),
                     "agnes_unsupported": list(gen_mod.AGNES_UNSUPPORTED),
                     "agnes_seconds": [gen_mod.AGNES_SECONDS_MIN,
                                       gen_mod.AGNES_SECONDS_MAX],
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
        # 只还 H3 的信号量——云端 agnes 不占 GPU，不该碰这把锁
        if params.get("backend") == "h3":
            _GEN_BUSY.release()


@app.post("/api/generate")
def api_generate():
    """发起视频生成（异步）。**双后端可选**：agnes 云端 / h3 本地 / auto 降级。
    成片落 <项目根>/AI生成/，不入库。

    root 只用于把任务进度记到某个库的 tasks 表，不影响成片落点。
    """
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False,
                        "err": "先选一个素材库（用于记录任务进度）"}), 400
    from core import gen as gen_mod
    backend = (d.get("backend") or "auto").strip().lower()
    if backend not in ("auto", "agnes", "h3"):
        return jsonify({"ok": False,
                        "err": f"未知后端 {backend}（可选 agnes / h3 / auto）"}), 400
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
        if backend == "agnes" and aspect in gen_mod.AGNES_UNSUPPORTED:
            raise ValueError(
                f"Agnes 不支持宽高比 {aspect}，可选 "
                + ", ".join(sorted(set(gen_mod.AGNES_ASPECTS.values()))))
    except ValueError as e:
        return jsonify({"ok": False, "err": str(e)}), 400
    chain = gen_mod.BACKEND_ORDER if backend == "auto" else (backend,)
    if not any(gen_mod.backend_ready(b) for b in chain):
        return jsonify({"ok": False,
                        "err": "没有就绪的视频后端：" +
                               ", ".join(f"{b}({'就绪' if gen_mod.backend_ready(b) else '缺依赖'})"
                                         for b in chain)}), 400
    # 云端 agnes 不占 GPU，可与本地 H3 并行；两个 H3 由 gen 内部锁串行
    if backend == "h3" and not _GEN_BUSY.acquire(blocking=False):
        return jsonify({"ok": False,
                        "err": "本地 H3 已有生成任务在跑（GPU 独占），"
                                "稍后再试或改用 backend=agnes 走云端"}), 409
    try:
        st = store_of(root)
        tid = st.new_task("gen", root, 100)
    except Exception as e:
        if backend == "h3":
            _GEN_BUSY.release()
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500
    params = {"prompt": prompt, "duration": duration, "aspect": aspect,
              "megapixels": megapixels, "seed": seed, "name": name,
              "ref_image": ref_image, "index": index, "backend": backend}
    threading.Thread(target=_gen_job, args=(root, tid, params),
                     daemon=True).start()
    return jsonify({"ok": True, "task_id": tid, "root": root,
                    "backend": backend,
                    "out_dir": gen_mod.output_dir(),
                    "eta_hint": "backend=agnes 云端通常几十秒~几分钟"
                                "（高峰会排队退避）；backend=h3 本地冷启动 + "
                                "12 步采样，5 秒约 10 分钟、10 秒约 40 分钟"})


# ---------------- AI 绘画（三后端生图：Agnes / SD1.5 / H3） ----------------
_AD_BUSY = threading.Semaphore(1)   # GPU 同时只能跑一个（与 _GEN_BUSY 同思路）


@app.get("/api/aidraw/info")
def api_aidraw_info():
    """绘画面板摸底：三后端就绪状态、默认后端、产物目录。"""
    from core import img_backend as ib
    try:
        return jsonify({"ok": True, "backends": ib.backends(),
                        "default": "agnes", "order": list(ib.BACKEND_ORDER),
                        "lora_default": ib.LORA_DEFAULT,
                        "out_dir": ib.paint_dir()})
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"})


@app.post("/api/aidraw/upload")
def api_aidraw_upload():
    """参考图上传（multipart）。存项目 uploads 目录，路径回传给生成接口。"""
    files = request.files.getlist("file")
    if not files:
        return jsonify({"ok": False, "err": "没有文件"}), 400
    f = files[0]
    fn = os.path.basename(f.filename or "")
    ext = os.path.splitext(fn)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return jsonify({"ok": False, "err": "参考图只支持 png/jpg/webp/bmp"}), 400
    from core import gen as gen_mod
    up = os.path.join(gen_mod.output_dir(), "_uploads")
    os.makedirs(up, exist_ok=True)
    p = os.path.join(up, f"ref_{int(time.time())}{ext}")
    f.save(p)
    return jsonify({"ok": True, "path": p, "name": fn})


def _aidraw_job(root: str, tid: int, params: dict):
    """绘画任务：进度写 tasks 表 + 实时事件通道（与 _gen_job 同一套机制）。"""
    from core import img_backend as ib
    st = store_of(root)
    lib = _lib_name_of(root)

    def tick(pct, msg):
        try:
            st.tick_task(tid, int(pct or 0), msg)
            events.ev_progress(root, lib, int(pct or 0), 100, phase="aidraw",
                               message=msg, kind="aidraw")
        except Exception:
            pass

    try:
        res = ib.gen_image(on_tick=tick, **params)
        base = os.path.basename(res.get("file") or "")
        st.finish_task(tid, "done",
                       f"完成：{base}（{res.get('w')}×{res.get('h')}，"
                       f"{res.get('note') or res.get('backend')}）")
        events.ev_task_done(root, lib, {
            "total": 1, "skipped": 0, "failed": [], "aidraw": True,
            "file": res.get("file"), "name": base,
            "backend": res.get("backend")})
    except Exception as e:
        print("[aidraw error]", e)
        st.finish_task(tid, "error", f"{type(e).__name__}: {e}")
        events.ev_task_done(root, lib, {
            "total": 1, "skipped": 0, "failed": [], "aidraw": True,
            "error": f"{type(e).__name__}: {e}"})
    finally:
        _AD_BUSY.release()


@app.post("/api/aidraw/generate")
def api_aidraw_generate():
    """发起绘画生成（异步）。三后端可选，默认 Agnes；auto 按优先级降级。

    产物落 <项目根>/AI生成/绘画/，旁边带 sidecar JSON 留痕（重掷用）。
    root 只用于把任务进度记到某个库的 tasks 表，不影响产物落点。
    """
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False,
                        "err": "先选一个素材库（用于记录任务进度）"}), 400
    from core import img_backend as ib
    backend = (d.get("backend") or "agnes").strip().lower()
    if backend not in ("agnes", "sd15", "h3", "auto"):
        return jsonify({"ok": False, "err": f"未知后端：{backend}"}), 400
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "err": "提示词不能为空"}), 400
    refs = [os.path.abspath(r) for r in (d.get("refs") or []) if r]
    for r in refs:
        if not os.path.isfile(r):
            return jsonify({"ok": False, "err": "参考图不存在：" + r}), 400
    try:
        w = int(d.get("w") or 0)
        h = int(d.get("h") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "err": "宽高必须是数字"}), 400
    if w and (w % 8 or not (256 <= w <= 1024)):
        return jsonify({"ok": False, "err": "宽必须是 8 的倍数且在 256~1024"}), 400
    if h and (h % 8 or not (256 <= h <= 1024)):
        return jsonify({"ok": False, "err": "高必须是 8 的倍数且在 256~1024"}), 400
    def _num(key, default, cast=float):
        v = d.get(key)
        return default if v is None else cast(v)

    params = {"backend": backend, "prompt": prompt,
              "negative": (d.get("negative") or "").strip(),
              "refs": refs, "seed": int(d.get("seed") or 0),
              "w": w, "h": h, "name": (d.get("name") or "").strip(),
              "lora": ("" if d.get("lora") is None
                       else str(d.get("lora")).strip()),
              "lora_scale": _num("lora_scale", ib.DEFAULT_LORA_SCALE),
              "control_strength": _num("control_strength",
                                       ib.DEFAULT_CONTROL_STRENGTH),
              "steps": _num("steps", 20, int),
              "cfg": _num("cfg", 7.0)}
    if not _AD_BUSY.acquire(blocking=False):
        return jsonify({"ok": False,
                        "err": "已经有一个绘画任务在跑（GPU 同时只能跑一个）"}), 409
    try:
        st = store_of(root)
        tid = st.new_task("aidraw", root, 100)
    except Exception as e:
        _AD_BUSY.release()
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500
    threading.Thread(target=_aidraw_job, args=(root, tid, params),
                     daemon=True).start()
    eta = {"agnes": "约 10~60 秒", "sd15": "约 5~20 秒",
           "h3": "约 5~10 分钟", "auto": "视后端而定"}[backend]
    return jsonify({"ok": True, "task_id": tid, "root": root,
                    "out_dir": ib.paint_dir(), "eta_hint": eta})


@app.get("/api/aidraw/outputs")
def api_aidraw_outputs():
    """绘画产物列表（新的在前，带 sidecar 参数）。"""
    from core import img_backend as ib
    try:
        return jsonify({"ok": True, "items": ib.list_outputs()})
    except Exception as e:
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"})


@app.post("/api/aidraw/reroll")
def api_aidraw_reroll():
    """重掷：按某张产物的 sidecar 参数复现，只换新种子（seed=0 即随机）。"""
    d = request.get_json(force=True) or {}
    f = (d.get("file") or "").strip()
    if not f or not os.path.isfile(f):
        return jsonify({"ok": False, "err": "文件不存在：" + f}), 400
    side = os.path.splitext(f)[0] + ".json"
    if not os.path.isfile(side):
        return jsonify({"ok": False, "err": "没有参数留痕（sidecar JSON 缺失）"}), 400
    try:
        with open(side, encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception as e:
        return jsonify({"ok": False, "err": f"sidecar 读取失败：{e}"}), 400
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False,
                        "err": "先选一个素材库（用于记录任务进度）"}), 400
    from core import img_backend as ib
    meta_lora = meta.get("lora")
    meta_scale = meta.get("lora_scale")
    meta_ctrl = meta.get("control_strength")
    params = {"backend": meta.get("backend") or "agnes",
              "prompt": meta.get("prompt") or "",
              "negative": meta.get("negative") or "",
              "refs": [r for r in (meta.get("refs") or [])
                       if r and os.path.isfile(r)],
              "seed": 0,   # 0 = 随机新种子
              "w": int(meta.get("w") or 0), "h": int(meta.get("h") or 0),
              "lora": "" if meta_lora is None else str(meta_lora),
              "lora_scale": ib.DEFAULT_LORA_SCALE if meta_scale is None
                            else float(meta_scale),
              "control_strength": (ib.DEFAULT_CONTROL_STRENGTH
                                   if meta_ctrl is None else float(meta_ctrl)),
              "steps": int(meta.get("steps") or 20),
              "cfg": float(meta.get("cfg") or 7.0)}
    if not params["prompt"]:
        return jsonify({"ok": False, "err": "sidecar 里没有提示词"}), 400
    if not _AD_BUSY.acquire(blocking=False):
        return jsonify({"ok": False,
                        "err": "已经有一个绘画任务在跑（GPU 同时只能跑一个）"}), 409
    try:
        st = store_of(root)
        tid = st.new_task("aidraw", root, 100)
    except Exception as e:
        _AD_BUSY.release()
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500
    threading.Thread(target=_aidraw_job, args=(root, tid, params),
                     daemon=True).start()
    return jsonify({"ok": True, "task_id": tid, "root": root,
                    "out_dir": ib.paint_dir(),
                    "eta_hint": "重掷（新种子，同参数）"})


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
    """第 1 步：用文稿或字幕建工程（同步，很快）。工程落 <项目根>/AI生成/手绘/。"""
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


@app.post("/api/handdraw/auto")
def api_handdraw_auto():
    """一键全自动：文稿 → 分幕 → 标注 → 批准 → 逐幕渲染 → 混流。省掉中间手动步骤。"""
    from core import handdraw as hd_mod
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or active_root() or "").strip())
    text = (d.get("text") or "").strip()
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False, "err": "先选一个素材库"}), 400
    if not text:
        return jsonify({"ok": False, "err": "先写要讲解的内容"}), 400
    if not _HD_BUSY.acquire(blocking=False):
        return jsonify({"ok": False, "err": "已经有一个手绘任务在跑了"}), 409
    try:
        st = store_of(root)
        tid = st.new_task("handdraw", root, 100)
    except Exception as e:
        _HD_BUSY.release()
        return jsonify({"ok": False, "err": f"{type(e).__name__}: {e}"}), 500

    def fn(tick):
        tick(3, "准备工程（文稿 → 分幕）")
        return hd_mod.auto_project(
            root=root, text=text, name=(d.get("name") or "").strip(),
            title=(d.get("title") or "").strip(),
            target_sec=int(d.get("target_sec") or 30), tick=tick)

    threading.Thread(target=_handdraw_job,
                     args=(root, tid, "auto", fn), daemon=True).start()
    return jsonify({"ok": True, "task_id": tid, "root": root,
                    "eta_hint": "建工程→分幕→渲染各幕→配音合成，整体几分钟"})


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
# 正在跑的索引器实例：root -> Indexer，给「停止」接口用来找到线程里的那个对象。
# 只在任务运行期间存在，跑完/出错都会摘掉（同一条路径同时只留最新一个）。
RUNNING_SCANS = {}
_SCAN_LOCK = threading.Lock()


def _register_scan(ix) -> None:
    with _SCAN_LOCK:
        RUNNING_SCANS[ix.root] = ix


def _unregister_scan(ix) -> None:
    with _SCAN_LOCK:
        if RUNNING_SCANS.get(ix.root) is ix:
            RUNNING_SCANS.pop(ix.root, None)


def start_scan(path: str, use_asr: bool, with_gif: bool, retry_empty: bool = True):
    def job():
        ix = None
        try:
            ix = IX.Indexer(path, use_asr=use_asr, with_gif=with_gif,
                            retry_empty=retry_empty)
            # 把库名喂给索引器，进度事件里才带得上是哪个库
            ix._lib_name = _lib_name_of(path)
            _register_scan(ix)
            ix.run()
            HUB.touch_scan(path)
        except Exception as e:
            print("[scan error]", e)
        finally:
            if ix is not None:
                _unregister_scan(ix)
    threading.Thread(target=job, daemon=True).start()


@app.post("/api/scan/stop")
def api_scan_stop():
    """停止正在跑的索引/识别任务（中途终止）。

    已经处理完的素材都已经落库了，下次点「索引」会按 md5 直接跳过它们、
    从断点接着往下跑；正在识别到一半的那一条会被留成「还没识别」，
    下次会重新识别它（不会留下半截台词）。
    """
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or "").strip()) if d.get("root") else ""
    with _SCAN_LOCK:
        ix = RUNNING_SCANS.get(root) if root else None
        if ix is None and not root and len(RUNNING_SCANS) == 1:
            ix = next(iter(RUNNING_SCANS.values()))   # 不指定库且只有一个在跑
        if ix is None and root:
            # 路径大小写/斜杠写法不一致时兜一次：按规范化路径再比一遍
            key = os.path.normcase(root)
            for r, v in RUNNING_SCANS.items():
                if os.path.normcase(os.path.abspath(r)) == key:
                    ix = v
                    break
    if ix is None:
        return jsonify({"ok": False, "err": "没有正在跑的索引任务"}), 404
    ix.request_stop()
    return jsonify({"ok": True, "root": ix.root, "lib": ix._lib_name or "",
                    "file": ix._cur_file or ""})


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
        ix = None
        try:
            ix = IX.Indexer(path, use_asr=False, with_gif=True, no_lines=True)
            ix._lib_name = "AI 生成"
            _register_scan(ix)
            n = 0
            for f in IX.list_video_files(path):
                if ix._cancel.is_set():
                    ix.stopped = True
                    break
                try:
                    ix.index_file(f)
                    n += 1
                except Exception as e:
                    print(f"内置 AI 库索引失败 {f}: {e}", file=sys.stderr)
            if n:
                print(f"内置 AI 库：索引 {n} 个生成产物 → {path}")
        except Exception as e:
            print("内置 AI 库索引失败：", e)
        finally:
            if ix is not None:
                _unregister_scan(ix)
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
        ix = None
        try:
            ix = IX.Indexer(root, use_asr=True, with_gif=False)
            ix._lib_name = _lib_name_of(root)
            _register_scan(ix)
            # 旧台词已在 retranscribe 里清空：库里的行、全文索引、磁盘上的旧截图
            # 一起删，再按当前优先级链重新识别。
            # force=True 时连同内嵌字幕也跳过，一律走 OCR 重跑。
            ix.retranscribe(vid, force=bool(d.get("force")))
        except Exception as e:
            print("[retranscribe error]", e)
        finally:
            if ix is not None:
                _unregister_scan(ix)

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
        ix = None
        try:
            ix = IX.Indexer(root, use_asr=True, with_gif=False)
            ix._lib_name = _lib_name_of(root)
            _register_scan(ix)
            ix.transcribe_asr(vid, model=model)
        except Exception as e:
            print("[transcribe_asr error]", e)
        finally:
            if ix is not None:
                _unregister_scan(ix)

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
        # 滚动分页：带 limit 时返回 {items,total}，否则返回完整数组（兼容旧调用方）
        limit = request.args.get("limit")
        offset = int(request.args.get("offset") or 0)
        try:
            limit = int(limit) if limit else None
        except ValueError:
            limit = None
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
        if limit is not None:
            total = len(out)
            page = out[offset:offset + limit]
            return jsonify({"items": page, "total": total,
                            "offset": offset, "limit": limit})
        return jsonify(out)
    root = request.args.get("root") or active_root()
    if not root:
        return jsonify([])
    rows = store_of(root).list_videos(q)
    for r in rows:
        r["duration_str"] = store_mod.fmt_ts(r.get("duration") or 0)
        r["root"] = os.path.abspath(root)
    return jsonify(rows)


@app.get("/api/ai-gallery")
def api_ai_gallery():
    """扫描项目「AI生成」目录，返回图片 + 视频产物（gen 成片 / aidraw 绘画 / 手绘成片）。

    这些产物不进素材库索引，单独给「素材」页的「AI生成素材库」视图用。
    排除「手绘」工程目录（里面是线稿 / 分区检查图 / 渲染中间帧，不是最终成片）。
    """
    from core import handdraw as hd_mod
    ai_dir = os.path.join(hd_mod.BASE, hd_mod.OUT_DIR_NAME)
    if not os.path.isdir(ai_dir):
        return jsonify([])
    # gif 不算成片：AI生成/.frameseek/gifs/ 下全是预览动图，混进素材库会刷屏
    IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    VID = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
    # 这些目录里全是缓存 / 中间产物，不是给用户看的成片，一律跳过：
    #   .frameseek  = 封面图(covers)、动图(gifs)、台词截图(shots)等索引缓存
    #   手绘        = 线稿、分区检查图、逐幕渲染帧
    SKIP_DIRS = {".frameseek", "手绘", "工程", "projects", "temp", "tmp"}
    out = []
    for root, dirs, files in os.walk(ai_dir):
        rel_root = root.replace("\\", "/")
        # 就地裁剪 dirs，让 os.walk 不再往下钻
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in IMG:
                t = "image"
            elif ext in VID:
                t = "video"
            else:
                continue
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out.append({
                "type": t,
                "file": fp,
                "name": f,
                "rel": os.path.relpath(fp, ai_dir).replace("\\", "/"),
                "mtime": int(st.st_mtime),
                "size": st.st_size,
            })
    out.sort(key=lambda r: -r["mtime"])
    return jsonify(out)


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
                        "rel": v.get("rel") or "",
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

    2026-10-15 起这些文件落在**项目目录**下（<BASE>/AI生成/手绘/、<BASE>/AI生成/_uploads/），
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
def _srt_first_text(srt_path: str) -> str:
    """读外挂 SRT 的第一句台词（烧字幕时顺手写在那儿，用来回补卡片标题）"""
    try:
        with open(srt_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return ""
    lines = [l.strip() for l in txt.replace("\r\n", "\n").split("\n")]
    for i, l in enumerate(lines):
        if "-->" in l and i + 1 < len(lines) and lines[i + 1]:
            return lines[i + 1]
    return ""


def _clip_entry(root: str, lib_name: str, fn: str, with_dur: bool = True) -> dict:
    """把 clips 目录下的一个 mp4 整理成前端卡片要的一条记录"""
    cd = IX.cache_dir(root)
    full = os.path.join(cd, C.CLIP_DIR, fn)
    rel = f"{C.CLIP_DIR}/{fn}"
    stem, _ = os.path.splitext(fn)
    # 文件名格式：<影片名[:24]>_<起点秒>_<时间戳>.mp4，末两段是起点秒和时间戳
    src_name, start = "", 0.0
    parts = stem.rsplit("_", 2)
    if len(parts) == 3:
        src_name = parts[0]
        try:
            start = float(parts[1])
        except ValueError:
            start = 0.0
    srt_path = os.path.splitext(full)[0] + ".srt"
    has_srt = os.path.exists(srt_path)
    d = {"rel": rel, "name": fn, "root": root, "lib": lib_name,
         "video": src_name or stem, "start": start,
         "text": _srt_first_text(srt_path) if has_srt else "",
         "sub": has_srt, "mtime": 0.0, "size": 0, "duration": 0.0}
    try:
        stt = os.stat(full)
        d["mtime"] = stt.st_mtime
        d["size"] = stt.st_size
    except OSError:
        pass
    if with_dur:
        try:
            d["duration"] = float(media.video_meta(full).get("duration") or 0)
        except Exception:
            d["duration"] = 0.0
    return d


@app.get("/api/clips")
def api_clips():
    """列出已导出的片段：扫每个素材库自己的 <库>/.frameseek/clips，
    历史导出的也在里面（不再只是本次会话内存里的那几条）"""
    scope = request.args.get("scope") or "all"
    want_dur = request.args.get("dur", "1") not in ("0", "false", "")
    libs = HUB.list_all(with_builtin=True)
    roots = []
    if scope == "active":
        roots = [{"path": active_root(), "name": ""}]
    else:
        seen = set()
        for L in libs:
            p = os.path.abspath(L.get("path") or "")
            if not p or not os.path.isdir(p):
                continue
            k = os.path.normcase(p)
            if k in seen:
                continue
            seen.add(k)
            roots.append({"path": p, "name": L.get("name") or os.path.basename(p)})

    jobs = []
    for r in roots:
        cd = os.path.join(IX.cache_dir(r["path"]), C.CLIP_DIR)
        if not os.path.isdir(cd):
            continue
        for fn in sorted(os.listdir(cd)):
            if os.path.splitext(fn)[1].lower() not in (".mp4", ".mov", ".mkv", ".webm"):
                continue
            jobs.append((r["path"], r["name"], fn))
    # 时长要 ffprobe，串行扫几十个文件会明显卡，8 并发刚好
    out = []
    if jobs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_clip_entry, rp, rn, fn, want_dur)
                    for rp, rn, fn in jobs]
            for f in futs:
                try:
                    out.append(f.result())
                except Exception:
                    continue
    out.sort(key=lambda d: d.get("mtime") or 0, reverse=True)
    return jsonify({"ok": True, "clips": out, "total": len(out),
                    "bytes": sum(d.get("size") or 0 for d in out),
                    "libs": sorted({d.get("lib") or "" for d in out if d.get("lib")})})


@app.post("/api/clips/delete")
def api_clips_delete():
    """删掉一个已导出片段（连带它的 .srt）。只作用于各库自己的 clips 目录"""
    d = request.get_json(force=True) or {}
    root = os.path.abspath(d.get("root") or active_root())
    if not root:
        return jsonify({"ok": False, "err": "没指定素材库"}), 400
    p = cache_file(root, d.get("rel", ""))
    if not os.path.isfile(p):
        return jsonify({"ok": False, "err": "文件不存在"}), 404
    try:
        os.remove(p)
        srt = os.path.splitext(p)[0] + ".srt"
        if os.path.exists(srt):
            os.remove(srt)
    except OSError as e:
        return jsonify({"ok": False, "err": f"删除失败：{e}"}), 500
    return jsonify({"ok": True})


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
    # 烧字幕三档：auto（按来源自动）/ off / force。也接受旧版布尔。
    sub_mode = d.get("subtitle", "auto")
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
    sub_notes = []
    for ln in items:
        v = st.get_video(ln["video_id"])
        if not v:
            continue
        src = os.path.abspath(resolve(root, v["rel"]))
        s = max(0.0, ln["start"] - pre)
        e = (ln.get("end") or ln["start"]) + post
        tag = "".join(ch if ch not in '\\/:*?"<>| .' else "_"
                      for ch in f"{os.path.splitext(v['name'])[0][:24]}_{ln['start']:.2f}")
        rel = f"{C.CLIP_DIR}/{tag}_{int(time.time()) % 100000}.mp4"
        dst = os.path.join(IX.cache_dir(root), rel)
        # OCR 来源的画面本来就有字幕，auto 模式下不烧（core.media.decide_burn）
        burn, note = media.decide_burn(sub_mode, v.get("line_source") or ln.get("src") or "")
        if note and note not in sub_notes:
            sub_notes.append(note)
        srt_path = None
        if burn:
            # 窗口内有台词才生成 SRT；时间从库内绝对秒平移成片段相对秒，
            # 超出片段末尾的部分裁掉（media.write_srt_at 干这事）
            dur = max(0.2, e - s)
            sub_rows = []
            for l2 in st.lines_of(ln["video_id"]):
                t0 = float(l2.get("start") or 0)
                t1 = float(l2.get("end") or t0 + 2)
                if t1 <= s or t0 >= e:
                    continue
                sub_rows.append({"text": l2.get("text", ""),
                                 "start": max(0.0, t0 - s),
                                 "end": min(dur, t1 - s)})
            if sub_rows:
                srt_path = media.write_srt_at(
                    os.path.splitext(dst)[0] + ".srt", sub_rows, dur=dur)
        if media.export_clip(src, s, e, dst, reencode=not fast, srt=srt_path):
            out.append({"rel": rel, "name": os.path.basename(dst), "video": v["name"],
                        "text": ln.get("text", ""), "start": s, "end": e, "root": root,
                        "sub": bool(srt_path), "sub_skipped": bool(note and not srt_path),
                        "video_src": v.get("line_source") or ""})
    r = {"ok": True, "clips": out, "root": root, "sub_mode": str(sub_mode).lower(),
         "dir": os.path.join(IX.cache_dir(root), C.CLIP_DIR)}
    if sub_notes:
        r["sub_note"] = sub_notes[0]
    return jsonify(r)


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
