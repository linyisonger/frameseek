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
from core import gen as gen_mod  # noqa: E402
from core import handdraw as hd_mod  # noqa: E402
from core import img_backend as img_mod  # noqa: E402
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
    "【异步任务】frameseek_scan / frameseek_retranscribe / frameseek_transcribe_asr / "
    "frameseek_generate_video / frameseek_handdraw_render / frameseek_handdraw_mix "
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
    "【AI 生成·图】frameseek_generate_image 出**静态图**（PNG），双后端可插拔："
    "agnes 云端秒级、sd15 本地种子可复现（重掷收敛，主力）。"
    "默认 auto 按 agnes→sd15 降级。"
    "产物落项目目录 AI生成/绘画/，**同步返回**（agnes 10~60 秒 / sd15 5~20 秒）。"
    "不满意用 frameseek_reroll_image 按留痕参数换种子重掷，"
    "别自己重新拼参数。发起前 frameseek_img_info 看哪个后端就绪。\n"
    "\n"
    "【AI 生成·视频】frameseek_generate_video 按提示词生成视频，成片落项目目录 "
    "AI生成/（不入库），异步。**双后端可选**：backend=agnes（云端 Agnes Video 2.5 "
    "Flash，1280x704、4~12 秒、自带音轨、几十秒~几分钟、需联网，高峰可能排队）／"
    "backend=h3（本地 MiniMax H3，736x416、3~15 秒、纯离线、很慢，独占 GPU）／"
    "backend=auto（默认，agnes→h3 降级）。提示词 H3 用英文、Agnes 可写中文。"
    "发起前用 frameseek_gen_info 看哪个后端就绪。\n"
    "\n"
    "【白板手绘（另一条线，本地 CPU 不占 GPU，可与上面并行）】"
    "frameseek_handdraw_info → frameseek_handdraw_init 建工程 → "
    "frameseek_handdraw_scenes 出分幕建议 → 逐幕 frameseek_handdraw_annotate 写区域标注"
    "（这是硬前置，结构见资源 frameseek://schema/handdraw-annotation）→ "
    "frameseek_handdraw_region_preview 核对 → frameseek_handdraw_render 渲染 → "
    "frameseek_handdraw_voice 试听配音（联网，需 allow_network=true）→ "
    "frameseek_handdraw_mix 合成。手绘是分步的，别指望一句话出片。\n"
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
    """视频生成任务：进度写 tasks 表，和扫描/识别同一套通道。
    后端由 params['backend'] 决定（auto / agnes / h3）。"""
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
    """生成引擎现状（双后端：Agnes 云端 + 本地 H3）+ 参数默认值。"""
    try:
        info = dict(gen_mod.engine_info())
        info.update({"aspects": list(gen_mod.ASPECTS),
                     "agnes_aspects": sorted(set(gen_mod.AGNES_ASPECTS.values())),
                     "agnes_unsupported": list(gen_mod.AGNES_UNSUPPORTED),
                     "agnes_seconds": [gen_mod.AGNES_SECONDS_MIN,
                                       gen_mod.AGNES_SECONDS_MAX],
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
    """列出所有登记的素材库（路径、素材数、台词数、是否索引过、目录是否丢失）

    返回的 `path` 就是其它所有工具的 `root` 参数值。
    """
    return _ok(hub_db=HUB.db_path, active=HUB.active_path(),
               libraries=[_lib_json(l) for l in HUB.list()])


@server.tool()
def frameseek_doctor() -> str:
    """一键体检：调用任何其它工具前，或工具报错看不懂时，先跑这个。

    一次给全：Python/ffmpeg/OCR/ASR/生成引擎/手绘引擎的可用性，
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
    gen = _gen_info()
    hd = _hd_info()
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
        engines={"ocr": ocr, "asr": asr, "gen": gen, "handdraw": hd,
                 "ffmpeg": bool(_ffmpeg_ok())},
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
def frameseek_task(
        path: Annotated[str, Field(default="",
                                   description="看某个库的最近任务；"
                                               "省略=看当前激活库")]) -> str:
    """查扫描/识别/生成/渲染任务进度。**所有异步工具的轮询入口**。

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
    封面与动图路径（相对库根目录，配合 Web 服务 /media 或 /stream 访问）。"""
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
    g = _ai_guard(r)
    if g:
        return _err(g)
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
    g = _ai_guard(r)
    if g:
        return _err(g)
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
    g = _ai_guard(r)
    if g:
        return _err(g)
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
    g = _ai_guard(r)
    if g:
        return _err(g)
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


# ---------------- 工具：图片生成（双后端） ----------------
@server.tool()
def frameseek_img_info() -> str:
    """图片生成现状：两个生图后端各自就绪没有、大概多久、要不要联网，
    以及产物落哪个目录。发起生成前先看这个，别盲目指定后端。"""
    try:
        bs = img_mod.backends()
    except Exception as e:
        return _err(f"摸底失败：{type(e).__name__}: {e}",
                    hint="出图依赖 ComfyUI 便携版和 sdgen.py，"
                         "确认 FRAMESEEK_COMFY_ROOT 指向 D:\\project\\AIProjects\\ComfyUI_windows_portable")
    ready = [b["id"] for b in bs if b.get("ready")]
    return _ok(backends=bs, ready=ready, order=list(img_mod.BACKEND_ORDER),
               default="agnes",
               out_dir=img_mod.paint_dir(),
               hint=("没有后端就绪时只能出视频（frameseek_generate_video）"
                     "或手绘（frameseek_handdraw_*）"
                     if not ready else ""))


@server.tool()
def frameseek_generate_image(
        prompt: Annotated[str, Field(description=
            "画面提示词。中文可以直接写（Agnes 是中文模型）；"
            "走 sd15 后端时英文效果更稳。"
            "主体、风格、构图、光线都写进去，别写镜头运动")],
        backend: Annotated[str, Field(default="auto", description=
            "生图后端。auto（默认）按 agnes → sd15 依次降级，"
            "前面的没就绪或失败就换下一个。"
            "agnes=Agnes Image 2.5 Flash（云端，秒级，1K/2K，最多 3 张参考图，需联网）；"
            "sd15=本地 SD1.5 + 线稿 LoRA + canny ControlNet（2~5 秒，种子可复现，"
            "参考图走 canny 锁构图不漂，**重掷就靠它**）"
            )] = "auto",
        negative: Annotated[str, Field(default="", description=
            "反向提示词（要避开的东西），如“多余手指、模糊、水印”。"
            "只有 sd15 后端吃这个参数，agnes 会忽略")] = "",
        refs: Annotated[Optional[List[str]], Field(default=None, description=
            "参考图绝对路径列表。agnes 最多 3 张（多图合成）；"
            "sd15 用首张算 canny 控制图锁构图。"
            "省略=纯文生图。**必须是磁盘上真实存在的文件**")] = None,
        width: Annotated[int, Field(default=0, ge=0, le=1024, description=
            "出图宽度，8 的倍数、256~1024。0=按后端默认（sd15 用 512，"
            "agnes 按 ratio 推 1K/2K）")] = 0,
        height: Annotated[int, Field(default=0, ge=0, le=1024, description=
            "出图高度，规则同 width。宽高都 0 时走后端默认尺寸")] = 0,
        ratio: Annotated[str, Field(default="", description=
            "宽高比（如 16:9 / 1:1 / 9:16）。**agnes 后端按它定 1K/2K 分辨率**，"
            "给了就优先于 width/height。sd15 是方形模型，给了会被忽略")] = "",
        seed: Annotated[int, Field(default=0, description=
            "随机种子。0=每次随机；固定同一个 seed 可复现同一张图。"
            "**换 seed 重掷是 sd15 的正确用法**（参数留痕在同名 sidecar JSON 里）")] = 0,
        steps: Annotated[int, Field(default=20, ge=1, le=60, description=
            "采样步数，只对 sd15 有效。20 出图干净；赶时间可降到 12 左右")] = 20,
        cfg: Annotated[float, Field(default=7.0, ge=1, le=15, description=
            "提示词服从度，只对 sd15 有效。7~8 常规；要更贴提示词往上调")] = 7.0,
        name: Annotated[str, Field(default="", description=
            "产物文件名（不含扩展名），留空按提示词自动生成")] = "",
        root: Root = "") -> str:
    """生成一张图片（PNG），落 <项目根>/AI生成/绘画/，旁边带同名 sidecar JSON
    留痕全部参数。双后端可插拔，见 frameseek_img_info。

    同步执行并直接返回文件路径——agnes 通常 10~60 秒，sd15 5~20 秒。
    **GPU 同时只能跑一个**，别并发调。

    想要「同一张图换个种子重掷」用 frameseek_reroll_image，
    它会照 sidecar 里的原参数复现，比自己重新拼参数靠谱。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return _err("提示词不能为空")
    b = (backend or "auto").strip().lower()
    if b not in ("agnes", "sd15", "auto"):
        return _err(f"未知后端：{b}（可选 agnes / sd15 / auto）")
    ref_list = [os.path.abspath(x) for x in (refs or []) if x]
    for x in ref_list:
        if not os.path.isfile(x):
            return _err("参考图不存在：" + x,
                        hint="refs 要给磁盘绝对路径，"
                             "不是 URL 也不是素材库里的相对路径")
    w, h = int(width or 0), int(height or 0)
    for label, v in (("宽", w), ("高", h)):
        if v and (v % 8 or not (256 <= v <= 1024)):
            return _err(f"{label}必须是 8 的倍数且在 256~1024 之间（当前 {v}）")
    if ratio:
        ref_list = ref_list[:3] if b == "agnes" else ref_list[:1]

    # 同步执行，进度只能记在闭包里最后回给调用方（MCP 这条路没有事件通道）
    trace: List[str] = []

    def tick(pct, msg):
        if msg and (not trace or trace[-1] != msg):
            trace.append(str(msg))

    try:
        res = img_mod.gen_image(backend=b, prompt=prompt,
                                negative=(negative or "").strip(),
                                refs=ref_list, seed=int(seed or 0),
                                w=w, h=h, out_dir="",
                                name=(name or "").strip(), steps=int(steps),
                                cfg=float(cfg), on_tick=tick)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}",
                    hint="看 %s 里的 stderr；agnes 后端要联网，"
                         "sd15 失败通常是 ComfyUI 目录或模型权重缺失" %
                         img_mod.__name__.rsplit(".", 1)[0])
    if not res or not res.get("file"):
        return _err("出图没有产物",
                    hint="auto 模式下三个后端可能都没就绪，先 frameseek_img_info")
    res["note"] = res.get("note") or ""
    if trace:
        res["progress"] = trace
    res["reroll_hint"] = ("不满意就 frameseek_reroll_image(file=该png路径)，"
                          "同参数换新种子")
    return _ok(**res)


