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
import time
from typing import Annotated, Any, Dict, List, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "vendor"))   # 本机 venv 里 anyio 半残，用 vendor 兜底

from pydantic import Field  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core import config as C  # noqa: E402
from core import indexer as IX  # noqa: E402
from core import media  # noqa: E402
from core import store as store_mod  # noqa: E402
from core import ocr as ocr_mod  # noqa: E402
from core import verify as V  # noqa: E402
from core import asr as asr_mod  # noqa: E402
from core.hub import HUB  # noqa: E402

ASR_MODELS_OK = ("tiny", "base", "small", "medium", "large-v3")

server = MCPServer(C.APP_NAME, title=C.APP_LABEL, instructions=(
    "拾帧 FrameSeek：本地视频素材库。素材文件夹登记在 hub.db，每个目录自己的索引在 "
    "<目录>/.frameseek/index.db。\n"
    "\n"
    "【开始前】先调 frameseek_doctor 或 frameseek_state 看库现状（有没有库、索引了没、"
    "台词来源是字幕还是 OCR、有没有任务在跑）。空库要先 frameseek_add_library + "
    "frameseek_scan（异步）。\n"
    "\n"
    "【最常用的一条链】frameseek_search 按台词跨库找 → 结果里带 root，"
    "后续每个工具都要把这个 root 原样带回（line_id 只在它自己的库内唯一，跨库会撞号）→ "
    "frameseek_export_clip 导出片段。\n"
    "\n"
    "【异步任务】frameseek_scan / frameseek_retranscribe / frameseek_transcribe_asr "
    "都立即返回、不等结果，之后用 frameseek_task 轮询（间隔至少 5~10 秒，别连打）。\n"
    "\n"
    "【找视频但不知道台词】用 frameseek_find_videos 按文件名/关键字找片，"
    "用 frameseek_video_summary 一次拿到某片的时长、台词数、来源和台词全文（省得逐条调）。\n"
    "\n"
    "【校对台词】frameseek_recog_line 重新识别这一句看写对没有（mode=ocr 看画面硬字幕 / "
    "mode=asr 只听这一句），它只报告不改库；采纳后用 frameseek_edit_line 落库"
    "（save_box=True 可同时写入画面方框）。frameseek_verify / frameseek_boxaudit 回答"
    "“这句在画面上真有框吗”，纯读库很快，纠错时优先怀疑 font_dev 超标的行。\n"
    "\n"
    "【读资源而不是逐个调工具】资源列表见 resources/list，常用的有："
    "frameseek://libraries（全部库+统计）、frameseek://tasks/running（在跑的任务）、"
    "frameseek://clips（已导出片段清单）、frameseek://tool-catalog（全工具速查表）。\n"
))


# ---------------- 小工具 ----------------
def _err(msg: str, hint: str = "", **extra) -> str:
    """统一错误返回。

    `hint` 是给 LLM 的**下一步动作建议**——以前只回一句「找不到台词 123」，
    模型只能瞎猜或重试同一个调用。现在把该调哪个工具、该带什么参数直接说清。
    """
    out: Dict[str, Any] = {"ok": False, "err": msg}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return json.dumps(out, ensure_ascii=False)


def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


# 公共参数类型：MCP 客户端把 Annotated 里的 Field(description=...) 写进
# tools/list 的 inputSchema.properties[].description，这是模型选参型的唯一依据。
# 以前全靠函数 docstring 顶着，参数级说明是空的 —— 「pre/post/fast/line_ids」
# 这类参数模型只能靠猜。改这些别名就等于改了全部工具的参数说明。
Root = Annotated[
    str,
    Field(default="", description=
          "素材库目录绝对路径。省略=环境变量 FRAMESEEK_ROOT，再省略=hub 里的激活库。"
          "**多库场景下必须带**：从 search / find_videos 的结果里原样复制，"
          "line_id 只在它自己的库内唯一，跨库会撞号")]
RootOpt = Annotated[
    Optional[str],
    Field(default=None, description=
          "同上，但可以是 null（显式表达「不指定，走默认」）")]


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


_FFMPEG_OK: Optional[bool] = None


def _ffmpeg_ok() -> bool:
    """ffmpeg 在不在（探测一次缓存住）。抽帧/导出全靠它，缺了整条链断掉。"""
    global _FFMPEG_OK
    if _FFMPEG_OK is None:
        try:
            r = media.run([C.FFMPEG, "-version"], timeout=15)
            _FFMPEG_OK = (r.returncode == 0)
        except Exception:
            _FFMPEG_OK = False
    return _FFMPEG_OK


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
               ocr=ocr_mod.engine_info(), asr=_asr_device())


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
    """列出所有登记的素材库（路径、素材数、台词数、是否索引过、目录是否丢失）

    返回的 `path` 就是其它所有工具的 `root` 参数值。
    """
    return _ok(hub_db=HUB.db_path, active=HUB.active_path(),
               libraries=[_lib_json(l) for l in HUB.list()])


@server.tool()
def frameseek_doctor() -> str:
    """一键体检：调用任何其它工具前，或工具报错看不懂时，先跑这个。

    一次给全：Python/ffmpeg/OCR/ASR 的可用性，
    每个库的状态（目录在不在、索引了没、素材数/台词数/台词来源），
    还有正在跑的任务和它们的进度。
    `problems` 里非空就是真有问题，照着它给的建议处理即可。
    """
    problems: List[str] = []
    libs = HUB.list()
    for l in libs:
        if not l.get("exists"):
            problems.append(f"库「{l['name']}」目录已丢失：{l['path']}（磁盘上找不到）")
        elif not l.get("indexed"):
            problems.append(f"库「{l['name']}」还没索引过：调 frameseek_scan(path='{l['path']}')")
        elif not l.get("lines"):
            problems.append(f"库「{l['name']}」索引了但一条台词都没有："
                            f"frameseek_retranscribe 逐片重识别，或 frameseek_scan(use_asr=True)")

    ocr = ocr_mod.engine_info()
    asr = _asr_device()
    if not ocr.get("ok"):
        problems.append("OCR 引擎不可用（纯 OCR 的素材不会有台词）：" +
                        str(ocr.get("err") or ocr.get("missing") or ""))
    if not asr.get("ok"):
        problems.append("语音识别不可用（无字幕素材只能靠它）：" +
                        str(asr.get("err") or ""))

    running: List[Dict[str, Any]] = []
    for l in libs:
        if not l.get("exists"):
            continue
        if not os.path.exists(os.path.join(l["path"], C.CACHE_DIR, C.DB_NAME)):
            continue
        try:
            t = IX.open_store(l["path"]).latest_task(l["path"])
        except Exception:
            t = None
        if t and t.get("status") == "running":
            t = dict(t)
            t["lib"] = l["name"]
            t["root"] = l["path"]
            running.append(t)

    return _ok(
        ok=not problems,           # 顶层 ok 只表示「有没有问题」，与其它工具语义不同
        problems=problems,
        libraries=[_lib_json(l) for l in libs],
        active=HUB.active_path(),
        total={"libraries": len(libs),
               "videos": sum(l["videos"] for l in libs),
               "lines": sum(l["lines"] for l in libs),
               "duration": round(sum(l["duration"] for l in libs), 1)},
        engines={"ocr": ocr, "asr": asr, "ffmpeg": bool(_ffmpeg_ok())},
        running=running,
        note=("一切正常。典型用法：frameseek_search 找片 → frameseek_export_clip 导出"
              if not problems else "按 problems 逐条处理后再干活"))


