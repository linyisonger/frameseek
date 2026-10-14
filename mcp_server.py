# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - MCP Server（stdio）

给 WorkBuddy 等 MCP 客户端用的连接器。**不依赖 Web 服务（app.py）是否在跑**：
直接读项目目录的 hub.db（库登记表）和各素材目录下的 .frameseek/index.db
（视频/台词索引），识别任务走 Indexer 本身。

两个本机坑，这里都绕开了：
1. venv 里 anyio 半残（import anyio.abc 失败），而 mcp SDK 依赖它
   → vendor 里放了一份完整的，必须排在 site-packages 前面。
2. stdio 协议的 stdout 只归 JSON-RPC 帧。SDK 2.3 的 stdio_server 在
   启动后会把 fd 1 改道到 stderr、协议走私有副本（子进程的 print 也
   跟着去 stderr），所以 core 里零星几处 print 不会污染协议；但
   **传输启动之前**（import 期）不要往 stdout 打东西。

接入配置（写进 ~/.workbuddy/mcp.json 的 mcpServers，和别的服务器合并）：
{
  "mcpServers": {
    "frameseek": {
      "command": "C:\\\\Users\\\\Administrator\\\\.workbuddy\\\\binaries\\\\python\\\\envs\\\\default\\\\Scripts\\\\python.exe",
      "args": ["D:\\\\project\\\\AIProjects\\\\frameseek\\\\mcp_server.py"],
      "description": "拾帧 FrameSeek 本地视频素材库：跨库台词搜索、OCR/语音转写、方框校验、片段导出"
    }
  }
}
"""
import asyncio
import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "vendor"))   # 本机 venv 里 anyio 半残，用 vendor 兜底

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core import config as C  # noqa: E402
from core import gen as gen_mod  # noqa: E402
from core import handdraw as hd_mod  # noqa: E402
from core import indexer as IX  # noqa: E402
from core import media  # noqa: E402
from core import store as store_mod  # noqa: E402
from core import ocr as ocr_mod  # noqa: E402
from core import verify as V  # noqa: E402
from core import asr as asr_mod  # noqa: E402
from core.hub import HUB  # noqa: E402

ASR_MODELS_OK = ("tiny", "base", "small", "medium", "large-v3")

server = MCPServer(C.APP_NAME, title=C.APP_LABEL, instructions=(
    "拾帧 FrameSeek：本地视频素材库。多个素材文件夹登记在 hub.db，每个目录自己的索引在 "
    "<目录>/.frameseek/index.db。典型流程：frameseek_state 看库现状 → frameseek_search "
    "按台词跨库找片（结果带 root，后续操作都要带）→ frameseek_get_lines 看某片全部台词 → "
    "frameseek_export_clip 导出该句前后片段。扫描/识别任务（frameseek_scan / "
    "frameseek_retranscribe / frameseek_transcribe_asr）是异步的，立即返回，"
    "用 frameseek_task 轮询进度。frameseek_verify / frameseek_boxaudit 回答"
    "“这条台词在画面上真有框吗”，用于纠错时优先怀疑 font_dev 超标的行。"
    "改台词用 frameseek_edit_line（可同时把画面方框写进库）；想核对某句到底"
    "写对了没有，用 frameseek_recog_line（mode=ocr 看画面硬字幕 / mode=asr "
    "只听这一句的音轨），它只报告结果不落库，采纳与否由 edit_line 决定。"
    "frameseek_generate_video 用本地 MiniMax H3（直调 ComfyUI 库，无服务）按提示词"
    "生成视频，成片落项目目录 生成/（不入库），同样异步 + frameseek_task 轮询；"
    "GPU 同时只能跑一个，发起前先用 frameseek_gen_info 看引擎是否就绪。"
    "白板手绘视频是另一条线（本地 CPU 逐笔渲染，不占 GPU）：frameseek_handdraw_info 看现状 → "
    "frameseek_handdraw_init 用文稿建工程 → frameseek_handdraw_scenes 出分幕建议 → "
    "逐幕写标注 frameseek_handdraw_annotate → 渲染 frameseek_handdraw_render → "
    "试听配音 frameseek_handdraw_voice → 合成 frameseek_handdraw_mix。"
    "手绘是**分步**的（区域标注要先确认），别指望一句话出片；渲染/混流较慢，异步 + frameseek_task 轮询。"
))


# ---------------- 小工具 ----------------
def _err(msg: str) -> str:
    return json.dumps({"ok": False, "err": msg}, ensure_ascii=False)


def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


def pick_root(root: str = "") -> str:
    """root 参数优先；其次环境变量 FRAMESEEK_ROOT；最后 hub 里的激活库。"""
    r = (root or "").strip()
    if r:
        return os.path.abspath(r)
    env = os.environ.get("FRAMESEEK_ROOT")
    if env:
        return os.path.abspath(env)
    return HUB.active_path()


def _setting_bool(k: str, default: bool) -> bool:
    v = HUB.get_setting(k, "")
    if v == "":
        return default
    return v in ("1", "true", "True")


def _lib_name_of(path: str) -> str:
    try:
        for l in HUB.list():
            if os.path.normcase(os.path.abspath(l["path"])) == \
               os.path.normcase(os.path.abspath(path)):
                return l.get("name") or ""
    except Exception:
        pass
    return os.path.basename(path.rstrip("\\/"))


def _lib_json(l: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": l["id"], "name": l["name"], "path": l["path"],
            "videos": l.get("videos", 0), "lines": l.get("lines", 0),
            "duration": round(l.get("duration", 0) or 0, 1),
            "indexed": l.get("indexed", False), "exists": l.get("exists", False),
            "last_scan_at": l.get("last_scan_at")}


# ---------------- 异步任务注册表 ----------------
# 扫描一部大库可能几十分钟，MCP 调用不能干等。所有长任务都起 daemon 线程，
# 立即返回；进度写进 store 的 tasks 表，用 frameseek_task 查。
_JOBS: Dict[str, threading.Thread] = {}
_JOBS_LOCK = threading.Lock()


def _bg(key: str, fn) -> bool:
    """同一个 key 的任务还在跑就返回 False（不重复起）。"""
    with _JOBS_LOCK:
        th = _JOBS.get(key)
        if th is not None and th.is_alive():
            return False
        th = threading.Thread(target=fn, daemon=True, name=f"frameseek-{key}")
        _JOBS[key] = th
        th.start()
        return True


def _ai_root() -> str:
    """内置 AI 生成产物目录（生成物单独一档，不登记进素材库）。"""
    try:
        from core import gen as _gen
        return os.path.abspath(_gen.output_dir())
    except Exception:
        return ""


def _is_ai_root(p: str) -> bool:
    a = _ai_root()
    return bool(a) and bool(p) and \
        os.path.normcase(os.path.abspath(p)) == os.path.normcase(a)


def _ai_guard(p: str):
    """台词类工具的统一拦截：命中 AI 产物库就返回错误文案，否则 None。

    生成物没有台词这回事——不读字幕、不 OCR、不跑语音识别、不复查方框。
    """
    return ("AI 生成产物不做字幕/语音识别，这个工具只对素材库生效"
            if _is_ai_root(p) else None)


def _scan_ai_job(root: str):
    """AI 产物库扫描：只登记媒体信息（封面/动图/时长），完全不碰台词。"""
    def job():
        try:
            ix = IX.Indexer(root, use_asr=False, with_gif=True, no_lines=True)
            ix._lib_name = "AI 生成"
            for f in IX.list_video_files(root):
                try:
                    ix.index_file(f)
                except Exception as e:
                    print(f"[frameseek-mcp] ai-index {f} 失败: {e}",
                          file=sys.stderr)
        except Exception as e:
            print(f"[frameseek-mcp] ai scan {root} 失败: {e}", file=sys.stderr)
    return job


def _scan_job(root: str, use_asr: bool, with_gif: bool):
    def job():
        try:
            ix = IX.Indexer(root, use_asr=use_asr, with_gif=with_gif)
            ix._lib_name = _lib_name_of(root)
            ix.run()
            HUB.touch_scan(root)
        except Exception as e:
            print(f"[frameseek-mcp] scan {root} 失败: {e}", file=sys.stderr)
    return job


def _retranscribe_job(root: str, vid: int, force: bool):
    def job():
        try:
            ix = IX.Indexer(root, use_asr=True, with_gif=False)
            ix._lib_name = _lib_name_of(root)
            ix.retranscribe(vid, force=force)
        except Exception as e:
            print(f"[frameseek-mcp] retranscribe {root}#{vid} 失败: {e}",
                  file=sys.stderr)
    return job


def _asr_job(root: str, vid: int, model: Optional[str], lang: Optional[str]):
    def job():
        try:
            ix = IX.Indexer(root, use_asr=True, with_gif=False)
            ix._lib_name = _lib_name_of(root)
            ix.transcribe_asr(vid, model=model, lang=lang)
        except Exception as e:
            print(f"[frameseek-mcp] transcribe_asr {root}#{vid} 失败: {e}",
                  file=sys.stderr)
    return job


def _asr_device() -> Dict[str, Any]:
    """GPU/CPU 现状（探测过一次就缓存，不重复花钱）。"""
    try:
        d = asr_mod.device_info()
        return {"ok": True, **d, "models": list(C.ASR_MODELS),
                "default_model": C.ASR_MODEL}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def _gen_job(root: str, tid: int, params: Dict[str, Any]):
    """MiniMax H3 生成任务：进度写 tasks 表，和扫描/识别同一套通道。"""
    st = IX.open_store(root)

    def tick(pct, msg):
        try:
            st.tick_task(tid, int(pct or 0), msg)
        except Exception:
            pass

    def job():
        try:
            res = gen_mod.generate(root=root, on_tick=tick, **params)
            st.finish_task(tid, "done",
                           f"完成：{os.path.basename(res.get('file') or '')}"
                           f"（{res.get('seconds')}s，{res.get('lines')} 条台词）")
        except Exception as e:
            print(f"[frameseek-mcp] generate 失败: {type(e).__name__}: {e}",
                  file=sys.stderr)
            try:
                st.finish_task(tid, "error", f"{type(e).__name__}: {e}")
            except Exception:
                pass
    return job


def _gen_info() -> Dict[str, Any]:
    """生成引擎现状（h3gen 直调 ComfyUI 库，无服务）+ 参数默认值。"""
    try:
        info = dict(gen_mod.engine_info())
        info.update({"aspects": list(gen_mod.ASPECTS),
                     "default_megapixels": gen_mod.DEFAULT_MEGAPIXELS,
                     "max_prompt": gen_mod.MAX_PROMPT,
                     "out_dir": gen_mod.output_dir()})
        return info
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


# ---------------- 白板手绘：异步任务 ----------------
def _hd_job(root: str, tid: int, op: str, fn):
    """白板手绘任务：渲染/混流较慢，进度写 tasks 表，和扫描/生成同一套通道。"""
    st = IX.open_store(root)

    def tick(pct, msg):
        try:
            st.tick_task(tid, int(pct or 0), msg)
        except Exception:
            pass

    def job():
        try:
            res = fn(tick)
            st.finish_task(tid, "done",
                           res.get("note") or f"完成：{op}")
        except Exception as e:
            print(f"[frameseek-mcp] handdraw {op} 失败: {type(e).__name__}: {e}",
                  file=sys.stderr)
            try:
                st.finish_task(tid, "error", f"{type(e).__name__}: {e}")
            except Exception:
                pass
    return job


def _hd_info() -> Dict[str, Any]:
    """白板手绘引擎现状。"""
    try:
        return {"ok": True, **hd_mod.engine_info()}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


# ---------------- 工具：总览 ----------------
@server.tool()
def frameseek_state() -> str:
    """FrameSeek 总览：全部素材库（素材数/台词数/是否索引/目录在不在）、
    当前激活库、OCR 引擎是否可用、OCR 采样步长、语音识别(ASR)走 GPU 还是 CPU。
    调用其它工具前先看这个，知道有没有库、台词从哪来、识别要等多久。"""
    libs = [_lib_json(l) for l in HUB.list()]
    total = {"videos": sum(l["videos"] for l in libs),
             "lines": sum(l["lines"] for l in libs),
             "duration": round(sum(l["duration"] for l in libs), 1)}
    return _ok(libraries=libs, active=HUB.active_path(),
               settings={"use_asr": _setting_bool("use_asr", True),
                         "with_gif": _setting_bool("with_gif", True)},
               ocr_step=float(C.OCR_STEP), total=total,
               ocr=ocr_mod.engine_info(), asr=_asr_device(),
               gen=_gen_info())


@server.tool()
def frameseek_stats() -> str:
    """全部素材库合计：库数量、素材数、台词数、总时长（秒）"""
    libs = HUB.list()
    return _ok(hub_db=HUB.db_path, active=HUB.active_path(),
               libraries=len(libs),
               videos=sum(l["videos"] for l in libs),
               lines=sum(l["lines"] for l in libs),
               duration=round(sum(l["duration"] for l in libs), 1))


# ---------------- 工具：素材库管理 ----------------
@server.tool()
def frameseek_list_libraries() -> str:
    """列出所有登记的素材库（路径、素材数、台词数、是否索引过、目录是否丢失）"""
    return _ok(hub_db=HUB.db_path, active=HUB.active_path(),
               libraries=[_lib_json(l) for l in HUB.list()])


@server.tool()
def frameseek_add_library(path: str, name: str = "", note: str = "",
                          make_active: bool = True) -> str:
    """登记一个新的素材文件夹（写进 hub.db，并在该目录下建 .frameseek）。
    同一路径重复登记不会重复。登记后不会自动扫描，接着调 frameseek_scan。
    AI 生成产物目录不需要登记——它是内置只读库，单独一档。"""
    if _is_ai_root(path):
        return _err("这是内置 AI 生成产物目录，不登记成素材库（素材页可直接筛到它）")
    r = HUB.add(path, name, note)
    if not r.get("ok"):
        return json.dumps(r, ensure_ascii=False)
    if make_active:
        HUB.set_active(r["id"])
    return _ok(id=r["id"], path=r["path"], name=r["name"],
               cache=IX.cache_dir(r["path"]))


@server.tool()
def frameseek_remove_library(library_id: int) -> str:
    """从登记表移除素材库（只删登记，不动磁盘素材和 .frameseek 索引）"""
    HUB.remove(library_id)
    return _ok(id=library_id)


# ---------------- 工具：扫描 / 任务进度 ----------------
@server.tool()
def frameseek_scan(path: str = "", use_asr: Optional[bool] = None,
                   with_gif: Optional[bool] = None,
                   all_libraries: bool = False) -> str:
    """扫描素材目录入库（md5 去重；生成封面/动图/台词/台词截图）。

    大目录（几十部电影）可能跑几十分钟，所以这里**立即返回**，
    之后用 frameseek_task 轮询进度，别反复调本工具。
    all_libraries=True 时扫全部已登记库；否则扫 path（或当前激活库）。
    use_asr 字段名是历史遗留，真实语义是“允不允许对没字幕的素材跑
    OCR/语音识别”（False 会快很多，但没字幕的片子将没有台词）；
    with_gif 控制是否生成动图预览。两个都不传 = 跟随 hub 里的设置。
    """
    if use_asr is None:
        use_asr = _setting_bool("use_asr", True)
    if with_gif is None:
        with_gif = _setting_bool("with_gif", True)
    if all_libraries:
        started = []
        for l in HUB.list():
            if not l.get("exists"):
                continue
            if _bg(f"scan:{l['path']}", _scan_job(l["path"], use_asr, with_gif)):
                started.append({"name": l["name"], "path": l["path"]})
        if not started:
            return _err("没有可扫的库（登记过的目录都不存在，或都在扫描中）")
        return _ok(started=started, count=len(started),
                   note="用 frameseek_task 查进度")
    root = pick_root(path)
    if not root or not os.path.isdir(root):
        return _err("目录不存在，先用 frameseek_add_library 登记")
    if _is_ai_root(root):
        # AI 产物目录：不登记成素材库，只登记媒体信息（不 OCR / 不 ASR）
        if not _bg(f"scan:{root}", _scan_ai_job(root)):
            return _err("这个目录正在扫描中，先用 frameseek_task 看进度")
        return _ok(started=root, ai=True, lines=False,
                   note="AI 生成产物只登记媒体信息，不识别台词")
    r = HUB.add(root)                     # 没登记过就登记上，并设为激活
    if r.get("id"):
        HUB.set_active(r["id"])
    if not _bg(f"scan:{root}", _scan_job(root, use_asr, with_gif)):
        return _err("这个库正在扫描中，先用 frameseek_task 看进度")
    HUB.touch_scan(root)
    return _ok(started=root, use_asr=use_asr, with_gif=with_gif,
               note="用 frameseek_task 查进度")


@server.tool()
def frameseek_task(path: str = "") -> str:
    """查扫描/识别任务进度。传 path 看某个库的最近任务；不传看当前激活库，
    并附带全部库正在跑的任务。status: running / done / error。
    done 时 message 里带汇总（几个素材、几条台词、来源）。"""
    out: Dict[str, Any] = {"ok": True, "running": []}
    root = pick_root(path)
    if root and os.path.isdir(root):
        db = os.path.join(root, C.CACHE_DIR, C.DB_NAME)
        if os.path.exists(db):
            out["latest"] = IX.open_store(root).latest_task(root) or {}
        else:
            out["latest"] = {}
    for l in HUB.list():
        if not l.get("exists"):
            continue
        db = os.path.join(l["path"], C.CACHE_DIR, C.DB_NAME)
        if not os.path.exists(db):
            continue
        try:
            t = IX.open_store(l["path"]).latest_task(l["path"])
        except Exception:
            t = None
        if t and t.get("status") == "running":
            t = dict(t)
            t["lib"] = l["name"]
            t["root"] = l["path"]
            out["running"].append(t)
    return json.dumps(out, ensure_ascii=False)


# ---------------- 工具：查询 ----------------
@server.tool()
def frameseek_search(query: str, root: str = "", limit: int = 50,
                     all_libraries: bool = True) -> str:
    """按台词搜视频片段。默认跨全部登记的素材库。
    返回 line_id / 时间码 / 截图 / 所属库 root（导出片段、重识别都要带 root）。"""
    if all_libraries and not root:
        rows = HUB.search_all(query, limit)
    else:
        r = pick_root(root)
        if not r:
            return _err("未设置目录")
        rows = IX.open_store(r).search(query, limit)
        for x in rows:
            x["root"] = r
    slim = [{"line_id": x["id"], "video_id": x["video_id"], "video": x["name"],
             "start": round(x["start"], 2), "start_str": x.get("start_str"),
             "end": x.get("end"), "end_str": x.get("end_str"),
             "text": x["text"], "shot": x.get("shot"),
             "lib": x.get("lib", ""), "root": x.get("root", "")} for x in rows]
    return _ok(count=len(slim), results=slim)


@server.tool()
def frameseek_list_videos(root: str = "", q: str = "", limit: int = 100) -> str:
    """列出某个素材库里的视频（时长、台词条数、带方框凭证的条数、动图路径）。
    q 非空时按文件名过滤。"""
    r = pick_root(root)
    if not r:
        return _err("未设置目录")
    rows = IX.open_store(r).list_videos(q, limit)
    videos = []
    for x in rows:
        s = IX.open_store(r).box_stats(x["id"])
        videos.append({
            "id": x["id"], "name": x["name"],
            "duration": round(x.get("duration") or 0, 2),
            "lines": x.get("line_count"), "source": x.get("line_source"),
            "boxed": s["boxed"], "no_box": s["total"] - s["boxed"],
            "gif": x.get("gif"), "cover": x.get("cover")})
    return _ok(root=r, count=len(videos), videos=videos)


@server.tool()
def frameseek_video_info(video_id: int, root: str = "") -> str:
    """单个素材的详情：时长/分辨率、台词条数与来源、方框凭证统计、
    封面与动图路径（相对库根目录，配合 Web 服务 /media 或 /stream 访问）。"""
    r = pick_root(root)
    if not r:
        return _err("未指定素材库")
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}")
    s = st.box_stats(video_id)
    return _ok(root=r, video={
        "id": v["id"], "name": v["name"], "rel": v.get("rel"),
        "duration": round(v.get("duration") or 0, 2),
        "width": v.get("width"), "height": v.get("height"),
        "md5": v.get("md5"), "status": v.get("status"),
        "line_count": v.get("line_count"), "line_source": v.get("line_source"),
        "err": v.get("err") or "",
        "boxed": s["boxed"], "no_box": s["total"] - s["boxed"],
        "cover": v.get("cover"), "gif": v.get("gif")})


@server.tool()
def frameseek_get_lines(video_id: int, root: str = "", limit: int = 500) -> str:
    """列出某个视频的全部台词（时间码 + 文本 + 来源 + 截图路径）。
    带方框凭证的行附 box 摘要（ar 字宽比 / hits 支持帧数 / drift 漂移 /
    font 字体指纹 / font_dev 字体偏离度）——font_dev 超标的行是纠错和
    二次验证时要优先怀疑或忽略的对象。"""
    r = pick_root(root)
    if not r:
        return _err("未指定素材库")
    st = IX.open_store(r)
    rows = st.lines_of(video_id)[:limit]
    lines = []
    for x in rows:
        b = x.get("box")
        item = {"line_id": x["id"], "start": round(x["start"], 2),
                "start_str": store_mod.fmt_ts(x["start"]),
                "end": round(x.get("end") or x["start"], 2),
                "end_str": store_mod.fmt_ts(x.get("end") or x["start"]),
                "text": x["text"], "src": x.get("src") or "",
                "shot": x.get("shot")}
        if isinstance(b, dict) and b:
            item["box"] = {"ar": b.get("ar"), "hits": b.get("hits"),
                           "drift": b.get("drift"), "conf": b.get("conf"),
                           "font": b.get("font"), "font_dev": b.get("font_dev")}
        lines.append(item)
    return _ok(root=r, video_id=video_id, count=len(lines), lines=lines)


# ---------------- 工具：校对单句 ----------------
@server.tool()
def frameseek_recog_line(line_id: int, mode: str = "ocr", root: str = "",
                         pad: float = 0.6) -> str:
    """重新识别**这一句**，看它到底写对了没有。只报告结果，不改库。

    mode=ocr  回到画面上抽首/中/尾三帧重跑 OCR：返回每帧识别到的文字，
              以及和库里这一句的偏移量（库里有框时）。画面有硬字幕时用这个。
    mode=asr  只抽这一句时间区间前后各 pad 秒的音频跑 faster-whisper：
              返回听到的文字。画面没字幕、或怀疑是听错的时候用这个。

    拿到结果后要不要改库，由 frameseek_edit_line 决定——这个工具永远
    不写库，免得把人工校对过的台词覆盖掉。asr 首次调用要加载模型，约 10 秒。
    """
    from core import asr as A
    from core import boxaudit as BA
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库")
    g = _ai_guard(r)
    if g:
        return _err(g)
    st = IX.open_store(r)
    row = st.get_line(line_id)
    if not row:
        return _err(f"在 {r} 里找不到台词 {line_id}")
    v = st.get_video(row["video_id"])
    if not v:
        return _err(f"台词 {line_id} 所属的素材已不在库里")
    video = os.path.join(r, v["rel"])
    if not os.path.exists(video):
        return _err("视频文件不在了")

    if mode == "asr":
        s = max(0.0, float(row.get("start") or 0) - float(pad))
        span = max(0.3, float(row.get("end") or 0) + float(pad) - s)
        try:
            audio = asr_mod._decode_16k(video, s, span)
        except Exception as e:
            return _err(f"抽音频失败：{e}")
        if audio is None or audio.size == 0:
            return _err("这一段没有音频")
        model, used, err = asr_mod.get_model(None)
        if model is None:
            return _err("语音模型加载失败：" + (err or ""))
        try:
            segs, _ = model.transcribe(
                audio, language=C.ASR_LANG, vad_filter=C.ASR_VAD,
                vad_parameters=dict(min_silence_duration_ms=300))
            text = "".join((g.text or "") for g in segs).strip()
        except Exception as e:
            return _err(f"识别失败：{type(e).__name__} {e}")
        return _ok(root=r, line_id=line_id, mode="asr", text=text,
                   start=round(s, 3), dur=round(span, 3), device=used,
                   saved_text=row.get("text") or "")

    if mode != "ocr":
        return _err('mode 只能是 "ocr" 或 "asr"')
    out = BA.probe(video, row)
    out.pop("saved", None)
    return json.dumps({"ok": True, "root": r, "line_id": line_id, "mode": "ocr",
                      "saved_text": row.get("text") or "", **out},
                     ensure_ascii=False)


@server.tool()
def frameseek_edit_line(line_id: int, text: str = None, start: float = None,
                        end: float = None, save_box: bool = False,
                        root: str = "") -> str:
    """人工校对一条台词：改文字、改起止时间，可选同时写入画面方框。

    传 None 的字段不动。save_box=True 时会回画面抽 3 帧重跑 OCR，
    三帧文字都对得上才把方框凭证写进库，对不上就整体拒绝、什么都不改——
    返回里能看到 probe 的逐帧结果和失败原因。
    改完返回该句最新的 verify 结论（ok / reasons）和 boxaudit 摘要。
    """
    from core import boxaudit as BA
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库")
    g = _ai_guard(r)
    if g:
        return _err(g)
    st = IX.open_store(r)
    row = st.get_line(line_id)
    if not row:
        return _err(f"在 {r} 里找不到台词 {line_id}")
    v = st.get_video(row["video_id"])
    if not v:
        return _err(f"台词 {line_id} 所属的素材已不在库里")
    if start is not None and end is not None and float(end) <= float(start):
        return _err("结束时间要晚于开始时间")

    probe = None
    if save_box:
        video = os.path.join(r, v["rel"])
        if not os.path.exists(video):
            return _err("视频文件不在了，方框没法写")
        probe = BA.probe(video, row)
        if not probe.get("ok"):
            return _err("这次重新识别没通过，方框没写入：" +
                        (probe.get("detail") or ""))
        box = BA.fold_probe(probe.get("frames") or [])
        if not box:
            return _err("没拿到可用的方框")
        st.set_box(line_id, box)

    if text is not None or start is not None or end is not None:
        rr = st.update_line(line_id, text=text, start=start, end=end)
        if not rr.get("ok"):
            return _err(rr.get("err") or "写入失败")

    v2 = st.get_line(line_id)
    chk = V.check_line({**(v2 or {}), "src": (v2 or {}).get("src")
                        or v.get("line_source") or ""})
    audit = BA.audit_line(st, row["video_id"], v2)
    return json.dumps({"ok": True, "root": r, "line_id": line_id,
                       "text": v2.get("text"), "start": v2.get("start"),
                       "end": v2.get("end"),
                       "has_box": audit.get("has_box"),
                       "verify": chk.to_dict(),
                       "probe": probe}, ensure_ascii=False)


# ---------------- 工具：识别 ----------------
@server.tool()
def frameseek_retranscribe(video_id: int, root: str = "",
                           force: bool = False) -> str:
    """对这个素材重新识别台词：先清空旧台词（含旧截图、全文索引），
    再按优先级链重跑（同名 .srt → 内嵌字幕 → OCR 画面硬字幕）。
    force=True 时连外挂/内嵌字幕也跳过，强制走 OCR。
    异步：立即返回，用 frameseek_task 查进度。"""
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库")
    g = _ai_guard(r)
    if g:
        return _err(g)
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}")
    key = f"retranscribe:{r}:{video_id}"
    if not _bg(key, _retranscribe_job(r, video_id, force)):
        return _err("这个素材正在重识别中，先用 frameseek_task 看进度")
    return _ok(started=True, name=v["name"], root=r, force=force,
               lines_before=v.get("line_count") or 0,
               note="用 frameseek_task 查进度")


@server.tool()
def frameseek_transcribe_asr(video_id: int, root: str = "", model: str = "",
                             lang: str = "") -> str:
    """语音识别（faster-whisper）：不读外挂/内嵌字幕、不跑 OCR，直接听音轨。
    给画面没有硬字幕的电影用。同样先清空旧台词再写，并落同名 .srt + 逐条截图。
    model: tiny/base/small/medium/large-v3，留空用默认（small）。
    异步：立即返回，用 frameseek_task 查进度。
    响应里带 device（gpu/cpu）和时长，可粗估等待时间。"""
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库")
    g = _ai_guard(r)
    if g:
        return _err(g)
    m = (model or "").strip()
    if m and m not in ASR_MODELS_OK:
        return _err(f"不支持的模型：{m}（可选 {', '.join(ASR_MODELS_OK)}）")
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}")
    key = f"asr:{r}:{video_id}"
    if not _bg(key, _asr_job(r, video_id, m or None, (lang or "").strip() or None)):
        return _err("这个素材正在语音识别中，先用 frameseek_task 看进度")
    dev = _asr_device()
    dur = round(v.get("duration") or 0, 1)
    return _ok(started=True, name=v["name"], root=r,
               model=m or C.ASR_MODEL, duration=dur,
               device=dev.get("device") or "", compute=dev.get("compute") or "",
               cuda=dev.get("cuda"),
               eta_hint=("GPU small 约 8~9 倍实时；CPU 约 1 倍实时量级，"
                         "长片会很久，建议换小模型或只对无字幕片段用"),
               lines_before=v.get("line_count") or 0,
               note="用 frameseek_task 查进度")


# ---------------- 工具：视频生成（MiniMax H3） ----------------
@server.tool()
def frameseek_gen_info() -> str:
    """视频生成现状：h3gen 引擎是否就绪（embeded python / 脚本 / ComfyUI 库目录）、
    支持的宽高比、默认像素档、提示词长度上限、成片落入哪个子目录。"""
    return json.dumps(_gen_info(), ensure_ascii=False)


@server.tool()
def frameseek_generate_video(prompt: str, duration: float = 5.0,
                             aspect: str = "16:9 (Widescreen)",
                             megapixels: float = 0.3, seed: int = 0,
                             name: str = "", ref_image: str = "",
                             index: str = "none", root: str = "") -> str:
    """用本地 MiniMax H3（直调 ComfyUI 库，无服务、不打开 ComfyUI 界面）按提示词
    生成视频，成片落 <项目根>/生成/，**不进素材库**（素材库只放原始素材）。

    prompt：英文描述效果最好；<2000 字符；相机运动可用 [slow zoom in] 标注。
    duration：秒，5~15（内部按 24fps 折算成官方帧数约束 124~362 帧）。
    aspect：见 frameseek_gen_info；megapixels：像素档，0.3 是 4060Ti 8G 的默认。
    seed：0 = 随机；固定同一个 seed 可复现同一构图。
    ref_image：参考图路径（可选），prompt 里用 <Picture 1> 引用它锁形象/风格。
    index：保留兼容参数，现已停用（产物在项目目录，不做 ASR/入库）。
    root：仅用于把任务进度记到某个库的 tasks 表，省略时用激活库。

    异步：立即返回 task_id，用 frameseek_task 查进度。GPU 同时只能跑一个。
    每次生成都是独立子进程（模型即用即加载、跑完即退），冷启动要等模型加载。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("目标素材库目录不存在，先用 frameseek_add_library 登记")
    # 启动前先挡掉明显不合法的参数（线程里才报就太晚了）
    try:
        gen_mod.frames_for(duration)
        if aspect not in gen_mod.ASPECTS:
            raise ValueError(f"不支持的宽高比：{aspect}")
        p = (prompt or "").strip()
        if not p:
            raise ValueError("prompt 不能为空")
        if len(p) > gen_mod.MAX_PROMPT:
            raise ValueError(
                f"prompt 超长（{len(p)} > {gen_mod.MAX_PROMPT} 字符上限）")
        if ref_image and not os.path.isfile(ref_image):
            raise ValueError("参考图不存在：" + ref_image)
        if index not in ("asr", "scan", "none"):
            raise ValueError("index 只支持 asr / scan / none")
    except ValueError as e:
        return _err(str(e))
    params = {"prompt": p, "duration": float(duration), "aspect": aspect,
              "megapixels": float(megapixels), "seed": int(seed or 0),
              "name": name, "ref_image": ref_image, "index": index}
    st = IX.open_store(r)
    tid = st.new_task("gen", r, 100)
    if not _bg("gen", _gen_job(r, tid, params)):
        # GPU 同时只能跑一个；把刚建的空任务收掉，别留一张假进度卡
        st.finish_task(tid, "error", "已有生成任务在进行中")
        return _err("已经有一个生成任务在跑了（GPU 同时只能跑一个），"
                    "先用 frameseek_task 看进度")
    return _ok(started=True, task_id=tid, root=r, params=params,
               eta_hint="子进程冷启动（模型即用即加载）+ 12 步采样，"
                        "5 秒片段通常几分钟起步，长片更久",
               note="用 frameseek_task 查进度")