@server.tool()
def frameseek_reroll_image(
        file: Annotated[str, Field(description=
            "要重掷的那张图的**绝对路径**（frameseek_generate_image 返回的 file，"
            "或 frameseek_list_images 列表里的）。"
            "工具会读它旁边的同名 .json 取出原始参数")],
        root: Root = "") -> str:
    """按某张图留痕的参数原样复现一张，只换新随机种子。**重掷就靠这个**，
    不用自己重新拼提示词和参数。sd15 后端下种子可复现，所以重掷是收敛的：
    连着掷几次就能挑到想要的构图。

    同步执行，直接返回新图路径。产物同样落 <项目根>/AI生成/绘画/。
    """
    f = (file or "").strip()
    if not f or not os.path.isfile(f):
        return _err("文件不存在：" + f,
                    hint="要给绝对路径。frameseek_list_images 能列出所有产物")
    side = os.path.splitext(f)[0] + ".json"
    if not os.path.isfile(side):
        return _err("没有参数留痕（sidecar JSON 缺失）",
                    hint="sidecar 是出图时自动写的；"
                         "缺它就没法复现，只能重新调 frameseek_generate_image")
    try:
        with open(side, encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception as e:
        return _err(f"sidecar 读取失败：{e}")
    if not meta.get("prompt"):
        return _err("sidecar 里没有提示词")
    _rb = meta.get("backend") or "agnes"
    if _rb not in ("agnes", "sd15"):   # 老 sidecar 若记了已下线的 h3，回落 auto
        _rb = "auto"
    kwargs = dict(
        backend=_rb,
        prompt=meta.get("prompt") or "",
        negative=meta.get("negative") or "",
        refs=[x for x in (meta.get("refs") or []) if x and os.path.isfile(x)],
        seed=0,                       # 0 = 随机新种子
        w=int(meta.get("w") or 0), h=int(meta.get("h") or 0),
        lora="" if meta.get("lora") is None else str(meta.get("lora")),
        lora_scale=(img_mod.DEFAULT_LORA_SCALE if meta.get("lora_scale") is None
                    else float(meta["lora_scale"])),
        control_strength=(img_mod.DEFAULT_CONTROL_STRENGTH
                          if meta.get("control_strength") is None
                          else float(meta["control_strength"])),
        steps=int(meta.get("steps") or 20), cfg=float(meta.get("cfg") or 7.0))
    try:
        res = img_mod.gen_image(**kwargs)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    if not res or not res.get("file"):
        return _err("重掷失败，没有新产物")
    res["rerolled_from"] = f
    return _ok(**res)


@server.tool()
def frameseek_list_images(
        limit: Annotated[int, Field(default=30, ge=1, le=200, description=
            "最多返回几条（新的在前）。默认 30")] = 30) -> str:
    """列出 <项目根>/AI生成/绘画/ 里的图片产物（新的在前），
    每条带生成时留痕的参数（提示词/后端/种子/尺寸/参考图）。
    想重掷其中某张就拿它的 file 调 frameseek_reroll_image。"""
    try:
        items = img_mod.list_outputs(limit)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    slim = [{"file": x["file"], "name": x["name"],
             "size_kb": x.get("size_kb"), "backend": (x.get("meta") or {}).get("backend"),
             "prompt": ((x.get("meta") or {}).get("prompt") or "")[:80],
             "seed": (x.get("meta") or {}).get("seed"),
             "w": (x.get("meta") or {}).get("w"), "h": (x.get("meta") or {}).get("h")}
            for x in items]
    return _ok(count=len(slim), dir=img_mod.paint_dir(), images=slim)


# ---------------- 工具：视频生成（MiniMax H3） ----------------
@server.tool()
def frameseek_gen_info() -> str:
    """视频生成现状：**双后端**（Agnes Video 2.5 Flash 云端 / 本地 MiniMax H3）、
    各自就绪状态、支持宽高比、默认像素档、提示词长度上限、成片落入哪个子目录。"""
    return json.dumps(_gen_info(), ensure_ascii=False)


@server.tool()
def frameseek_generate_video(
        prompt: Annotated[str, Field(description=
            "画面提示词。**H3 用英文效果最好**（这套模型中文理解弱）；"
            "**Agnes 是中文模型，直接写中文即可**。必须 <2000 字符。"
            "相机运动 H3 可写成 [slow zoom in] / [pan left] 这类方括号标注")],
        backend: Annotated[str, Field(default="auto", description=
            "视频后端。auto（默认）= agnes → h3 依次降级（云端快，失败回落本地）；"
            "agnes = Agnes Video 2.5 Flash（云端，1280x704，4~12 秒，数十秒~几分钟，"
            "需联网，免费账号高峰可能排队报 video_queue_full）；"
            "h3 = 本地 MiniMax H3（离线，736x416，3~15 秒，12 步采样，慢，"
            "10 秒片约 40 分钟，且同一时刻只能跑一个）")] = "auto",
        duration: Annotated[float, Field(default=5.0, ge=3, le=15, description=
            "成片秒数。H3 内部按 24fps 折成 124~362 帧；"
            "Agnes 取整到 4~12 秒。越久越慢，先用 5 秒试构图再决定") ] = 5.0,
        aspect: Annotated[str, Field(default="16:9 (Widescreen)", description=
            "宽高比，见 frameseek_gen_info 返回的 aspects 列表（要用原字符串，"
            "带括号和空格，别自己简写）。注意 2:3 和 3:2 只有 H3 支持")] = "16:9 (Widescreen)",
        megapixels: Annotated[float, Field(default=0.3, gt=0, description=
            "像素档，0.3 适配 8G 显存（4060Ti）。**只对 H3 有效**，"
            "Agnes 固定 720P/1280x704。调大会更清晰但更慢、显存不够会失败")] = 0.3,
        seed: Annotated[int, Field(default=0, description=
            "随机种子。0=每次随机；固定同一个非 0 值 H3 可复现同一构图，"
            "调提示词时用同一 seed 反复试省时间（Agnes 仅记录，不保证复现）")] = 0,
        name: Annotated[str, Field(default="",
                                    description="成片文件名（不含扩展名），留空自动生成")] = "",
        ref_image: Annotated[str, Field(default="", description=
            "参考图绝对路径（可选）。H3 在 prompt 里用 <Picture 1> 引用它锁形象/风格；"
            "Agnes 自动切 reference 模式。文件必须存在，不存在会直接报错")] = "",
        index: Annotated[str, Field(default="none", description=
            "已停用的兼容参数，保持 none 即可")] = "none",
        root: Root = "") -> str:
    """按提示词生成视频，成片落 <项目根>/AI生成/，**不进素材库**（素材库只放原始素材）。

    两个后端可选：
    - **Agnes Video 2.5 Flash**（默认优先，云端）：1280x704、4~12 秒、**成片自带音轨**，
      通常几十秒到几分钟，需联网。免费账号在高峰可能排队，报 video_queue_full。
    - **本地 MiniMax H3**（离线兜底）：736x416、3~15 秒，12 步采样，很慢
      （10 秒片约 40 分钟），且 **GPU 同时只能跑一个**。

    backend=auto 时按 agnes → h3 降级；显式指定则只跑那一个，不达标直接报错。
    prompt：H3 用英文、Agnes 用中文；<2000 字符；相机运动 H3 可用 [slow zoom in] 标注。
    duration/aspect/seed：见各参数说明；megapixels 只对 H3 生效。
    ref_image：参考图路径（可选），H3 用 <Picture 1> 引用。

    异步：立即返回 task_id，用 frameseek_task 轮询（间隔 10 秒以上）。
    H3 每次生成都是独立子进程（模型即用即加载、跑完即退），冷启动要等模型加载。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("目标素材库目录不存在，先用 frameseek_add_library 登记",
                    "root 只是用来记任务进度，随便给一个已登记的库即可；"
                    "或省略 root 走激活库")
    backend = (backend or "auto").strip().lower()
    if backend not in ("auto", "agnes", "h3"):
        return _err(f"未知后端：{backend}（可选 agnes / h3 / auto）")
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
        if backend == "agnes" and aspect in gen_mod.AGNES_UNSUPPORTED:
            raise ValueError(
                f"Agnes 不支持宽高比 {aspect}，"
                "可选 " + ", ".join(sorted(set(gen_mod.AGNES_ASPECTS.values()))))
    except ValueError as e:
        return _err(str(e),
                    "aspect 必须是 frameseek_gen_info 的 aspects 里那个原字符串；"
                    "H3 建议改用英文、Agnes 可直接写中文，控制在 2000 字符内")
    params = {"prompt": p, "duration": float(duration), "aspect": aspect,
              "megapixels": float(megapixels), "seed": int(seed or 0),
              "name": name, "ref_image": ref_image, "index": index,
              "backend": backend}
    # 后端全部未就绪时提前报错，别建一张注定失败的空任务卡
    chain = gen_mod.BACKEND_ORDER if backend == "auto" else (backend,)
    if not any(gen_mod.backend_ready(b) for b in chain):
        st = IX.open_store(r)
        tid = st.new_task("gen", r, 100)
        st.finish_task(tid, "error", "没有就绪的视频后端")
        return _err("没有就绪的视频后端：" +
                    ", ".join(f"{b}({'就绪' if gen_mod.backend_ready(b) else '缺依赖'})"
                              for b in chain),
                    "Agnes 需要 AGNES_API_KEY 或 ~/.workbuddy/models.json 里有 Key；"
                    "H3 需要 ComfyUI 便携版 + h3gen.py")
    st = IX.open_store(r)
    tid = st.new_task("gen", r, 100)
    # 云端 agnes 不占 GPU，可以和本地 H3 并行；两个 H3 由 gen 内部的
    # _H3_LOCK 串行化，所以这里按后端拆 key，不该互相挡。
    job_key = "gen" if backend == "h3" else f"gen:{backend}"
    if not _bg(job_key, _gen_job(r, tid, params)):
        st.finish_task(tid, "error", "已有同后端生成任务在进行中")
        return _err(f"已经有一个 {backend} 生成任务在跑了，先用 frameseek_task 看进度。"
                    + ("（H3 走 GPU 独占；云端 agnes 可与它并行）"
                       if backend == "auto" else ""))
    return _ok(started=True, task_id=tid, root=r, params=params,
               eta_hint="backend=agnes 云端出片通常几十秒~几分钟（高峰会排队退避）；"
                        "backend=h3 本地子进程冷启动 + 12 步采样，"
                        "5 秒片段约 10 分钟、10 秒片约 40 分钟",
               note="用 frameseek_task 查进度")


# ---------------- 工具：白板手绘视频 ----------------
@server.tool()
def frameseek_handdraw_info() -> str:
    """白板手绘视频引擎现状：本地 CPU 逐笔渲染引擎是否就绪、缺哪些包、
    默认音色、纸张底色、成片落哪个子目录。生成手绘视频前先看这个。
    不占 GPU，可与 MiniMax H3 生成并行。"""
    return json.dumps(_hd_info(), ensure_ascii=False)


Project = Annotated[str, Field(description=
    "工程目录名（frameseek_handdraw_init 返回的 project 值），"
    "之后每一步都用它指认同一个工程")]
SceneId = Annotated[str, Field(description="幕 id（frameseek_handdraw_scenes "
                             "返回的 scene 编号，如 scene-01）")]


@server.tool()
def frameseek_handdraw_init(
        script: Annotated[str, Field(default="", description=
            "文字稿 .txt 的绝对路径。引擎按句拆成字幕并**预估**时间轴"
            "（不是配音对齐结果，后面试听配音时才会定）")] = "",
        srt: Annotated[str, Field(default="", description=
            "已有 .srt 字幕的绝对路径，给了就保留原时间轴。"
            "script / srt 至少给一个")] = "",
        name: Annotated[str, Field(default="",
                                    description="工程目录名，省略则用 title 或源文件名")] = "",
        title: Annotated[str, Field(default="",
                                    description="片名，写进 project.json")] = "",
        root: Root = "") -> str:
    """白板手绘第 1 步：用文稿（或已有 SRT）建一个手绘工程，返回工程目录名。

    工程落在 <项目根>/AI生成/手绘/<name>/，之后所有 handdraw 工具都用
    返回的 `project` 指认。同名工程已存在会报错（不覆盖）——换 name 或先删旧的。

    返回后接 frameseek_handdraw_scenes 出分幕建议，再逐幕生成线稿+标注。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("需要一个可用素材库来记录任务进度（工程本身在项目目录下）",
                    "root 只是用来记任务进度，随便给一个已登记的库；"
                    "或省略 root 走激活库")
    try:
        if not hd_mod.engine_ready():
            raise ValueError("手绘引擎未就绪，缺：" +
                             ", ".join(hd_mod.engine_info()["missing"]))
        res = hd_mod.init_project(root=r, script=script, srt=srt,
                                  title=title, name=name)
    except ValueError as e:
        return _err(str(e),
                    "script 必须是 .txt、srt 必须是 .srt，且文件得存在；"
                    "工程名重复就换 name")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["root"] = r
    res["next"] = "出分幕建议：frameseek_handdraw_scenes"
    return _ok(**res)


@server.tool()
def frameseek_handdraw_scenes(
        project: Project,
        target_sec: Annotated[int, Field(default=30, ge=10, le=120, description=
            "每幕目标秒数（默认 30，建议 25~35）。纯按字幕条数均分，"
            "定完可以自己再调")] = 30,
        root: Root = "") -> str:
    """白板手绘第 2 步：读工程字幕，给分幕建议（纯本地，不生图、不花钱）。

    返回字幕条（index/startMs/endMs/text）和分幕建议
    （sceneIndex/cueRange 覆盖的字幕号/suggested 时长），用来定分幕策略。
    纯解析，下一步要自己准备每一幕的线稿图。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）",
                                   hint="用 frameseek_list_libraries 取一个库 的 path 传进来，或省略 root 走激活库")
    try:
        res = hd_mod.suggest_scenes(root=r, project=project,
                                    target_sec=int(target_sec))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["project"] = project
    res["root"] = r
    return _ok(**res)


@server.tool()
def frameseek_handdraw_annotate(
        project: Project,
        scene_id: SceneId,
        image: Annotated[str, Field(description=
            "这一幕的源线稿 png **绝对路径**：暖米黄纸底、无文字、留白充足，"
            "且文件名与你要传的 annotation 内容对应")],
        annotation: Annotated[str, Field(description=
            "标注 JSON **字符串**（不是对象，要自己 JSON.stringify）。"
            "完整 schema + 可直接改用的示例见资源 frameseek://schema/handdraw-annotation；"
            "必填 canvas{width,height}=源图像素 与 elements[]，"
            "每个 element 含 sequence / narrativeRole / region{x,y,width,height} / "
            "reveal{startMs,durationMs,direction,protectedRegions} / handPath。"
            "**sequence 与 reveal.startMs 必须按字幕叙事顺序，不是从左到右**")],
        root: Root = "") -> str:
    """白板手绘第 3 步：给某一幕写「区域标注」，**渲染的硬前置**（不写就没法渲）。

    engine 会校验：canvas 必须等于源图像素宽高、region 必须在画布内、
    sequence 顺序与 startMs 单调递增。不对会直接报错，什么都不会写。
    image 与 annotation 自动拷进工程 scenes/ 并登记进 project.json；
    同名 scene_id 再调会覆盖（方便重标）。

    标注写完先用 frameseek_handdraw_region_preview 出编号检查图核对一遍，
    确认「分区编号顺序 == 字幕叙事顺序」再渲染，否则时序会乱。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）",
                                   hint="用 frameseek_list_libraries 取一个库 的 path 传进来，或省略 root 走激活库")
    ann = None
    try:
        ann = json.loads(annotation) if isinstance(annotation, str) else annotation
    except Exception as e:
        return _err(f"annotation 不是合法 JSON：{e}",
                    "它必须是**字符串化**的 JSON 对象。"
                    "先读资源 frameseek://schema/handdraw-annotation 拿模板再填")
    try:
        res = hd_mod.write_annotation(root=r, project=project,
                                      scene_id=scene_id, image=image,
                                      annotation=ann)
    except ValueError as e:
        return _err(str(e),
                    "对照资源 frameseek://schema/handdraw-annotation 检查："
                    "canvas 宽高是否等于源图像素、region 是否越界、"
                    "sceneId 是否填了、sequence/startMs 是否按叙事顺序递增")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["project"] = project
    res["next"] = ("核对分区：frameseek_handdraw_region_preview → "
                   "渲染：frameseek_handdraw_render")
    return _ok(**res)