@server.tool()
def frameseek_find_videos(
        q: Annotated[str, Field(
            description="文件名关键词（子串匹配）。"
                        "跨库搜，认片名而不是台词——用户说「找一下潜伏第三集」时用它")],
        limit: Annotated[int, Field(default=30, ge=1, le=200,
                                    description="最多返回几条（默认 30）")] = 30,
        all_libraries: Annotated[bool, Field(
            default=True, description="True（默认）=跨全部库；False=只看 root")] = True,
        root: Root = "",
        has_lines: Annotated[Optional[bool], Field(
            default=None, description=
            "True=只给有台词的片；False=只给没台词的；省略=全给")] = None
        ) -> str:
    """按**文件名**跨库找片（认片名，不认台词）。

    和 frameseek_search 的区别：那个搜台词内容（"我愿意"出现在哪部片），
    这个搜片名（"潜伏" 有哪几集）。用户想找某部片子但不知道里面说了什么时用它。
    结果带 `root` 和 `id`，接着可调 frameseek_video_summary / frameseek_get_lines。
    """
    want_lines = None if has_lines is None else bool(has_lines)
    targets: List[Dict[str, Any]]
    if all_libraries and not root:
        targets = [{"name": l["name"], "path": l["path"], "id": None}
                   for l in HUB.list() if l.get("exists")]
    else:
        r = pick_root(root)
        if not r or not os.path.isdir(r):
            return _err("未指定素材库或目录不存在",
                        "用 frameseek_list_libraries 取 path，或"
                        "直接让 all_libraries=True（默认）跨全部库搜")
        targets = [{"name": _lib_name_of(r), "path": r, "id": None}]

    out: List[Dict[str, Any]] = []
    for t in targets:
        try:
            st = IX.open_store(t["path"])
            for x in st.list_videos(q, limit * 2):
                nl = int(x.get("line_count") or 0)
                if want_lines is True and nl <= 0:
                    continue
                if want_lines is False and nl > 0:
                    continue
                out.append({
                    "id": x["id"], "name": x["name"], "root": t["path"],
                    "lib": t["name"],
                    "duration": round(x.get("duration") or 0, 2),
                    "lines": nl, "source": x.get("line_source"),
                    "cover": x.get("cover"), "gif": x.get("gif")})
        except Exception as e:
            print(f"[frameseek-mcp] find_videos {t['path']} 失败: {e}",
                  file=sys.stderr)
    out.sort(key=lambda x: (x["lib"], x["name"]))
    total = len(out)
    out = out[:limit]
    res: Dict[str, Any] = {"count": len(out), "videos": out}
    if not res["videos"]:
        res["hint"] = (f"没有库里有文件名含「{q}」的片。库里一共 "
                       f"{sum(l['videos'] for l in HUB.list())} 部片——"
                       "关键词缩短到 2~3 字再试，或先 frameseek_list_videos "
                       "看看实际叫什么名字")
    elif total > len(out):
        res["truncated"] = True
        res["hint"] = f"共命中 {total} 部，只返回前 {len(out)} 部，调大 limit 重取"
    return _ok(**res)