# ---------------- 工具：白板手绘视频 ----------------
@server.tool()
def frameseek_handdraw_info() -> str:
    """白板手绘视频引擎现状：本地 CPU 逐笔渲染引擎是否就绪、缺哪些包、
    默认音色、纸张底色、成片落哪个子目录。生成手绘视频前先看这个。
    不占 GPU，可与 MiniMax H3 生成并行。"""
    return json.dumps(_hd_info(), ensure_ascii=False)


@server.tool()
def frameseek_handdraw_init(script: str = "", srt: str = "",
                            name: str = "", title: str = "",
                            root: str = "") -> str:
    """白板手绘第 1 步：用文稿（或已有 SRT）建一个手绘工程，返回工程目录名。

    script：.txt 文字稿路径。引擎按句拆成字幕并估时间轴（时间轴是**预估**，
    不是配音对齐结果，后面试听配音时才会定）。
    srt：已有 .srt 字幕路径，则保留原时间轴。script / srt 给一个就行。
    name：工程目录名，省略时用标题或文件名。
    title：片名（写进 project.json）。
    工程落在 <项目根>/手绘工程/<name>/，之后所有 handdraw 工具都用 name 指认。
    （2026-10-15 起工程与成片都在 FrameSeek 项目目录下，不进素材库。）

    返回后接 frameseek_handdraw_scenes 出分幕建议，再逐幕生成线稿+标注。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("需要一个可用素材库来记录任务进度（工程本身在项目目录下）")
    try:
        if not hd_mod.engine_ready():
            raise ValueError("手绘引擎未就绪，缺：" +
                             ", ".join(hd_mod.engine_info()["missing"]))
        res = hd_mod.init_project(root=r, script=script, srt=srt,
                                  title=title, name=name)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["root"] = r
    res["next"] = "出分幕建议：frameseek_handdraw_scenes"
    return _ok(**res)


@server.tool()
def frameseek_handdraw_scenes(project: str, target_sec: int = 30,
                              root: str = "") -> str:
    """白板手绘第 2 步：读工程字幕，给分幕建议（纯本地，不生图）。

    project：init 返回的工程名。
    target_sec：每幕目标秒数（默认 30，建议 25~35）。
    返回字幕条（index/startMs/endMs/text）和分幕建议
    （sceneIndex/cueRange 覆盖的字幕号/suggested 时长），用来定分幕策略。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    try:
        res = hd_mod.suggest_scenes(root=r, project=project,
                                    target_sec=int(target_sec))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["project"] = project
    res["root"] = r
    return _ok(**res)