@server.tool()
def frameseek_handdraw_region_preview(project: Project, scene_id: SceneId,
                                      root: Root = "") -> str:
    """白板手绘第 4 步：出某幕的编号/方向检查图，核对分区与叙事顺序一致、
    区域都在画布内。渲染前建议先跑一次（纯本地，很快）。"""
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）",
                                   hint="用 frameseek_list_libraries 取一个库 的 path 传进来，或省略 root 走激活库")
    try:
        res = hd_mod.region_preview(root=r, project=project, scene_id=scene_id)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["project"] = project
    return _ok(**res)


@server.tool()
def frameseek_handdraw_render(
        project: Project,
        scene_id: SceneId,
        preview_ms: Annotated[int, Field(default=0, ge=0, le=60000, description=
            "只渲前几毫秒 = 快速试笔迹效果（调标注时用，省时间）；"
            "0 = 渲完整一幕")] = 0,
        root: Root = "") -> str:
    """白板手绘第 5 步：渲染单幕手绘 mp4（本地 CPU 逐笔流式渲染，不占 GPU）。

    **前置**：这一幕必须已经写过标注（frameseek_handdraw_annotate），否则渲不了。
    调标注阶段建议 preview_ms=3000 快速试笔迹，定稿再渲完整。
    异步：立即返回 task_id，用 frameseek_task 轮询（间隔 10 秒以上），
    成片在工程 output/ 下。同一幕不能同时渲两个。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）",
                                   hint="用 frameseek_list_libraries 取一个库 的 path 传进来，或省略 root 走激活库")
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
def frameseek_handdraw_voice(
        text: Annotated[str, Field(description=
            "试听文案，10~20 秒最合适，最好含数字和问句（最能听出色色的区别）")],
        voices: Annotated[Optional[List[str]], Field(default=None, description=
            "音色列表，如 ['zh-CN-XiaoxiaoNeural','zh-CN-YunxiNeural']；"
            "省略用默认 zh-CN-XiaoxiaoNeural（晓晓）。给多个会各自输出一份试听文件")] = None,
        allow_network: Annotated[bool, Field(default=False, description=
            "**必须显式传 true**：这是 Edge TTS 在线服务，会把 text 发到微软服务器。"
            "其余所有工具都是全本地的，只有这一个要联网")] = False,
        project: Annotated[str, Field(default="",
                                       description="工程名（可选）：给了就存进该工程，"
                                                   "省得混流时找不到")] = "",
        root: Root = "") -> str:
    """白板手绘第 6 步：配音音色试听（Edge TTS，免账号、在线合成）。

    这是**唯一联网**的工具——正式混流（第 7 步）内部也走它。
    用户还没同意联网时不要调这个，先说明会把文案发到微软的在线服务。
    """
    if not allow_network:
        return _err("配音是联网合成，需 allow_network=true 显式许可",
                    "这一步会把试听文案发到微软 Edge TTS 在线服务。"
                    "先跟用户确认愿意联网，再传 allow_network=true 重调")
    r = pick_root(root) if (root or project) else None
    try:
        res = hd_mod.audition_voice(text=text, voices=voices,
                                    allow_network=True, root=r or "",
                                    project=project)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    return _ok(**res)


@server.tool()
def frameseek_handdraw_mix(
        project: Project,
        index: Annotated[str, Field(default="none",
                                    description="已停用的兼容参数，保持 none")] = "none",
        root: Root = "") -> str:
    """白板手绘第 7 步：配音 + 伴奏混流，合成最终 mp4。**这是终态，出片在这里。**

    需要工程时间轴已定（先试听配音再 mix）。
    成片落工程 output/，同时另存一份到 <项目根>/AI生成/<工程名>.mp4。
    不进素材库索引（index 参数已停用）。
    异步：立即返回 task_id，用 frameseek_task 轮询（间隔 10 秒以上）。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）",
                                   hint="用 frameseek_list_libraries 取一个库 的 path 传进来，或省略 root 走激活库")
    try:
        hd_mod.project_json(r, project)
    except Exception as e:
        return _err(str(e),
                    "工程名不对。用 frameseek_handdraw_init 返回的 project 值")
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
def frameseek_handdraw_qa(project: Project, root: Root = "") -> str:
    """白板手绘第 8 步：质检——帧数 / 时长 / 音画长度一致性（纯读工程文件）。"""
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库（仅用于记录任务进度，工程不在库里）",
                                   hint="用 frameseek_list_libraries 取一个库 的 path 传进来，或省略 root 走激活库")
    try:
        res = hd_mod.qa(root=r, project=project)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    res["project"] = project
    return _ok(**res)


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
    g = _ai_guard(r)
    if g:
        return _err(g)
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
    g = _ai_guard(r)
    if g:
        return _err(g)
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
        root: Root = "") -> str:
    """导出台词前后各 N 秒的片段，返回 mp4 的绝对路径（可直接给播放器/剪辑软件）。

    line_id 从 frameseek_search / frameseek_get_lines 拿，**root 用结果里的 root**
    （多库时不能省：line_id 只在它自己的库内唯一，跨库会撞号）。
    成片落在 `<root>/.frameseek/clips/`，文件名带影片名和起始秒，便于人工辨认。
    """
    r = pick_root(root)
    if not r or not os.path.isdir(r):
        return _err("未指定素材库，或目录已不存在",
                    "用 frameseek_list_libraries 确认路径，"
                    "或直接复用 frameseek_search 结果里的 root 字段")
    g = _ai_guard(r)
    if g:
        return _err(g)
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

        srt_path = ""
        burn, sub_note = media.decide_burn(
            subtitle, v.get("line_source") or ln.get("src") or "")
        if sub_note:
            sub_notes.append(sub_note)
        if burn:
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
                                 srt=srt_path or None):
            return _err(f"导出失败（ffmpeg？）台词 {lid}",
                        "看 stderr 里 ffmpeg 的报错；常见是磁盘满或源文件损坏")
        clips.append({"line_id": lid, "path": dst, "video": v["name"],
                      "text": ln.get("text", ""), "start": round(s, 2),
                      "end": round(e, 2), "subtitle_burned": bool(srt_path),
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
                 description="所有库当前正在跑的扫描/识别/生成/渲染任务及进度。"
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


@server.resource("frameseek://images/backends",
                 name="生图后端说明与当前就绪状态",
                 description="双后端（agnes 云端 / sd15 本地可复现）"
                             "各自的能力、参数适用性、耗时，以及**此刻**哪些就绪。"
                             "选后端前读这个，别盲目指定。",
                 mime_type="application/json")
def _res_img_backends() -> str:
    try:
        bs = img_mod.backends()
    except Exception as e:
        return _j({"ok": False, "err": f"{type(e).__name__}: {e}"})
    ready = [b["id"] for b in bs if b.get("ready")]
    return _j({
        "auto_order": list(img_mod.BACKEND_ORDER),
        "ready_now": ready,
        "out_dir": img_mod.paint_dir(),
        "backends": bs,
        "parameter_matrix": {
            "agnes": "吃 prompt(中文友好) / refs(≤3张多图合成) / ratio(定1K·2K)；"
                     "忽略 negative、steps、cfg、lora、control_strength。"
                     "**联网**，会把提示词和参考图发到云端。",
            "sd15": "吃全部参数；固定 seed 可复现同一张（重掷主力）；"
                    "refs 首张算 canny 控制图锁构图；方形模型，ratio 无效，"
                    "width/height 用 512。纯本地。",
        },
        "how_to_reroll": [
            "1. frameseek_generate_image 出图，返回 file 绝对路径",
            "2. 不满意就 frameseek_reroll_image(file=那个路径)",
            "   —— 它读同名 .json sidecar 复现全部参数，只换 seed=0（随机）",
            "3. sd15 下同 seed 出同一张，所以连掷几次能挑到想要的构图",
            "别自己重新拼 prompt 重出，那样参数容易和原来不一样",
        ],
        "sizes": "width/height 必须是 8 的倍数、256~1024；"
                 "给 ratio（如 16:9）优先于 width/height，agnes 按它出 1K/2K。",
        "pitfalls": [
            "GPU 同时只能跑一个，别并发调",
            "refs 必须是磁盘绝对路径，不是 URL 也不是素材库相对路径",
            "出图是**同步**的（ags 10~60 秒 / sd15 5~20 秒），"
            "不像视频生成那样异步轮询",
            "产物不入素材库索引，落 <项目根>/AI生成/绘画/",
        ],
    })


@server.resource("frameseek://schema/handdraw-annotation",
                 name="白板手绘区域标注 schema",
                 description="frameseek_handdraw_annotate 的 annotation 参数"
                             "完整字段说明 + 可直接改用的示例。"
                             "写标注前必须读这个。",
                 mime_type="application/json")
def _res_annotation_schema() -> str:
    return _j({
        "how_to_use": [
            "1. 先用 frameseek_handdraw_scenes 拿字幕条和分幕建议",
            "2. 为这一幕准备一张源线稿 png（暖米黄纸底、无文字、留白充足）",
            "3. 读本 schema，照着下面的示例写 annotation JSON",
            "4. **序列按字幕叙事顺序排**，不是按画面从左到右",
            "5. canvas.width/height 必须等于源图的真实像素（可用 PIL 读）",
            "6. 调完用 frameseek_handdraw_region_preview 核对，渲染前 preview_ms=3000 试笔迹",
        ],
        "top_level_fields": {
            "sceneId": "必填，幕 id，与工具参数 scene_id 一致",
            "canvas": "必填 {width,height} = 源图像素；引擎会校验 region 是否越界",
            "storyBasis": "选填，一句话写清这一幕画的是什么（给人看的）",
            "sceneDurationMs": "选填但建议给，这一幕的总时长（毫秒）",
            "elements": "必填，数组，按 sequence 升序",
        },
        "element_fields": {
            "id": "选填，元素标识，英文短名",
            "label": "选填，中文名（检查图上会标这个）",
            "sequence": "必填，1..N，**叙事顺序**。比如先铺垫再冲突，不能按画面左右排",
            "narrativeRole": "必填，这一块的叙事作用，如 场景铺垫/动作冲突/反应结果/重点强调",
            "subtitle": "选填，对应的字幕原文",
            "type": "选填 structure / character / object / chart 等",
            "region": "必填 {x,y,width,height}，左上角原点在画布左上角",
            "reveal": "必填 {startMs,durationMs,direction,protectedRegions}",
            "handPath": "选填 {start:[x,y],end:[x,y],easing}，笔迹起止点，"
                        "默认沿 region 的竖直中线从上到下",
        },
        "reveal_fields": {
            "startMs": "相对这一幕开头的毫秒，必须与 sequence 顺序一致地递增",
            "durationMs": "这一块画完用多久（笔迹动画时长）",
            "direction": "top_to_bottom / left_to_right / bottom_to_top / right_to_left",
            "maskPaddingPx": "选填，笔迹边缘留白，默认 22",
            "protectedRegions": "选填数组，别被笔迹覆盖的区域（已画好的内容）",
        },
        "example": {
            "sceneId": "scene-01",
            "canvas": {"width": 1672, "height": 941},
            "storyBasis": "示例：三点式对比",
            "sceneDurationMs": 8600,
            "elements": [
                {"id": "left", "label": "左侧场景", "sequence": 1,
                 "narrativeRole": "场景铺垫", "subtitle": "这里是铺垫的台词。",
                 "type": "structure",
                 "region": {"x": 20, "y": 120, "width": 540, "height": 780},
                 "reveal": {"direction": "top_to_bottom", "startMs": 300,
                            "durationMs": 2600, "maskPaddingPx": 22,
                            "protectedRegions": []},
                 "handPath": {"start": [290, 130], "end": [290, 890],
                              "easing": "easeInOut"}},
                {"id": "center", "label": "中间主体", "sequence": 2,
                 "narrativeRole": "动作冲突", "subtitle": "这里是冲突的台词。",
                 "type": "character",
                 "region": {"x": 560, "y": 100, "width": 560, "height": 800},
                 "reveal": {"direction": "top_to_bottom", "startMs": 3000,
                            "durationMs": 2600, "maskPaddingPx": 22,
                            "protectedRegions": []},
                 "handPath": {"start": [840, 110], "end": [840, 890],
                              "easing": "easeInOut"}},
                {"id": "right", "label": "右侧反应", "sequence": 3,
                 "narrativeRole": "反应结果", "subtitle": "这里是结果的台词。",
                 "type": "character",
                 "region": {"x": 1120, "y": 140, "width": 532, "height": 760},
                 "reveal": {"direction": "top_to_bottom", "startMs": 5700,
                            "durationMs": 2400, "maskPaddingPx": 22,
                            "protectedRegions": []},
                 "handPath": {"start": [1380, 150], "end": [1380, 880],
                              "easing": "easeInOut"}},
            ],
        },
        "common_mistakes": [
            "sequence 按画面从左到右排 → 笔迹时序和字幕对不上，播出来很怪",
            "canvas 尺寸填错（用了缩放后的值）→ region 越界，直接被拒",
            "startMs 忘了递增或与 sequence 冲突 → 渲染崩",
            "region 之间重叠 → 后一个的笔迹会划穿前一个，该用 protectedRegions 挡",
        ],
        "full_doc": "更细的视觉规范见 skill srt-whiteboard-animation 的 "
                    "references/annotation-format.md 与 visual-spec.md",
    })


@server.resource("frameseek://tool-catalog",
                 name="工具速查表",
                 description="按「我想干什么」组织的工具索引——"
                             "比读 33 个 tools/list 的描述快得多。",
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
            "「生成 AI 视频」": "frameseek_gen_info → frameseek_generate_video（异步）",
            "「做白板手绘视频」": "手绘管线 8 步，见下「白板手绘」",
        },
        "三条铁律": [
            "1. 多库场景 root 必须带：video_id / line_id 只在各自库内唯一，跨库撞号",
            "2. 异步工具立即返回不等待，之后用 frameseek_task 轮询，间隔 5~10 秒",
            "3. 唯一联网的工具是 frameseek_handdraw_voice，必须显式 allow_network=true",
        ],
        "白板手绘管线": [
            "1 frameseek_handdraw_info 看引擎",
            "2 frameseek_handdraw_init 用文稿/字幕建工程",
            "3 frameseek_handdraw_scenes 出分幕建议",
            "4 准备每幕线稿 png",
            "5 frameseek_handdraw_annotate 写区域标注（硬前置，先读资源 frameseek://schema/handdraw-annotation）",
            "6 frameseek_handdraw_region_preview 核对分区与叙事顺序",
            "7 frameseek_handdraw_render 逐幕渲染（异步，preview_ms 先试笔迹）",
            "8 frameseek_handdraw_voice 试听配音（联网）→ frameseek_handdraw_mix 合成",
            "9 frameseek_handdraw_qa 质检",
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