@server.tool()
def frameseek_video_summary(
        video_id: Annotated[int, Field(description="素材 id，必须配正确的 root")],
        root: Root = "",
        max_chars: Annotated[int, Field(
            default=4000, ge=200, le=40000, description=
            "台词全文最多给多少字（默认 4000）。整集 500~800 条，"
            "超出会自动截断并标注，省 token")] = 4000,
        include_lines: Annotated[bool, Field(
            default=True, description="False=只要元信息和剧情摘要，不要逐条台词")] = True
        ) -> str:
    """单个素材的「一站式概览」：元信息 + 台词全文 + 首尾几行 + 完整性评估。

    比 frameseek_video_info + frameseek_get_lines 两步省一半往返——
    想知道「这片讲了什么、台词准不准」时直接用这个。
    台词过长时会截断（会标 truncated），要全文就调大 max_chars。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库或目录不存在",
                    "video_id 只在本库内唯一，用 frameseek_find_videos "
                    "拿 root + id 一起用")
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}",
                    "id 属于别的库；用 frameseek_find_videos 拿 root + id 成对使用")
    rows = st.lines_of(video_id)
    s = st.box_stats(video_id)
    dur = round(v.get("duration") or 0, 2)
    out: Dict[str, Any] = {
        "root": r,
        "video": {"id": v["id"], "name": v["name"], "duration": dur,
                  "width": v.get("width"), "height": v.get("height"),
                  "line_count": len(rows), "line_source": v.get("line_source"),
                  "cover": v.get("cover"), "gif": v.get("gif")},
        "quality": {"boxed": s["boxed"], "no_box": s["total"] - s["boxed"],
                    "note": "台词来源 srt/embedded 的片子没有画面方框，属正常；"
                            "纯 OCR 的片子 boxed=0 说明这一句没有可靠的画面凭证，"
                            "用 frameseek_verify / boxaudit 查细节"},
        "lib": _lib_name_of(r)}
    if not rows:
        out["hint"] = ("这部片子没有台词： frameseek_retranscribe 重识别，"
                       "或 frameseek_transcribe_asr 用语音识别（适合无字幕片）")
        return _ok(**out)
    if not include_lines:
        return _ok(**out)
    items, used, trunc = [], 0, False
    for x in rows:
        t = x["text"] or ""
        if used + len(t) > max_chars:
            trunc = True
            break
        used += len(t)
        items.append({"line_id": x["id"], "start": round(x["start"], 2),
                      "start_str": store_mod.fmt_ts(x["start"]), "text": t})
    out["lines"] = items
    out["total_lines"] = len(rows)
    out["chars"] = used
    if trunc:
        out["truncated"] = True
        out["hint"] = (f"台词共 {len(rows)} 条/{used + 1} 字以上，这次只返回前 "
                       f"{len(items)} 条（{used} 字）。调大 max_chars 或改用 "
                       "frameseek_get_lines 分段取")
    return _ok(**out)


@server.tool()
def frameseek_add_library(
        path: Annotated[str, Field(description="素材文件夹的绝对路径，目录必须已存在")],
        name: Annotated[str, Field(default="",
                                   description="给这个库起个显示名，省略则用目录名")] = "",
        note: Annotated[str, Field(default="", description="备注，可留空")] = "",
        make_active: Annotated[bool, Field(
            default=True, description="是否设为当前激活库（后续工具省略 root 时用它）")
        ] = True) -> str:
    """登记一个新的素材文件夹（写进 hub.db，并在该目录下建 .frameseek）。
    同一路径重复登记不会重复。登记后不会自动扫描，接着调 frameseek_scan。"""
    r = HUB.add(path, name, note)
    if not r.get("ok"):
        return json.dumps(r, ensure_ascii=False)
    if make_active:
        HUB.set_active(r["id"])
    return _ok(id=r["id"], path=r["path"], name=r["name"],
               cache=IX.cache_dir(r["path"]))


@server.tool()
def frameseek_remove_library(
        library_id: Annotated[int, Field(
            description="库 id（从 frameseek_list_libraries / frameseek_state 拿，不是目录名）")]
) -> str:
    """从登记表移除素材库（只删登记，不动磁盘素材和 .frameseek 索引）"""
    HUB.remove(library_id)
    return _ok(id=library_id)


# ---------------- 工具：扫描 / 任务进度 ----------------
@server.tool()
def frameseek_scan(
        path: Annotated[str, Field(default="",
                                   description="要扫的目录绝对路径；省略=当前激活库。"
                                               "目录没登记过会自动登记并设为激活")] = "",
        use_asr: Annotated[Optional[bool], Field(default=None, description=
            "字段名是历史遗留，真实语义是「允不允许对没字幕的素材跑 OCR / 语音识别」。"
            "False=快很多但没字幕的片子将没有任何台词（适合只想建目录不想花钱的场合）；"
            "True/省略=按 hub 设置（默认 True）")] = None,
        with_gif: Annotated[Optional[bool], Field(
            default=None, description="是否生成动图预览；省略=按 hub 设置")] = None,
        all_libraries: Annotated[bool, Field(
            default=False, description="True=扫全部已登记库（忽略 path）")] = False) -> str:
    """扫描素材目录入库（md5 去重；生成封面/动图/台词/台词截图）。

    大目录（几十部电影）可能跑几十分钟，所以这里**立即返回**，
    之后用 frameseek_task 轮询进度（间隔 5~10 秒），别反复调本工具。
    all_libraries=True 时扫全部已登记库；否则扫 path（或当前激活库）。
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
    r = HUB.add(root)                     # 没登记过就登记上，并设为激活
    if r.get("id"):
        HUB.set_active(r["id"])
    if not _bg(f"scan:{root}", _scan_job(root, use_asr, with_gif)):
        return _err("这个库正在扫描中，先用 frameseek_task 看进度")
    HUB.touch_scan(root)
    return _ok(started=root, use_asr=use_asr, with_gif=with_gif,
               note="用 frameseek_task 查进度")


@server.tool()
def frameseek_task(
        path: Annotated[str, Field(default="",
                                   description="看某个库的最近任务；"
                                               "省略=看当前激活库")]) -> str:
    """查扫描/识别任务进度。**所有异步工具的轮询入口**。

    status: running / done / error。done 时 message 里带汇总
    （几个素材、几条台词、来源、成片文件名）。
    无论传不传 path，返回里都会附上**全部库正在跑的任务**——
    所以想知道「现在有没有任务在跑」调它就够了。
    轮询间隔建议 5~10 秒，别连着猛打。

    判断异步任务是否结束的判据：`running` 数组里没有它，且 `latest.status`
    是 done / error（不是 running）。
    """
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
def frameseek_search(
        query: Annotated[str, Field(
            description="要搜的台词关键词。子串匹配（不是分词），"
                        "多字连着写命中率更高，如「我愿意」；搜不到就把关键词缩短到 2~3 字")],
        root: Root = "",
        limit: Annotated[int, Field(default=50, ge=1, le=500,
                                    description="最多返回几条（默认 50，上限 500）")] = 50,
        all_libraries: Annotated[bool, Field(
            default=True,
            description="True（默认）=跨全部已登记库搜；False=只搜 root 指定的库")] = True
        ) -> str:
    """按台词搜视频片段。默认跨全部登记的素材库。

    每条结果都带 `root`（所属库的绝对路径）——**后续所有工具都要把这个 root
    原样带回**，因为 line_id 只在它自己的库内唯一，多库时 id 会撞号
    （实测同一库里 id=1 同时是三部不相关的片子）。
    结果里还带 `shot`（台词截图相对路径）和 `start`/`end`（秒）。
    """
    if all_libraries and not root:
        rows = HUB.search_all(query, limit)
    else:
        r = pick_root(root)
        if not r:
            return _err("未设置目录",
                        "先调 frameseek_list_libraries 看有哪些库，"
                        "或用 frameseek_doctor 一次性看全")
        rows = IX.open_store(r).search(query, limit)
        for x in rows:
            x["root"] = r
    slim = [{"line_id": x["id"], "video_id": x["video_id"], "video": x["name"],
             "start": round(x["start"], 2), "start_str": x.get("start_str"),
             "end": x.get("end"), "end_str": x.get("end_str"),
             "text": x["text"], "shot": x.get("shot"),
             "lib": x.get("lib", ""), "root": x.get("root", "")} for x in rows]
    out: Dict[str, Any] = {"count": len(slim), "results": slim}
    if not slim:
        out["hint"] = (
            "没命中。依次排查：① 库是否索引过（frameseek_doctor 看每库 lines 是否为 0，"
            "为 0 就 frameseek_scan）② 关键词太长——缩到 2~3 字再试，"
            "或换同义字 ③ 台词来源：source 是 srt/embedded 的库里有字幕，"
            "纯 OCR 的库可能整体识别不准")
    return _ok(**out)