@server.tool()
def frameseek_handdraw_annotate(project: str, scene_id: str,
                                image: str, annotation: str,
                                root: str = "") -> str:
    """白板手绘第 3 步：给某一幕写「区域标注」，渲染的前置条件。

    image：这一幕的源线稿 png 绝对路径（暖米黄纸底、无文字、留白充足）。
    annotation：标注 JSON **字符串**，结构见本工具返回的模板或 skill 文档。
      核心字段：canvas{width,height}=源图像素、elements[] 每项含
      sequence / narrativeRole / region{x,y,width,height} /
      reveal{startMs,durationMs,direction,protectedRegions} / handPath。
      sequence 与 startMs 必须按字幕叙事顺序，不是从左到右。
    引擎要求 image 与 annotation 成对同名，本工具自动拷进工程 scenes/ 并
    登记进 project.json。同名 scene_id 再调会覆盖（方便重标）。

    标注没写对渲染会崩或时序乱——先用 frameseek_handdraw_region_preview
    出编号检查图核对一遍，再渲染。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    ann = None
    try:
        ann = json.loads(annotation) if isinstance(annotation, str) else annotation
    except Exception as e:
        return _err(f"annotation 不是合法 JSON：{e}")
    try:
        res = hd_mod.write_annotation(root=r, project=project,
                                      scene_id=scene_id, image=image,
                                      annotation=ann)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["project"] = project
    res["next"] = ("核对分区：frameseek_handdraw_region_preview → "
                   "渲染：frameseek_handdraw_render")
    return _ok(**res)


@server.tool()
def frameseek_handdraw_region_preview(project: str, scene_id: str,
                                      root: str = "") -> str:
    """白板手绘第 4 步：出某幕的编号/方向检查图，核对分区与叙事顺序一致、
    区域都在画布内。渲染前建议先跑一次（纯本地，很快）。"""
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    try:
        res = hd_mod.region_preview(root=r, project=project, scene_id=scene_id)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["project"] = project
    return _ok(**res)


@server.tool()
def frameseek_handdraw_render(project: str, scene_id: str,
                              preview_ms: int = 0, root: str = "") -> str:
    """白板手绘第 5 步：渲染单幕手绘 mp4（本地 CPU 逐笔流式渲染，不占 GPU）。

    preview_ms > 0 只渲前几毫秒（快速试笔迹效果），0 = 渲完整一幕。
    异步：立即返回 task_id，用 frameseek_task 查进度，成片在工程 output/ 下。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    try:
        hd_mod.project_json(r, project)   # 校验工程存在
    except Exception as e:
        return _err(str(e))
    st = IX.open_store(r)
    tid = st.new_task("handdraw", r, 100)
    key = f"hd-render-{project}-{scene_id}"
    params = {"project": project, "scene_id": scene_id,
              "preview_ms": int(preview_ms or 0), "root": r}

    def fn(tick):
        tick(5, "渲染中")
        res = hd_mod.render_scene(**params)
        res["note"] = f"完成：{os.path.basename(res.get('output') or '')}"
        return res

    if not _bg(key, _hd_job(r, tid, "render", fn)):
        st.finish_task(tid, "error", "该幕已有渲染任务在进行中")
        return _err("这一幕已经在渲染了，先用 frameseek_task 看进度")
    return _ok(started=True, task_id=tid, root=r, **params,
               eta_hint="本地 CPU 逐笔渲染，单幕几十秒~几分钟",
               note="用 frameseek_task 查进度")


