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
                 "box.js", "clips.js"):
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
    libs = HUB.list()
    total = {"videos": sum(l["videos"] for l in libs),
             "lines": sum(l["lines"] for l in libs),
             "duration": sum(l["duration"] for l in libs)}
    return jsonify({
        "libraries": libs,
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


@app.post("/api/video/retranscribe")
def api_video_retranscribe():
    """只对这一个素材重新识别台词（忽略已有结果，强制重跑一遍优先级链）"""
    d = request.get_json(force=True) or {}
    root = os.path.abspath((d.get("root") or "").strip())
    vid = int(d.get("video_id") or 0)
    if not root or not vid:
        return jsonify({"ok": False, "err": "参数不全"}), 400
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
                r["lib_id"] = int(lib.get("id") or 0)
                out.append(r)
        out.sort(key=lambda r: (r.get("lib", ""), r.get("name", "")))
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

    只读一次 GROUP BY 聚合（line_box_counts），不跑 verify，也不逐片开 SQL，
    所以片子再多也是毫秒级——点进某个素材才做逐条核算。
    """
    out = []
    for lib in HUB.list():
        if not lib.get("exists"):
            continue
        try:
            st = store_of(lib["path"])
            counts = st.line_box_counts()
        except Exception:
            continue
        for v in st.list_videos(limit=5000):
            if not (v.get("line_count") or 0):
                continue
            total, boxed = counts.get(int(v["id"]), (int(v.get("line_count") or 0), 0))
            out.append({"id": v["id"], "name": v["name"], "root": lib["path"],
                        "rel": v.get("rel") or "",
                        "lib": lib["name"], "lib_id": int(lib["id"]),
                        "lines": total,
                        "boxed": boxed,
                        "no_box": total - boxed,
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


# ---------------- 轨道探测（导出前挑音轨 / 字幕轨） ----------------
def _as_int_or_none(v):
    """把前端传来的轨道索引转成 int；None / 空串 / 'null' / 非法值一律当「没选」。

    前端下拉的值是字符串，还得能表达「这一项没选」，所以统一在这里收敛，
    免得每个调用点各写一遍 try/except。
    """
    if v is None or v == "" or v == "null":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _is_auto_mode(mode) -> bool:
    """「导出字幕」是不是 auto 档。只有 auto 才走「内嵌字幕回退」那条路：
    force/off 是用户明确表了态，不该被自动逻辑改写。旧的布尔接口也算表态。"""
    if mode is None or mode == "":
        return True
    if isinstance(mode, bool):
        return False
    return str(mode).strip().lower() in ("auto", "none", "null")


@app.get("/api/video/tracks")
def api_video_tracks():
    """探一个素材的**音轨 / 字幕轨清单**，给导出前「挑一条」用。

    按 video_id + root 定位（video_id 只在库内唯一）。轨道**现探不入库**：
    ffprobe 只读容器头，几十毫秒；片源换轨 / 重新压制后下次探测自然就是新的，
    缓存进库反而会脏。

    multi=True = 「有得挑」：音轨 ≥2 条，或者存在内嵌字幕轨——后者意味着字幕
    至少有两种来源可选（项目台词库 vs 内嵌轨），也值得让用户选一下。
    """
    root = os.path.abspath(request.args.get("root") or active_root())
    if not root:
        return jsonify({"ok": False, "err": "没指定素材库"}), 400
    vid = _as_int_or_none(request.args.get("video_id"))
    if vid is None:
        return jsonify({"ok": False, "err": "缺 video_id"}), 400
    v = store_of(root).get_video(vid)
    if not v:
        return jsonify({"ok": False, "err": "找不到素材"}), 404
    src = os.path.abspath(resolve(root, v["rel"]))
    if not os.path.isfile(src):
        return jsonify({"ok": False, "err": "视频文件不在了：" + v["rel"]}), 404
    t = media.list_tracks(src)
    return jsonify({"ok": True, "video": v["name"],
                    "audio": t["audio"], "subtitle": t["subtitle"],
                    "multi": len(t["audio"]) > 1 or len(t["subtitle"]) > 0})


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
    libs = HUB.list_all()
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
    # 轨道选择：都是 ffprobe 的绝对流索引，由 /api/video/tracks 探出来给前端挑。
    # 不给（None）= 保持原行为：音轨交给 ffmpeg 挑、字幕走台词库的三档判断。
    audio_track = _as_int_or_none(d.get("audio_track"))
    sub_track = _as_int_or_none(d.get("sub_track"))
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
    # 同一批导出里同一个片源会被反复问「有没有内嵌字幕轨」，ffprobe 每次几十毫秒，
    # 一次导出几十个片段时攒起来可观，按路径缓存一下。
    _auto_sub_cache = {}
    for ln in items:
        v = st.get_video(ln["video_id"])
        if not v:
            continue
        src = os.path.abspath(resolve(root, v["rel"]))
        s = max(0.0, ln["start"] - pre)
        e = (ln.get("end") or ln["start"]) + post
        tag = "".join(ch if ch not in '\\/:*?"<>| .' else "_"
                      for ch in f"{os.path.splitext(v['name'])[0][:24]}_{ln['start']:.2f}")
        # 同一秒内连续导两条会撞名、后一条静默覆盖前一条（实测很容易发生：
        # 先导 force 再导 auto，看起来就像「强制烧录没生效」）。撞名时加序号。
        stem = f"{tag}_{int(time.time()) % 100000}"
        rel = f"{C.CLIP_DIR}/{stem}.mp4"
        dst = os.path.join(IX.cache_dir(root), rel)
        n = 2
        while os.path.exists(dst):
            rel = f"{C.CLIP_DIR}/{stem}_{n}.mp4"
            dst = os.path.join(IX.cache_dir(root), rel)
            n += 1
        line_src = v.get("line_source") or ln.get("src") or ""
        # 本次这条用哪条字幕轨。不能直接改写循环外的 sub_track——那样下一次
        # 迭代会带着上一条的回退结果，多片段导出时第二片起全被污染。
        sti = sub_track
        if sti is not None:
            # 显式选了内嵌字幕轨：就烧它，不吃 auto「OCR 源跳过」那一档判断
            burn, note = True, ""
        else:
            # OCR 来源的画面本来就有字幕，auto 模式下不烧（core.media.decide_burn）
            burn, note = media.decide_burn(sub_mode, line_src)
            # 内嵌字幕回退：片源自带字幕轨时烧它，别让原片字幕导出后凭空消失。
            # 只在 auto 档生效（force/off 是用户明确表态，不被改写）；
            # ocr 源由 auto_embedded_sub 自己挡掉（画面已有字幕，再烧会重影）。
            if _is_auto_mode(sub_mode) and os.path.isfile(src):
                if src not in _auto_sub_cache:
                    _auto_sub_cache[src] = media.auto_embedded_sub(src, line_src)
                _si = _auto_sub_cache[src]
                if _si is not None:
                    sti, burn = _si, True
                    note = "已烧片源内嵌字幕轨（保留原片字幕）"
            if note and note not in sub_notes:
                sub_notes.append(note)
        srt_path = None
        if burn and sti is None:
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
        if media.export_clip(src, s, e, dst, reencode=not fast, srt=srt_path,
                             audio_track=audio_track, sub_track=sti):
            out.append({"rel": rel, "name": os.path.basename(dst), "video": v["name"],
                        "text": ln.get("text", ""), "start": s, "end": e, "root": root,
                        "sub": bool(srt_path or sti is not None),
                        "sub_track": sti, "audio_track": audio_track,
                        "sub_skipped": bool(note and not srt_path and sti is None),
                        "video_src": v.get("line_source") or ""})
    r = {"ok": True, "clips": out, "root": root, "sub_mode": str(sub_mode).lower(),
         "audio_track": audio_track, "sub_track": sub_track,
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