@server.tool()
def frameseek_list_videos(
        root: Root = "",
        q: Annotated[str, Field(default="",
                                 description="按文件名子串过滤，省略=列出该库全部")] = "",
        limit: Annotated[int, Field(default=100, ge=1, le=1000,
                                    description="最多返回几条（默认 100）")] = 100
        ) -> str:
    """列出**某个库**里的视频（时长、台词条数、带方框凭证的条数、动图路径）。

    注意：只看一个库。跨库找片请用 frameseek_find_videos。
    `lines` 为 0 说明这部片子没识别出台词（可能没字幕且 OCR 没开）。
    """
    r = pick_root(root)
    if not r:
        return _err("未设置目录",
                    "用 frameseek_list_libraries 看有哪些库并取 path，"
                    "或用 frameseek_find_videos（跨库）")
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
    out: Dict[str, Any] = {"root": r, "count": len(videos), "videos": videos}
    if not videos and q:
        out["hint"] = f"在 {r} 里没有文件名含「{q}」的片子；试试缩短关键词（2~3 字）"
    return _ok(**out)


@server.tool()
def frameseek_video_info(
        video_id: Annotated[int, Field(
            description="素材 id（从 frameseek_list_videos / frameseek_find_videos 拿，"
                        "注意只在本库内唯一，跨库会撞号")],
        root: Root = "") -> str:
    """单个素材的详情：时长/分辨率、台词条数与来源、方框凭证统计、
    封面与动图路径（相对库根目录，配合 Web 服务 /media 或 /stream 访问），
    以及**音轨 / 字幕轨清单**（tracks：导出时用来挑语言或内嵌字幕，
    index 可直接喂给 frameseek_export_clip 的 audio_track / sub_track）。"""
    r = pick_root(root)
    if not r:
        return _err("未指定素材库",
                    "video_id 只在它自己的库内唯一，必须同时带正确的 root；"
                    "root 从 frameseek_list_videos / find_videos 结果里拿")
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}",
                    "video_id 只在本库内唯一，跨库会撞号（同一个 id 在不同库"
                    "是完全不相干的片子）。用 frameseek_find_videos 拿 root + id "
                    "成对使用最稳，或先 frameseek_list_videos 确认 id")
    # 音轨 / 字幕轨清单：给「导出时挑一条」用。文件不在了就给空清单，
    # 不让一个可选信息把整个详情工具带崩。
    src = os.path.join(r, v.get("rel") or "")
    tracks = {"audio": [], "subtitle": []}
    if os.path.isfile(src):
        try:
            tracks = media.list_tracks(src)
        except Exception:
            pass
    s = st.box_stats(video_id)
    return _ok(root=r, video={
        "id": v["id"], "name": v["name"], "rel": v.get("rel"),
        "duration": round(v.get("duration") or 0, 2),
        "width": v.get("width"), "height": v.get("height"),
        "md5": v.get("md5"), "status": v.get("status"),
        "line_count": v.get("line_count"), "line_source": v.get("line_source"),
        "err": v.get("err") or "",
        "tracks": tracks,
        "boxed": s["boxed"], "no_box": s["total"] - s["boxed"],
        "cover": v.get("cover"), "gif": v.get("gif")})


@server.tool()
def frameseek_get_lines(
        video_id: Annotated[int, Field(
            description="素材 id（从 frameseek_list_videos / find_videos 拿）")],
        root: Root = "",
        limit: Annotated[int, Field(default=500, ge=1, le=5000,
                                    description="最多返回几条（默认 500；"
                                                "整集通常 500~800 条，可能需要分两次取）")]
        = 500) -> str:
    """列出某个视频的**全部台词**（时间码 + 文本 + 来源 + 截图路径）。

    带方框凭证的行附 box 摘要（ar 字宽比 / hits 支持帧数 / drift 漂移 /
    font 字体指纹 / font_dev 字体偏离度）——font_dev 超标的行是纠错和
    二次验证时要优先怀疑或忽略的对象。
    台词多时调大 limit 或改用 frameseek_video_summary（带摘要，更省 token）。
    """
    r = pick_root(root)
    if not r:
        return _err("未指定素材库",
                    "video_id 只在本库内唯一，必须同时带正确的 root")
    st = IX.open_store(r)
    allrows = st.lines_of(video_id)
    if not allrows:
        return _err(f"素材 {video_id} 没有台词",
                    "可能还没索引（frameseek_scan）或没字幕且 OCR 未开"
                    "（frameseek_retranscribe / frameseek_transcribe_asr）")
    rows = allrows[:limit]
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
    out: Dict[str, Any] = {"root": r, "video_id": video_id, "count": len(lines),
                           "total": len(allrows), "lines": lines}
    if len(allrows) > len(lines):
        out["truncated"] = True
        out["hint"] = (f"共 {len(allrows)} 条，这次只返回前 {len(lines)} 条。"
                       "调大 limit 重取，或改用 frameseek_video_summary")
    return _ok(**out)