@server.tool()
def frameseek_handdraw_voice(text: str, voices: List[str] = None,
                             allow_network: bool = False,
                             project: str = "", root: str = "") -> str:
    """白板手绘第 6 步：配音音色试听（Edge TTS，免账号、在线合成）。

    text：试听文案（10~20 秒、含数字和问句最有代表性）。
    voices：音色列表，省略用默认 zh-CN-XiaoxiaoNeural（晓晓）。
    allow_network：必须为 true——会把 text 发到 Edge TTS 服务合成语音。
    多个音色会用同一段文案分别输出试听文件。

    这是**在线服务**，和其余全本地工具不同；正式混流也在第 7 步里。
    """
    if not allow_network:
        return _err("配音是联网合成，需 allow_network=true 显式许可")
    r = pick_root(root) if (root or project) else None
    try:
        res = hd_mod.audition_voice(text=text, voices=voices,
                                    allow_network=True, root=r or "",
                                    project=project)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    return _ok(**res)


@server.tool()
def frameseek_handdraw_mix(project: str, index: str = "none",
                           root: str = "") -> str:
    """白板手绘第 7 步：配音 + 伴奏混流，合成最终 mp4。

    需要工程时间轴已定（先试听配音再 mix）。成片落工程 output/，
    同时另存一份到 <项目根>/生成/<工程名>.mp4。不进素材库索引
    （2026-10-15 起 index 参数停用）。

    异步：立即返回 task_id，用 frameseek_task 查进度。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    try:
        hd_mod.project_json(r, project)
    except Exception as e:
        return _err(str(e))
    st = IX.open_store(r)
    tid = st.new_task("handdraw", r, 100)
    key = f"hd-mix-{project}"
    params = {"project": project, "index": index, "root": r}

    def fn(tick):
        tick(5, "合成中（配音+伴奏）")
        res = hd_mod.mix(**params)
        res["note"] = f"完成：{os.path.basename(res.get('file') or '')}"
        return res

    if not _bg(key, _hd_job(r, tid, "mix", fn)):
        st.finish_task(tid, "error", "该工程已有混流任务在进行中")
        return _err("这个工程已经在混流了，先用 frameseek_task 看进度")
    return _ok(started=True, task_id=tid, root=r, **params,
               eta_hint="Edge TTS 逐条配音 + 本地伴奏合成 + 编码",
               note="用 frameseek_task 查进度")


@server.tool()
def frameseek_handdraw_qa(project: str, root: str = "") -> str:
    """白板手绘第 8 步：质检——帧数 / 时长 / 音画长度一致性（纯读工程文件）。"""
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    try:
        res = hd_mod.qa(root=r, project=project)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["project"] = project
    return _ok(**res)


# ---------------- 工具：校验 / 复查 ----------------
@server.tool()
def frameseek_verify(video_id: int, root: str = "", check: bool = False,
                     limit: int = 50) -> str:
    """方框校验：这条台词在画面上真有框支撑吗。纯读库（毫秒级，不重跑 OCR）。
    返回通过率 + 不达标原因分布；check=True 时附逐条结论（可疑行带 reasons）。
    来源是 srt/embedded/asr 的台词判“不适用”(skipped)，不计失败。"""
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    g = _ai_guard(r)
    if g:
        return _err(g)
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}")
    verdicts = V.check_video(st, video_id)
    passed = sum(1 for x in verdicts if x.ok and not x.skipped)
    skipped = sum(1 for x in verdicts if x.skipped)
    reasons: Dict[str, int] = {}
    for x in verdicts:
        for rr in x.reasons:
            k = rr.split(" ")[0]
            reasons[k] = reasons.get(k, 0) + 1
    checked = len(verdicts) - skipped
    out: Dict[str, Any] = {
        "ok": True, "root": r, "name": v["name"],
        "total": len(verdicts), "checked": checked, "passed": passed,
        "skipped": skipped, "failed": checked - passed,
        "rate": round(passed / checked, 4) if checked else 0.0,
        "reasons": reasons}
    if check:
        items = [{"line_id": i, "text": x.text, "start": round(x.start, 2),
                  "ok": x.ok, "skipped": x.skipped, "reasons": x.reasons}
                 for i, x in enumerate(verdicts)]
        out["lines"] = items[:limit]
        out["lines_truncated"] = len(items) > limit
    return json.dumps(out, ensure_ascii=False)


@server.tool()
def frameseek_boxaudit(video_id: int, root: str = "", limit: int = 50) -> str:
    """方框复查清单：每条台词的凭证指标（字宽比 ar / 支持帧 hits / 漂移 drift /
    字体指纹 font / 字体偏离 font_dev）+ 通过与否 + 不达标原因。
    用来定位“哪几条可疑、为什么”。纯读库，不重跑 OCR（现场重检用 Web 页面）。
    limit 控制返回多少条明细（按时间序，前面的更可能是问题集中的位置另说）；
    全量汇总不受 limit 影响。"""
    from core import boxaudit as BA
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    g = _ai_guard(r)
    if g:
        return _err(g)
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}")
    rep = BA.audit_video(st, video_id)
    items = rep.pop("items", [])
    rep["root"] = r
    rep["items"] = items[:limit]
    rep["items_truncated"] = len(items) > limit
    return json.dumps(rep, ensure_ascii=False)


# ---------------- 工具：导出 ----------------
@server.tool()
def frameseek_export_clip(line_id: int, pre: float = 5.0, post: float = 5.0,
                          fast: bool = True, root: str = "") -> str:
    """导出某句台词前后各 N 秒的片段（默认前5秒后5秒），返回 mp4 绝对路径。
    line_id 从 frameseek_search / frameseek_get_lines 拿，root 用结果里的
    root（多库时不能省，line_id 只在它自己的库内唯一）。
    fast=True 用 -c copy 秒出；False 重编码，帧精确但慢。"""
    r = pick_root(root)
    if not r:
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）")
    g = _ai_guard(r)
    if g:
        return _err(g)
    st = IX.open_store(r)
    ln = st.get_line(line_id)
    if not ln:
        return _err(f"在 {r} 里找不到台词 {line_id}")
    v = st.get_video(ln["video_id"])
    if not v:
        return _err("这条台词的素材不在了")
    src = os.path.join(r, v["rel"])
    if not os.path.exists(src):
        return _err("视频文件不在了：" + v["rel"])
    s = max(0.0, ln["start"] - pre)
    e = (ln.get("end") or ln["start"]) + post
    rel = f"{C.CLIP_DIR}/line{line_id}_{ln['start']:.2f}.mp4"
    dst = os.path.join(IX.cache_dir(r), rel)
    if not media.export_clip(src, s, e, dst, reencode=not fast):
        return _err("导出失败（ffmpeg？）")
    return _ok(path=dst, video=v["name"], text=ln["text"],
               start=s, end=e, root=r)


if __name__ == "__main__":
    HUB.migrate_json()
    asyncio.run(server.run_stdio_async())