# ---------------- 工具：校对单句 ----------------
@server.tool()
def frameseek_recog_line(
        line_id: Annotated[int, Field(
            description="台词 id（从 frameseek_search / frameseek_get_lines 拿，"
                        "必须带对应的 root")],
        mode: Annotated[str, Field(default="ocr", description=
            "ocr（默认）=回画面抽首/中/尾三帧重跑 OCR，适合画面有硬字幕的情况；"
            "asr =只抽这一句时间区间的音频跑语音识别，适合画面没字幕或怀疑听错的情况"
            "（首次调用要加载模型，约 10 秒）")] = "ocr",
        root: Root = "",
        pad: Annotated[float, Field(default=0.6, ge=0, le=10, description=
            "mode=asr 时，这一句前后各多听几秒（默认 0.6，防止词被切掉）")] = 0.6) -> str:
    """重新识别**这一句**，看它到底写对了没有。只报告结果，不改库。

    mode=ocr  回到画面上抽首/中/尾三帧重跑 OCR：返回每帧识别到的文字，
              以及和库里这一句的偏移量（库里有框时）。画面有硬字幕时用这个。
    mode=asr  只抽这一句时间区间前后各 pad 秒的音频跑 faster-whisper：
              返回听到的文字。画面没字幕、或怀疑是听错的时候用这个。

    拿到结果后要不要改库，由 frameseek_edit_line 决定——这个工具永远
    不写库，免得把人工校对过的台词覆盖掉。
    """
    from core import asr as A
    from core import boxaudit as BA
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库")
    st = IX.open_store(r)
    row = st.get_line(line_id)
    if not row:
        return _err(f"在 {r} 里找不到台词 {line_id}",
                               hint="line_id 只在本库内唯一。回查 frameseek_search "
                               "结果里的 root 字段并原样带回这个工具")
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
def frameseek_edit_line(
        line_id: Annotated[int, Field(
            description="台词 id（从 frameseek_search / frameseek_get_lines 拿，"
                        "必须带对应的 root")],
        text: Annotated[Optional[str], Field(
            default=None, description="改成什么文字；省略/null=不动")] = None,
        start: Annotated[Optional[float], Field(
            default=None, description="改起始秒数；省略/null=不动")] = None,
        end: Annotated[Optional[float], Field(
            default=None, description="改结束秒数；省略/null=不动")] = None,
        save_box: Annotated[bool, Field(
            default=False, description=
            "True=顺带回画面抽 3 帧重跑 OCR，三帧文字都对得上才把方框凭证写进库，"
            "对不上整体拒绝、什么都不改。字幕来源是 srt/embedded 的片子一般不需要，"
            "纯 OCR 的片子改了文字才需要") ] = False,
        root: Root = "") -> str:
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
    st = IX.open_store(r)
    row = st.get_line(line_id)
    if not row:
        return _err(f"在 {r} 里找不到台词 {line_id}",
                               hint="line_id 只在本库内唯一。回查 frameseek_search "
                               "结果里的 root 字段并原样带回这个工具")
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
def frameseek_retranscribe(
        video_id: Annotated[int, Field(
            description="素材 id（从 frameseek_list_videos / find_videos 拿）")],
        root: Root = "",
        force: Annotated[bool, Field(default=False, description=
            "True=跳过外挂/内嵌字幕，强制走 OCR（最慢最贵，"
            "只在字幕明显不准时才用）")] = False) -> str:
    """对这个素材**重新识别台词**：先清空旧台词（含旧截图、全文索引），
    再按优先级链重跑（同名 .srt → 内嵌字幕 → OCR 画面硬字幕）。
    ⚠️ 旧的会被清掉，且**不可撤销**——先 frameseek_video_summary 看一眼现状，
    确认真的要重来再调。
    异步：立即返回，用 frameseek_task 轮询（间隔 5~10 秒）。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库或目录不存在",
                    "用 frameseek_list_libraries 取 path，"
                    "或省略 root 走激活库")
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}",
                             hint="video_id 只在本库内唯一。先 frameseek_list_libraries "
                             "看有哪些库，再对该库调 frameseek_list_videos 确认 id；"
                             "或直接用 frameseek_find_videos 跨库搜文件名")
    key = f"retranscribe:{r}:{video_id}"
    if not _bg(key, _retranscribe_job(r, video_id, force)):
        return _err("这个素材正在重识别中，先用 frameseek_task 看进度")
    return _ok(started=True, name=v["name"], root=r, force=force,
               lines_before=v.get("line_count") or 0,
               note="用 frameseek_task 查进度")


@server.tool()
def frameseek_transcribe_asr(
        video_id: Annotated[int, Field(
            description="素材 id（从 frameseek_list_videos / find_videos 拿）")],
        root: Root = "",
        model: Annotated[str, Field(default="", description=
            "语音模型：tiny / base / small / medium / large-v3，留空用默认 small。"
            "small 中文够用；tiny 快约 3 倍但错字明显多；"
            "长片在 CPU 上很慢，宁可先用 tiny 试")] = "",
        lang: Annotated[str, Field(default="",
                                    description="语言代码，如 zh / en；留空=自动")] = "") -> str:
    """语音识别（faster-whisper）：不读外挂/内嵌字幕、不跑 OCR，直接听音轨。
    给画面没有硬字幕的电影用。同样先清空旧台词再写，并落同名 .srt + 逐条截图。
    异步：立即返回，用 frameseek_task 轮询（间隔 5~10 秒）。
    响应里带 device（gpu/cpu）和时长，可粗估等待时间。"""
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库或目录不存在",
                    "用 frameseek_list_libraries 取 path，或省略 root 走激活库")
    m = (model or "").strip()
    if m and m not in ASR_MODELS_OK:
        return _err(f"不支持的模型：{m}（可选 {', '.join(ASR_MODELS_OK)}）")
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}",
                             hint="video_id 只在本库内唯一。先 frameseek_list_libraries "
                             "看有哪些库，再对该库调 frameseek_list_videos 确认 id；"
                             "或直接用 frameseek_find_videos 跨库搜文件名")
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


# ---------------- 工具：校验 / 复查 ----------------
@server.tool()
def frameseek_verify(
        video_id: Annotated[int, Field(
            description="素材 id（从 frameseek_list_videos / find_videos 拿）")],
        root: Root = "",
        check: Annotated[bool, Field(
            default=False, description=
            "True=附逐条结论（含可疑行的 reasons）；False=只要汇总")] = False,
        limit: Annotated[int, Field(default=50, ge=1, le=1000, description=
            "check=True 时最多返回几条明细（按时间序取前面的）")] = 50) -> str:
    """方框校验：这条台词在画面上真有框支撑吗。**纯读库（毫秒级，不重跑 OCR）**。

    返回通过率 + 不达标原因分布；check=True 时附逐条结论（可疑行带 reasons）。
    来源是 srt/embedded/asr 的台词判"不适用"(skipped)，不计失败——
    skipped 高说明这部片子本来就有字幕、没做 OCR，rate 低不代表质量差。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）",
                                   hint="用 frameseek_list_libraries 取一个库 的 path 传进来，或省略 root 走激活库")
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}",
                             hint="video_id 只在本库内唯一。先 frameseek_list_libraries "
                             "看有哪些库，再对该库调 frameseek_list_videos 确认 id；"
                             "或直接用 frameseek_find_videos 跨库搜文件名")
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
def frameseek_boxaudit(
        video_id: Annotated[int, Field(
            description="素材 id（从 frameseek_list_videos / find_videos 拿）")],
        root: Root = "",
        limit: Annotated[int, Field(default=50, ge=1, le=2000, description=
            "最多返回多少条明细（按时间序取前面的）；全量汇总不受它影响")] = 50
        ) -> str:
    """方框复查清单：每条台词的凭证指标（字宽比 ar / 支持帧 hits / 漂移 drift /
    字体指纹 font / 字体偏离 font_dev）+ 通过与否 + 不达标原因。
    用来定位"哪几条可疑、为什么"。纯读库，不重跑 OCR（现场重检要用 Web 页面）。
    **排查套路**：先看 font_dev 超标（上限见返回里的 font_dev_max）的行——
    那是字体指纹偏离本片主流字体的，多半是错行/水印，其次看 hits（支持帧太少）。
    """
    from core import boxaudit as BA
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）",
                                   hint="用 frameseek_list_libraries 取一个库 的 path 传进来，或省略 root 走激活库")
    st = IX.open_store(r)
    v = st.get_video(video_id)
    if not v:
        return _err(f"在 {r} 里找不到素材 {video_id}",
                             hint="video_id 只在本库内唯一。先 frameseek_list_libraries "
                             "看有哪些库，再对该库调 frameseek_list_videos 确认 id；"
                             "或直接用 frameseek_find_videos 跨库搜文件名")
    rep = BA.audit_video(st, video_id)
    items = rep.pop("items", [])
    rep["root"] = r
    rep["items"] = items[:limit]
    rep["items_truncated"] = len(items) > limit
    return json.dumps(rep, ensure_ascii=False)


def _is_auto_mode(mode) -> bool:
    """「导出字幕」是不是 auto 档。只有 auto 才走「内嵌字幕回退」那条路：
    force/off 是调用方明确表了态，不该被自动逻辑改写。布尔旧接口也算表态。
    （与 app.py 的同名函数保持一致，两边不互相 import：app 是 Flask 应用。）"""
    if mode is None or mode == "":
        return True
    if isinstance(mode, bool):
        return False
    return str(mode).strip().lower() in ("auto", "none", "null")


# ---------------- 工具：导出 ----------------
@server.tool()
def frameseek_list_clips(
        root: Root = "",
        q: Annotated[str, Field(default="",
                                 description="按文件名/影片名过滤，省略=全列")] = "",
        limit: Annotated[int, Field(default=50, ge=1, le=500,
                                    description="最多返回几条（默认 50，按导出时间倒序）")]
        = 50,
        all_libraries: Annotated[bool, Field(
            default=True, description="True（默认）=跨全部库；False=只看 root")] = True
        ) -> str:
    """列出**已经导出过的片段**（跨会话累积，不是本次会话临时生成的）。

    片段落在 `<库>/.frameseek/clips/`，所以 Web 端导过的这里也能看到——
    刷新浏览器丢了结果、或想确认「上次那个片段存哪了」时用这个。
    每条带文件名、影片名、时长、体积、导出时间、有没有烧字幕。
    """
    targets: List[str] = []
    if all_libraries and not root:
        targets = [l["path"] for l in HUB.list() if l.get("exists")]
    else:
        r = pick_root(root)
        if not r or not os.path.isdir(r):
            return _err("未指定素材库或目录不存在",
                        "用 frameseek_list_libraries 取 path，"
                        "或让 all_libraries=True（默认）跨全部库")
        targets = [r]

    def _dur(p: str):
        try:
            m = media.video_meta(p)
            return round(float(m.get("duration") or 0), 2)
        except Exception:
            return 0.0

    items: List[Dict[str, Any]] = []
    ql = (q or "").strip().lower()
    for r in targets:
        cdir = os.path.join(IX.cache_dir(r), C.CLIP_DIR)
        if not os.path.isdir(cdir):
            continue
        try:
            names = sorted(os.listdir(cdir), reverse=True)
        except Exception:
            continue
        for fn in names:
            if not fn.lower().endswith(".mp4"):
                continue
            if ql and ql not in fn.lower():
                continue
            fp = os.path.join(cdir, fn)
            try:
                stt = os.stat(fp)
            except OSError:
                continue
            items.append({
                "name": fn, "path": fp, "root": r, "lib": _lib_name_of(r),
                "size_mb": round(stt.st_size / 1048576, 2),
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(stt.st_mtime)),
                "subtitle": os.path.exists(os.path.splitext(fp)[0] + ".srt")})
    items.sort(key=lambda x: x["name"], reverse=True)
    total = len(items)
    items = items[:limit]
    # 时长要对每个文件 ffprobe，全部探会很慢；只探返回的这一批
    for it in items:
        it["duration"] = _dur(it["path"])
    res: Dict[str, Any] = {"count": len(items), "clips": items,
                           "total": total}
    if not items:
        res["hint"] = ("还没有导出过片段。先 frameseek_search 找台词 → "
                       "frameseek_export_clip 导出")
    elif total > len(items):
        res["hint"] = f"共 {total} 个片段，只返回最近 {len(items)} 个，调大 limit"
    return _ok(**res)


@server.tool()
def frameseek_export_clip(
        line_id: Annotated[Optional[int], Field(
            default=None, description=
            "单条台词 id（从 frameseek_search 结果拿）。要导多条用 line_ids")] = None,
        line_ids: Annotated[Optional[List[int]], Field(
            default=None, description=
            "批量导出：台词 id 数组，全部在同一个 root 内。"
            "逐条调用 frameseek_export_clip 更稳，批量适合确定要一起出片的场景")] = None,
        pre: Annotated[float, Field(default=5.0, ge=0, le=120, description=
            "这句台词**之前**留几秒（默认 5）")] = 5.0,
        post: Annotated[float, Field(default=5.0, ge=0, le=120, description=
            "这句台词**之后**留几秒（默认 5）")] = 5.0,
        fast: Annotated[bool, Field(
            default=True,
            description="True（默认）= ffmpeg -c copy 秒出但切点会前移到关键帧，"
                        "时间可能差零点几秒；False=重编码，帧精确但慢几倍")] = True,
        subtitle: Annotated[str, Field(
            default="auto", description=
            "烧字幕三档："
            "auto（默认）= 画面本来就带字幕的素材（台词来自 OCR 识别）自动跳过烧录，"
            "其余照旧烧；"
            "off = 一律不烧；"
            "force = 一律烧（OCR 字幕有错字、纠完想重新烧一份时用这个）。"
            "只影响导出；烧字幕会强制重编码（忽略 fast），比秒切慢几倍")] = "auto",
        audio_track: Annotated[Optional[int], Field(
            default=None, description=
            "指定用哪条**音轨**（ffprobe 的绝对流索引，从 frameseek_video_info "
            "返回的 tracks.audio[].index 拿）。多音频轨的片子（国语/粤语/原声各一条）"
            "用它选语言；不给 = 交给 ffmpeg 挑它认为最佳的那条，和以前一样")] = None,
        sub_track: Annotated[Optional[int], Field(
            default=None, description=
            "指定烧哪条**内嵌字幕轨**（绝对流索引，从 tracks.subtitle[].index 拿）。"
            "给了就以它为准，不再烧项目台词库里的台词；位图字幕（PGS/DVDSUB）"
            "也能烧——所以选内嵌轨走的是滤镜直烧，不先抽 SRT。不给则维持原逻辑")] = None,
        root: Root = "") -> str:
    """导出台词前后各 N 秒的片段，返回 mp4 的绝对路径（可直接给播放器/剪辑软件）。

    line_id 从 frameseek_search / frameseek_get_lines 拿，**root 用结果里的 root**
    （多库时不能省：line_id 只在它自己的库内唯一，跨库会撞号）。
    成片落在 `<root>/.frameseek/clips/`，文件名带影片名和起始秒，便于人工辨认。

    片源有多条音轨 / 内嵌字幕轨时，先用 frameseek_video_info 看 tracks，
    再用 audio_track / sub_track 指定要哪一条。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库，或目录已不存在",
                    "用 frameseek_list_libraries 确认路径，"
                    "或直接复用 frameseek_search 结果里的 root 字段")
    ids: List[int] = []
    if line_ids:
        ids = [int(x) for x in line_ids]
    if line_id is not None:
        ids.insert(0, int(line_id))
    if not ids:
        return _err("没给要导出的台词",
                    "传 line_id（单条）或 line_ids（批量），"
                    "id 从 frameseek_search / frameseek_get_lines 拿")

    st = IX.open_store(r)
    clips: List[Dict[str, Any]] = []
    sub_notes: List[str] = []
    for lid in ids:
        ln = st.get_line(lid)
        if not ln:
            return _err(f"在 {r} 里找不到台词 {lid}",
                        "这个 id 属于别的库——line_id 只在它自己的库内唯一。"
                        "回查 frameseek_search 结果里的 root 并原样带回",
                        asked=list(ids), missing=lid)
        v = st.get_video(ln["video_id"])
        if not v:
            return _err(f"台词 {lid} 所属的素材已不在库里",
                        "素材可能被移走了，用 frameseek_list_videos 看该库还有哪些片")
        src = os.path.join(r, v["rel"])
        if not os.path.exists(src):
            return _err("视频文件不在了：" + v["rel"],
                        "磁盘上找不到这个文件，可能被移动/删除；"
                        "重新 frameseek_scan 该库可以刷新索引")
        s = max(0.0, float(ln["start"]) - float(pre))
        e = (float(ln.get("end") or ln["start"])) + float(post)
        tag = "".join(ch if ch not in '\\/:*?"<>| .' else "_"
                      for ch in f"{os.path.splitext(v['name'])[0][:24]}_{ln['start']:.2f}")
        dst = os.path.join(IX.cache_dir(r), f"{C.CLIP_DIR}/{tag}_{lid}.mp4")

        line_src = v.get("line_source") or ln.get("src") or ""
        # 本次这条用哪条字幕轨。不能改写外层 sub_track——批量导出时那会污染
        # 后续每条（第二条起就带着第一条的回退结果）。
        sti = sub_track
        srt_path = ""
        if sti is not None:
            burn, sub_note = True, ""
        else:
            burn, sub_note = media.decide_burn(subtitle, line_src)
            # 内嵌字幕回退：片源自带字幕轨就烧它，别让原片字幕导出后凭空消失。
            # 只在 auto 档生效；ocr 源由 auto_embedded_sub 自己挡掉（画面已有
            # 字幕，再烧会重影）。
            if _is_auto_mode(subtitle) and os.path.isfile(src):
                _si = media.auto_embedded_sub(src, line_src)
                if _si is not None:
                    sti, burn = _si, True
                    sub_note = "已烧片源内嵌字幕轨（保留原片字幕）"
            if sub_note:
                sub_notes.append(sub_note)
        if burn and sti is None:
            # 窗口内的台词按片段相对时间写一份 SRT；没有重叠台词就不烧
            dur = max(0.2, e - s)
            rows = []
            for l2 in st.lines_of(ln["video_id"]):
                t0 = float(l2.get("start") or 0)
                t1 = float(l2.get("end") or t0 + 2)
                if t1 <= s or t0 >= e:
                    continue
                rows.append({"text": l2.get("text", ""),
                             "start": max(0.0, t0 - s),
                             "end": min(dur, t1 - s)})
            if rows:
                srt_path = media.write_srt_at(
                    os.path.splitext(dst)[0] + ".srt", rows, dur=dur)

        if not media.export_clip(src, s, e, dst, reencode=not fast,
                                 srt=srt_path or None,
                                 audio_track=audio_track, sub_track=sti):
            return _err(f"导出失败（ffmpeg？）台词 {lid}",
                        "看 stderr 里 ffmpeg 的报错；常见是磁盘满或源文件损坏")
        clips.append({"line_id": lid, "path": dst, "video": v["name"],
                      "text": ln.get("text", ""), "start": round(s, 2),
                      "end": round(e, 2),
                      "subtitle_burned": bool(srt_path or sti is not None),
                      "sub_track": sti, "audio_track": audio_track,
                      "video_src": v.get("line_source") or "",
                      "srt": srt_path})
    out: Dict[str, Any] = {"count": len(clips), "clips": clips,
                           "sub_mode": str(subtitle).strip().lower()
                           if not isinstance(subtitle, bool) else (
                               "force" if subtitle else "off"),
                           "dir": os.path.join(IX.cache_dir(r), C.CLIP_DIR)}
    if sub_notes:
        out["sub_note"] = sub_notes[0]
    # 兼容旧形态：单条时 path 放顶层，别让已写好的调用链断掉
    if len(clips) == 1:
        out.update({k: clips[0][k] for k in
                    ("path", "video", "text", "start", "end", "subtitle_burned", "srt")})
    out["root"] = r
    return _ok(**out)


# ================================================================
# 资源层：让 LLM「读一次拿到全局快照」，不必逐个工具调
#
# 为什么加：素材库现状 / 在跑任务 / 已导出片段这三样东西，模型每次都得
# 连着调 2~3 个工具拼出来，还容易漏。做成资源后一次 read 就全拿到，
# 而且是「当前真实状态」而不是模型记忆里的旧状态。
# ================================================================

def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


@server.resource("frameseek://libraries",
                 name="素材库全览",
                 description="全部已登记素材库的快照：路径、素材数、台词数、"
                             "台词来源、是否索引过、目录是否丢失。"
                             "这是所有工具 root 参数的取值来源。",
                 mime_type="application/json")
def _res_libraries() -> str:
    libs = [_lib_json(l) for l in HUB.list()]
    return _j({
        "active": HUB.active_path(),
        "libraries": libs,
        "total": {"libraries": len(libs),
                  "videos": sum(l["videos"] for l in libs),
                  "lines": sum(l["lines"] for l in libs),
                  "duration": round(sum(l["duration"] for l in libs), 1)},
        "usage": "root 参数=这里的 path 字段原样复制；"
                 "video_id / line_id 只在各自库内唯一，必须成对使用"})


@server.resource("frameseek://tasks/running",
                 name="正在跑的任务",
                 description="所有库当前正在跑的扫描/识别任务及进度。"
                             "发起异步操作前后读这个。",
                 mime_type="application/json")
def _res_tasks() -> str:
    running: List[Dict[str, Any]] = []
    for l in HUB.list():
        if not l.get("exists"):
            continue
        if not os.path.exists(os.path.join(l["path"], C.CACHE_DIR, C.DB_NAME)):
            continue
        try:
            t = IX.open_store(l["path"]).latest_task(l["path"])
        except Exception:
            t = None
        if t and t.get("status") == "running":
            t = dict(t)
            t["lib"] = l["name"]
            t["root"] = l["path"]
            running.append(t)
    return _j({"count": len(running), "running": running,
              "note": "空数组=当前没有任务在跑，可以发起新的异步操作了"})


@server.resource("frameseek://clips",
                 name="已导出片段清单",
                 description="历史上导出过的全部片段（含 Web 端导的），"
                             "带路径/时长/体积/导出时间/有无字幕。",
                 mime_type="application/json")
def _res_clips() -> str:
    items: List[Dict[str, Any]] = []
    for l in HUB.list():
        if not l.get("exists"):
            continue
        cdir = os.path.join(IX.cache_dir(l["path"]), C.CLIP_DIR)
        if not os.path.isdir(cdir):
            continue
        try:
            names = sorted(os.listdir(cdir), reverse=True)[:40]
        except Exception:
            continue
        for fn in names:
            if not fn.lower().endswith(".mp4"):
                continue
            fp = os.path.join(cdir, fn)
            try:
                stt = os.stat(fp)
            except OSError:
                continue
            items.append({"name": fn, "path": fp, "lib": l["name"],
                          "root": l["path"],
                          "size_mb": round(stt.st_size / 1048576, 2),
                          "subtitle": os.path.exists(
                              os.path.splitext(fp)[0] + ".srt")})
    return _j({"count": len(items), "clips": items,
              "note": "每库最多列最近 40 个；要完整清单用工具 frameseek_list_clips"})


@server.resource("frameseek://tool-catalog",
                 name="工具速查表",
                 description="按「我想干什么」组织的工具索引——"
                             "比逐个读 tools/list 的描述快得多。",
                 mime_type="application/json")
def _res_catalog() -> str:
    return _j({
        "按意图查工具": {
            "「这台机器现在什么状态」": "frameseek_doctor（一次给全）",
            "「有哪些素材库」": "资源 frameseek://libraries 或工具 frameseek_list_libraries",
            "「有哪些正在跑的任务」": "资源 frameseek://tasks/running 或工具 frameseek_task",
            "「找某部片子（认片名）」": "frameseek_find_videos → frameseek_video_summary",
            "「某句台词在哪部片（认台词）」": "frameseek_search → frameseek_export_clip",
            "「这片讲了什么」": "frameseek_video_summary（一次给元信息+台词全文）",
            "「导出片段」": "frameseek_export_clip（subtitle=true 可烧字幕，支持批量）",
            "「导出过哪些片段」": "资源 frameseek://clips 或工具 frameseek_list_clips",
            "「这句台词准不准」": "frameseek_recog_line → 采纳后 frameseek_edit_line",
            "「这句在画面上有框吗」": "frameseek_verify（汇总）/ frameseek_boxaudit（逐条）",
            "「给目录建索引」": "frameseek_add_library → frameseek_scan（异步）",
            "「这片没台词」": "frameseek_retranscribe（有字幕的）/ "
                           "frameseek_transcribe_asr（没字幕的）",
        },
        "三条铁律": [
            "1. 多库场景 root 必须带：video_id / line_id 只在各自库内唯一，跨库撞号",
            "2. 异步工具立即返回不等待，之后用 frameseek_task 轮询，间隔 5~10 秒",
        ],
        "参数速记": {
            "root": "素材库绝对路径；从任何查询结果里原样复制",
            "pre/post": "台词前后各留几秒（默认 5/5）",
            "fast": "导出时 True=秒出但切点可能漂，False=帧精确",
            "subtitle": "导出时 True=把台词硬烧进画面",
            "limit": "各查询工具的返回条数上限",
            "use_asr": "scan 的历史命名，真实语义是「允不允许对没字幕的素材跑 OCR/ASR」",
        },
    })


if __name__ == "__main__":
    HUB.migrate_json()
    asyncio.run(server.run_stdio_async())
