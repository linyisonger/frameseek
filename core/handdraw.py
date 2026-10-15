# -*- coding: utf-8 -*-
"""白板手绘视频引擎（包 whiteboard-video-studio 的 studio.py CLI）。

跟 gen.py 同样的思路：不另起服务，直接把已装 skill 的命令行包起来。
底层引擎是 ~/.workbuddy/skills/whiteboard-video-studio，它自带笔迹渲染
（本地 CPU、OpenCV + PyAV）、Edge TTS 配音、本地伴奏合成。

管线是**分步**的，不是"一句话出片"——因为逐幕的区域标注（哪块先画、画多久）
需要人/AI确认，盲目全自动容易出废片。对外暴露：

    engine_info() / engine_ready()   引擎现状（依赖齐没齐）
    init_project()      文稿或 SRT → 工程目录 + 预估字幕
    suggest_scenes()    字幕 → 分幕建议（纯本地，不调生图）
    write_annotation()  写某一幕的区域标注 JSON（渲染必需）
    render_scene()      单幕渲染 mp4
    audition_voice()    音色试听（Edge TTS，需联网许可）
    mix()               配音 + 伴奏混流合成最终 mp4
    qa()                质检：帧数 / 时长 / 音画长度

工程目录落在 <项目根>/手绘工程/<项目名>/，成片落 <项目根>/AI生成/（与 h3gen 同目录）。
2026-10-15 起两者都搬出素材库：素材库只放用户自己的原始素材，生成物单独一档。
注意 root 参数仅为兼容旧调用保留，已不参与路径定位。

注意：studio.py 输出 JSON 到 stdout、进度日志到 stderr，本模块把 stdout 原样
解析成 dict 返回；长任务（渲染/混流）由调用方套 frameseek_task 轮询。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKILLS_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")
STUDIO_SKILL = os.path.join(SKILLS_DIR, "whiteboard-video-studio")
STUDIO_PY = os.path.join(STUDIO_SKILL, "scripts", "studio.py")
# 引擎内部自建的隔离解释器（prepare_env.py 建的），没建就退回系统 python
STUDIO_VENV_PY = os.path.join(STUDIO_SKILL, ".venv", "Scripts", "python.exe")

PROJECT_ROOT_NAME = "手绘工程"   # <项目根>/手绘工程/<项目名>/
# 成片目录名：跟 gen.py 的产物同目录（2026-10-15 从「生成」改名「AI生成」）。
# 实际落点走 gen.output_dir()，这里只留给 info 展示用。
OUT_DIR_NAME = "AI生成"
CANVAS = "#F5EBD7"               # 暖米黄纸张底
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# studio.py 的子命令
COMMANDS = ("init", "check", "render", "region-preview",
            "audition", "voices", "mix", "qa", "list-voices")

# 渲染/配音要用的 Python 包（只查不装，缺了让 engine_info 报出来）
REQUIRED_MODULES = ("cv2", "numpy", "av", "PIL")


def _log(msg: str):
    """只写 stderr——MCP stdio 的 stdout 归 JSON-RPC 协议，不能污染。"""
    print(f"[frameseek-handdraw] {msg}", file=sys.stderr)


# ---------------- 引擎现状 ----------------
def _interpreter() -> str:
    """优先用引擎自带的 .venv（opencv/PyAV 装在那），否则退回当前 python。"""
    if os.path.isfile(STUDIO_VENV_PY):
        return STUDIO_VENV_PY
    return sys.executable


def _missing_modules(py: str) -> list:
    """探测引擎依赖（一次性，纯导入检查）。"""
    if not py:
        return list(REQUIRED_MODULES)
    code = ("import importlib,json,sys\n"
            "miss=[m for m in %r if not _ok(m)]\n" % (REQUIRED_MODULES,))
    code = ("import importlib\n"
            "def _ok(m):\n"
            "    try:\n"
            "        importlib.import_module(m); return True\n"
            "    except Exception:\n"
            "        return False\n"
            "miss=[m for m in %r if not _ok(m)]\n" % (list(REQUIRED_MODULES),)) \
        + "import json;print(json.dumps(miss))\n"
    try:
        out = subprocess.run([py, "-c", code], capture_output=True, text=True,
                             timeout=90)
        line = (out.stdout or "").strip().splitlines()
        for ln in reversed(line):
            ln = ln.strip()
            if ln.startswith("["):
                return json.loads(ln)
    except Exception as e:
        _log(f"依赖探测失败: {type(e).__name__}: {e}")
    return list(REQUIRED_MODULES)  # 探测不了就当全缺，让调用方去装


def engine_info() -> dict:
    """引擎现状：脚本在不在、Python 解释器、缺哪些依赖、工程根目录。

    .venv 不是硬门槛——引擎的依赖（cv2/numpy/av/PIL）只要当前解释器有就
    能跑；.venv 只是 skill 自己推荐的隔离环境。有依赖但没 .venv 时仍算就绪。
    """
    missing = []
    if not os.path.isfile(STUDIO_PY):
        missing.append("whiteboard-video-studio skill 未安装")
        return {"engine": "whiteboard-video-studio",
                "note": "本地 CPU 渲染，逐幕流式笔迹；分步工程，非一句话出片",
                "ready": False, "missing": missing,
                "interpreter": "", "script": STUDIO_PY,
                "skill_dir": STUDIO_SKILL,
                "project_root_name": PROJECT_ROOT_NAME, "canvas": CANVAS,
                "project_root_dir": os.path.join(BASE, PROJECT_ROOT_NAME),
                "out_dir": os.path.join(BASE, OUT_DIR_NAME),
                "default_voice": DEFAULT_VOICE, "ffmpeg": shutil.which("ffmpeg") or "",
                "missing_modules": list(REQUIRED_MODULES)}
    py = _interpreter()
    deps = _missing_modules(py)
    if deps:
        missing.append("缺 Python 包：" + ", ".join(deps))
    return {
        "engine": "whiteboard-video-studio",
        "note": "本地 CPU 渲染，逐幕流式笔迹；分步工程，非一句话出片",
        "ready": not missing,
        "missing": missing,
        "interpreter": py,
        "script": STUDIO_PY,
        "skill_dir": STUDIO_SKILL,
        "project_root_name": PROJECT_ROOT_NAME,
        "project_root_dir": os.path.join(BASE, PROJECT_ROOT_NAME),
        "out_dir": os.path.join(BASE, OUT_DIR_NAME),
        "canvas": CANVAS,
        "default_voice": DEFAULT_VOICE,
        "ffmpeg": shutil.which("ffmpeg") or "",
        "missing_modules": deps,
        "venv": os.path.isfile(STUDIO_VENV_PY),
    }


def engine_ready() -> bool:
    return bool(engine_info().get("ready"))


# ---------------- 工程路径 ----------------
def safe_name(name: str) -> str:
    """目录名安全化：去路径、去非法字符、限长。"""
    n = os.path.basename((name or "").strip().strip("\"'"))
    n = re.sub(r"[\\/:*?\"<>|]+", "_", n).strip(" .")
    return n[:60] or "handdraw"


def project_root(root: str = "") -> str:
    """<项目根>/手绘工程/，建好返回绝对路径。

    2026-10-15 改：工程整体搬出素材库，落在 FrameSeek 项目目录下——
    素材库只留用户自己的原始素材。root 参数保留只为兼容旧调用，不再用于定位。
    """
    d = os.path.join(BASE, PROJECT_ROOT_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def project_dir(root: str, project: str) -> str:
    """<项目根>/手绘工程/<项目名>/，不存在则报错。"""
    p = os.path.join(project_root(root), safe_name(project))
    if not os.path.isdir(p):
        raise ValueError(f"手绘工程不存在：{p}")
    return p


def project_json(root: str, project: str) -> str:
    """工程清单 project.json 的绝对路径。"""
    p = os.path.join(project_dir(root, project), "project.json")
    if not os.path.isfile(p):
        raise ValueError(f"工程清单不存在：{p}（先跑 init）")
    return p


def dest_path(root: str = "", name: str = "") -> str:
    """成片落点：<项目根>/AI生成/（与 H3 成片同目录），同名自动递增。

    2026-10-15 改：不再落素材库。root 参数保留只为兼容旧调用。
    落点跟 gen.output_dir() 走同一处（尊重 hub 的 gen_outdir 设置）。
    """
    from core import gen as _gen
    d = _gen.output_dir()
    os.makedirs(d, exist_ok=True)
    fn = os.path.basename((name or "").strip()) or time.strftime("手绘_%Y%m%d_%H%M%S")
    fn = re.sub(r'[\\/:*?"<>|]+', "_", fn).strip(" .")[:80]
    if not fn.lower().endswith(".mp4"):
        fn += ".mp4"
    p = os.path.join(d, fn)
    i = 1
    while os.path.exists(p):
        p = os.path.join(d, f"{fn[:-4]}_{i}.mp4")
        i += 1
    return p


# ---------------- 调 studio.py ----------------
def _run(args: list, timeout: int = 3600) -> dict:
    """调 studio.py 一个子命令，返回结果 dict。

    studio.py 的 stdout 有两种形态：
    - check/init/region-preview/mix 等：单个 pretty JSON 对象；
    - render：进度行 `scene-01: 5.0/24.0s` + 末行 `OUTPUT=<路径>`。
    两种都要认；stderr 转发到我们自己的 stderr（排错用，不污染协议通道）。
    """
    py = _interpreter()
    if not os.path.isfile(STUDIO_PY):
        raise RuntimeError("whiteboard-video-studio 未安装")
    cmd = [py, "-u", STUDIO_PY] + [str(a) for a in args]
    _log("studio.py " + " ".join(str(a) for a in args[:6]) + " ...")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.stderr:
        for ln in proc.stderr.strip().splitlines()[-6:]:
            _log("  " + ln)
    out = (proc.stdout or "").strip()
    if not out:
        # 失败时 stdout 可能为空，真正的错因在 stderr（已转发），这里给个可读提示
        tail = " / ".join(proc.stderr.strip().splitlines()[-3:]) if proc.stderr else ""
        raise RuntimeError(f"studio.py {' '.join(str(a) for a in args[:3])} "
                           f"失败（exit {proc.returncode}）{tail}")

    # 形态 1：末尾有 OUTPUT=<路径> 的进度流
    m_out = re.search(r"^OUTPUT=(.+)$", out, re.M)
    if m_out and not out.lstrip().startswith("{"):
        result = {"output": m_out.group(1).strip().strip('"')}
        prog = re.findall(r"([\w.-]+):\s*([\d.]+)/([\d.]+)s", out)
        if prog:
            result["progress"] = [{"scene": a, "at_s": float(b), "total_s": float(c)}
                                  for a, b, c in prog[-3:]]
        return result

    # 形态 1b：末行是带引号的绝对路径（部分 studio.py 版本不带 OUTPUT= 前缀）
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if lines and not out.lstrip().startswith("{"):
        last = lines[-1]
        cand = last.strip('"')
        if (last.startswith('"') and last.endswith('"')) or \
                (os.path.isabs(cand) and os.path.splitext(cand)[1].lower() in
                 (".mp4", ".wav", ".mp3", ".png")):
            result = {"output": cand}
            prog = re.findall(r"([\w.-]+):\s*([\d.]+)/([\d.]+)s", out)
            if prog:
                result["progress"] = [{"scene": a, "at_s": float(b), "total_s": float(c)}
                                      for a, b, c in prog[-3:]]
            return result

    # 形态 2：pretty JSON（可能有进度行混在前面）
    for chunk in reversed(_json_blocks(out)):
        try:
            parsed = json.loads(chunk)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except Exception:
            continue
    if m_out:  # 前面有杂行但末尾有 OUTPUT
        return {"output": m_out.group(1).strip().strip('"')}
    raise RuntimeError("studio.py 输出无法解析：" + out[:200])


def _json_blocks(text: str) -> list:
    """把 stdout 切成候选 JSON 块（完整对象/数组优先）。"""
    blocks = []
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "]}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append(text[start:i + 1])
                    start = None
    if not blocks and text.strip():
        blocks.append(text.strip())
    return blocks


# ---------------- 分步管线 ----------------
def init_project(root: str, script: str = "", srt: str = "",
                 title: str = "", name: str = "") -> dict:
    """第 1 步：文稿（或已有 SRT）→ 工程目录 + 预估字幕 + project.json。

    script：文字稿路径（.txt）；引擎按句拆条估时间轴（标注 timelineStatus=estimated）。
    srt：已有字幕路径（.srt）；保留原时间轴（timelineStatus=needs-review）。
    两者给一个就行。name：工程目录名，省略时用标题或时间戳。
    """
    root = os.path.abspath(root) if root else ""
    src = (srt or script or "").strip()
    if not src:
        raise ValueError("script 或 srt 至少给一个")
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise ValueError(f"文稿/字幕文件不存在：{src}")
    if script and not srt and not src.lower().endswith(".txt"):
        raise ValueError("script 只能是 .txt 文字稿；已有字幕请用 srt 参数")
    if srt and not src.lower().endswith(".srt"):
        raise ValueError("srt 只能是 .srt 字幕文件")

    pname = safe_name(name or title or os.path.splitext(os.path.basename(src))[0])
    dest = os.path.join(project_root(root), pname)
    if os.path.exists(dest):
        raise ValueError(f"工程目录已存在：{dest}（换 name 或删掉旧的；不覆盖）")

    args = ["init", dest, "--title", title or pname]
    args += ["--srt", src] if srt else ["--text", src]
    res = _run(args, timeout=120)
    res["project"] = pname
    res["project_dir"] = dest
    res["source"] = src
    return res


def suggest_scenes(root: str, project: str,
                   target_sec: int = 30) -> dict:
    """第 2 步（辅助）：字幕 → 分幕建议，只给节奏不调生图。

    这是 srt-whiteboard-animation 的 parse_srt.py 能力（纯本地解析字幕，
    按 25~35 秒/幕切分并给 sceneDurationMs），用来帮定分幕策略。
    """
    root = os.path.abspath(root)
    srt = os.path.join(project_dir(root, project), "captions-v1.srt")
    if not os.path.isfile(srt):
        raise ValueError(f"工程字幕不存在：{srt}（先跑 init）")
    script = os.path.join(SKILLS_DIR, "srt-whiteboard-animation",
                          "scripts", "parse_srt.py")
    if not os.path.isfile(script):
        raise RuntimeError("srt-whiteboard-animation skill 未安装（分幕建议功能）")
    py = _interpreter()
    proc = subprocess.run(
        [py, "-u", script, srt, "--target-sec", str(int(target_sec)),
         "--min-sec", "25", "--max-sec", "35"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    if proc.stderr:
        for ln in proc.stderr.strip().splitlines()[-6:]:
            _log("  " + ln)
    out = (proc.stdout or "").strip()
    for chunk in reversed(_json_blocks(out)):
        try:
            return json.loads(chunk)
        except Exception:
            continue
    raise RuntimeError("分幕建议解析失败")


def _validate_annotation(ann: dict, cw: int, ch: int, fps: int) -> None:
    """前置校验标注，尽量在渲染前把错误说清楚（否则引擎只会抛 KeyError/traceback）。

    复刻 skill 的 check_project 规则：
    - sourceStartMs 必须等于本幕之前的累计时长（多幕必须从 0 连续衔接）
    - sceneDurationMs 要落在帧边界、> 800ms
    - canvas 必须等于源图像素
    - sequence 唯一且与 startMs 顺序一致（一支笔不能并行作画）
    - 每个 region 在画布内、startMs 间隔足够（至少 3 帧）
    - 最后至少留 500ms 完整画面
    """
    from PIL import Image  # noqa: F401  (仅确认可用)

    dur = ann.get("sceneDurationMs")
    if not isinstance(dur, (int, float)) or dur <= 800:
        raise ValueError(f"sceneDurationMs 必须 > 800ms（当前 {dur}）")
    if abs(dur * fps / 1000 - round(dur * fps / 1000)) >= 0.001:
        raise ValueError(f"sceneDurationMs={dur} 不落在 {fps}fps 的帧边界上")
    if "sourceStartMs" not in ann:
        raise ValueError("annotation 缺 sourceStartMs（本幕在全片时间轴上的起点，"
                         "多幕必须从 0 连续衔接：第一幕 0，之后 = 前面各幕时长累加）")
    if int(ann["sourceStartMs"]) < 0:
        raise ValueError("sourceStartMs 不能为负")

    canvas = ann.get("canvas") or {}
    if int(canvas.get("width") or 0) != cw or int(canvas.get("height") or 0) != ch:
        raise ValueError(
            f"canvas {canvas.get('width')}x{canvas.get('height')} 与源图 "
            f"{cw}x{ch} 不符（必须等于源图像素尺寸）")

    def valid_rect(rect):
        if not isinstance(rect, dict):
            return False
        try:
            x, y, w, h = (rect[k] for k in ("x", "y", "width", "height"))
        except KeyError:
            return False
        return 0 <= x < x + w <= cw and 0 <= y < y + h <= ch

    seqs, end = set(), 0
    prev_seq = -1
    for item in sorted(ann.get("elements") or [],
                       key=lambda e: (e.get("reveal") or {}).get("startMs") or 0):
        rv = item.get("reveal") or {}
        seq = item.get("sequence")
        start = rv.get("startMs")
        d_ms = rv.get("durationMs")
        if seq is None or seq in seqs or seq <= prev_seq:
            raise ValueError("sequence 必须唯一且与作画时间顺序一致"
                             f"（当前 {seq}，前一个 {prev_seq}）")
        if not valid_rect(item.get("region")):
            raise ValueError(f"区域超出源图或格式错：{item.get('region')}")
        if start is None or d_ms is None:
            raise ValueError(f"元素 {item.get('id') or seq} 缺 reveal.startMs/durationMs")
        if start < end:
            raise ValueError(f"元素 {item.get('id') or seq} startMs={start} 与上一元素"
                             f"结束 {end} 重叠（一支笔不能并行作画）")
        if d_ms * fps / 1000 < 3:
            raise ValueError(f"元素 {item.get('id') or seq} 作画时长 {d_ms}ms "
                             f"不足 3 帧")
        for pr in rv.get("protectedRegions") or []:
            if not valid_rect(pr):
                raise ValueError(f"保护区域越界：{pr}")
        seqs.add(seq)
        prev_seq = seq
        end = start + d_ms
    if end > dur - 500:
        raise ValueError(f"最后一个元素结束于 {end}ms，超出「结尾留 500ms 完整画面」"
                         f"（sceneDurationMs={dur}）；调大时长或缩短作画时间")


def write_annotation(root: str, project: str, scene_id: str,
                     image: str, annotation: dict,
                     source_start_ms: int = None) -> dict:
    """第 3 步：写某一幕的区域标注 JSON，并把它登记进 project.json 的 scenes。

    image：源线稿 png 的绝对路径（或相对工程目录）。
    annotation：引擎的标注 dict（canvas/elements/sceneDurationMs/sourceStartMs 等），
    完整 schema 见 skill 的 SKILL.md。
    source_start_ms：省略时从 project.json 里已有的 scenes 累计推断
    （按 sequence/id 顺序累加各幕 sceneDurationMs）。
    引擎要 image + annotation 成对同名，缺一渲染不了。
    """
    root = os.path.abspath(root)
    pdir = project_dir(root, project)
    pj = project_json(root, project)

    if not isinstance(annotation, dict) or not annotation.get("elements"):
        raise ValueError("annotation 至少要有 elements 列表")
    if not annotation.get("canvas"):
        raise ValueError("annotation 缺 canvas（必须等于源图像素宽高）")
    if not str(annotation.get("sceneId") or "").strip():
        raise ValueError("annotation 缺 sceneId")

    sid = safe_name(scene_id)
    # 线稿图放到工程 scenes/ 下，和标注同名
    img_path = os.path.abspath(image)
    if not os.path.isfile(img_path):
        raise ValueError(f"源线稿不存在：{img_path}")
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            cw, ch = im.size
    except Exception as e:
        raise ValueError(f"读不了源线稿：{type(e).__name__}: {e}")
    with open(pj, encoding="utf-8") as f:
        proj = json.load(f)
    fps = int(proj.get("fps") or 30)

    ann = dict(annotation)
    ann["sceneId"] = sid
    # sourceStartMs：显式给就用，否则按已登记幕累加推断
    if source_start_ms is None:
        acc = 0
        for s in sorted(proj.get("scenes", []), key=lambda s: str(s.get("id") or "")):
            if str(s.get("id")) == sid:
                continue
            try:
                with open(resolve_rel(pdir, s["annotation"]), encoding="utf-8") as fh:
                    acc += int(json.load(fh).get("sceneDurationMs") or 0)
            except Exception:
                pass
        source_start_ms = acc
    ann["sourceStartMs"] = int(source_start_ms)

    _validate_annotation(ann, cw, ch, fps)

    scenes_dir = os.path.join(pdir, "scenes")
    os.makedirs(scenes_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(img_path))[0]
    dst_img = os.path.join(scenes_dir, f"{stem}.png")
    if os.path.abspath(img_path) != os.path.abspath(dst_img):
        shutil.copy2(img_path, dst_img)

    ann_path = os.path.join(scenes_dir, f"{stem}.annotation.json")
    with open(ann_path, "w", encoding="utf-8") as f:
        json.dump(ann, f, ensure_ascii=False, indent=2)

    # 登记进 project.json 的 scenes（同名覆盖，便于重标）
    scenes = [s for s in proj.get("scenes", []) if s.get("id") != sid]
    entry = {"id": sid,
             "image": os.path.relpath(dst_img, pdir).replace("\\", "/"),
             "annotation": os.path.relpath(ann_path, pdir).replace("\\", "/")}
    if ann.get("overlay"):
        ov_src = ann["overlay"]
        if os.path.isfile(ov_src):
            dst_ov = os.path.join(scenes_dir, f"{stem}.overlay.json")
            shutil.copy2(ov_src, dst_ov)
            entry["overlay"] = os.path.relpath(dst_ov, pdir).replace("\\", "/")
    scenes.append(entry)
    scenes.sort(key=lambda s: str(s.get("id") or ""))
    proj["scenes"] = scenes
    with open(pj, "w", encoding="utf-8") as f:
        json.dump(proj, f, ensure_ascii=False, indent=2)

    return {"ok": True, "scene": sid, "image": dst_img,
            "annotation": ann_path, "scenes": len(scenes),
            "elements": len(ann.get("elements", [])),
            "sceneDurationMs": ann.get("sceneDurationMs"),
            "sourceStartMs": ann["sourceStartMs"],
            "timeline_position_ms": int(source_start_ms) + int(ann["sceneDurationMs"])}


def resolve_rel(base: str, rel: str) -> str:
    """把 project.json 里的相对路径还原成绝对路径。"""
    return rel if os.path.isabs(rel) else os.path.join(base, rel.replace("/", os.sep))


def region_preview(root: str, project: str, scene_id: str,
                   output: str = "") -> dict:
    """第 4 步（可选）：出编号/方向检查图，核对分区与叙事顺序。

    引擎的 new_output 不肯覆盖同名文件，所以已存在的检查图会自动加序号，
    每次重标都能再出一张对比。
    """
    pj = project_json(os.path.abspath(root), project)
    out = output or os.path.join(os.path.dirname(pj), "review",
                                 f"{safe_name(scene_id)}-regions.png")
    out = _unique_path(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    return _run(["region-preview", pj, "--scene", safe_name(scene_id),
                 "--output", out], timeout=300)


def _unique_path(path: str) -> str:
    """已存在就加 _1 / _2 序号，返回没被占用的路径。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


def render_scene(root: str, project: str, scene_id: str,
                 preview_ms: int = 0, output: str = "") -> dict:
    """第 5 步：渲染单幕手绘成片 mp4（本地 CPU，笔迹流式渲染）。

    preview_ms > 0 只渲前几毫秒（快速试笔迹），否则渲完整一幕。
    """
    root = os.path.abspath(root)
    pdir = project_dir(root, project)
    pj = project_json(root, project)
    sid = safe_name(scene_id)
    # 引擎不覆盖同名成片——重渲自动加序号，保留旧版可对比
    out = _unique_path(output or os.path.join(pdir, "output", f"{sid}.mp4"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    args = ["render", pj, "--scene", sid, "--output", out]
    if preview_ms:
        args += ["--preview-ms", str(int(preview_ms))]
    res = _run(args, timeout=3600)
    res["output"] = out
    return res


def audition_voice(text: str, voices: list = None,
                   allow_network: bool = False, root: str = "",
                   project: str = "") -> dict:
    """第 6 步：音色试听（Edge TTS 在线合成，免账号）。

    allow_network 必须为 True——会把待合成文本发到 Edge TTS 服务。
    root/project 给定时用该工程的 voice 配置，否则用默认音色。
    """
    if not allow_network:
        raise ValueError("试听/配音是联网合成，需 allow_network=True 显式许可")
    text = (text or "").strip()
    if not text:
        raise ValueError("试听文本不能为空")
    if root and project:
        pj = project_json(os.path.abspath(root), project)
    else:
        # 没工程就用最小临时工程——studio 的 audition 只读 voice 配置
        pj = _tmp_min_project()
    args = ["audition", pj, "--text", text, "--allow-network"]
    for v in (voices or [DEFAULT_VOICE]):
        args += ["--voice", v]
    return _run(args, timeout=300)


def mix(root: str, project: str, output: str = "",
        index: str = "none") -> dict:
    """第 7 步：配音 + 伴奏混流，合成最终 mp4（工程 finalPath）。

    index：保留兼容参数，现已停用——工程与成片都在项目目录下，不进素材库。
    成片落 <项目根>/AI生成/<工程名>.mp4。
    """
    pdir = project_dir(root, project)
    pj = project_json(root, project)
    res = _run(["mix", pj], timeout=3600)

    final = os.path.join(pdir, "output", "final-v1.mp4")
    if not os.path.isfile(final):
        cands = [os.path.join(pdir, "output", f) for f in os.listdir(os.path.join(pdir, "output"))
                 if f.lower().endswith(".mp4")]
        final = max(cands, key=os.path.getmtime) if cands else final

    out = {"ok": True, "file": final}
    # 成片另存一份到 <项目根>/AI生成/，方便统一取用
    if os.path.isfile(final):
        try:
            dst = dest_path(name=os.path.basename(pdir))
            shutil.copy2(final, dst)
            out["out_file"] = dst
        except Exception as e:
            _log(f"成片另存失败（不影响成片）: {e}")
    res.update(out)
    return res


def qa(root: str, project: str) -> dict:
    """第 8 步：质检——帧数 / 时长 / 音画长度一致性。"""
    return _run(["qa", project_json(os.path.abspath(root), project)],
                timeout=600)


def approve_timeline(root: str, project: str) -> dict:
    """批准时间轴：把 project.json 的 timelineStatus 置为 approved。

    引擎故意卡正式渲染：timelineStatus 不是 approved 就只允许 --preview-ms
    短预览（防止在字幕时间轴没定时渲出时序错乱的废片）。
    确认分幕时长/时间轴没问题后调一次，之后 render 才能出整幕。
    """
    pj = project_json(os.path.abspath(root), project)
    tl = _check_timeline(os.path.abspath(root), project)
    if not tl["ok"]:
        raise ValueError("时间轴还不够长：" + tl["advice"])
    with open(pj, encoding="utf-8") as f:
        proj = json.load(f)
    proj["timelineStatus"] = "approved"
    with open(pj, "w", encoding="utf-8") as f:
        json.dump(proj, f, ensure_ascii=False, indent=2)
    return {"ok": True, "project": project, "timelineStatus": "approved",
            "picture_total_ms": tl["picture_total_ms"],
            "cue_total_ms": tl["cue_total_ms"]}


def timeline_status(root: str, project: str) -> dict:
    """查工程当前的时间轴状态与体检结果。"""
    pj = project_json(os.path.abspath(root), project)
    with open(pj, encoding="utf-8") as f:
        proj = json.load(f)
    tl = _check_timeline(os.path.abspath(root), project)
    return {"ok": True, "project": project,
            "timelineStatus": proj.get("timelineStatus"),
            "scenes": len(proj.get("scenes", [])),
            **tl}


def check(root: str, project: str) -> dict:
    """校验工程配置（幕数 / 时间轴 / 标注合法性），渲染前跑一次。"""
    return _run(["check", project_json(os.path.abspath(root), project)],
                timeout=300)


def _cue_span_ms(root: str, project: str) -> int:
    """工程字幕的总时长（毫秒）。画面总时长不能小于它，否则 check 会报
    「字幕超过总画面时长」。

    SRT 是 CRLF 行尾，用正则直接扫时间行，别按行块判空。
    """
    pdir = project_dir(os.path.abspath(root), project)
    srt = os.path.join(pdir, "captions-v1.srt")
    if not os.path.isfile(srt):
        return 0
    try:
        with open(srt, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except Exception:
        return 0
    last = 0
    for m in re.finditer(
            r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*"
            r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})", text):
        g = [int(x) for x in m.groups()]
        last = max(last, ((g[4] * 60 + g[5]) * 60 + g[6]) * 1000 + g[7])
    return last


def _check_timeline(root: str, project: str) -> dict:
    """渲染前的整片时间轴体检：字幕总长 vs 画面总长、各幕衔接、字幕覆盖情况。

    引擎的 check 只在报错时说「字幕超过总画面时长」，不说差多少、该怎么补。
    这里提前算清楚并给建议。
    """
    pdir = project_dir(os.path.abspath(root), project)
    with open(project_json(os.path.abspath(root), project), encoding="utf-8") as f:
        proj = json.load(f)
    scenes, total = [], 0
    for s in sorted(proj.get("scenes", []), key=lambda x: str(x.get("id") or "")):
        d = 0
        try:
            with open(resolve_rel(pdir, s["annotation"]), encoding="utf-8") as fh:
                d = int(json.load(fh).get("sceneDurationMs") or 0)
        except Exception:
            pass
        scenes.append({"id": s.get("id"), "sceneDurationMs": d})
        total += d
    cue_ms = _cue_span_ms(os.path.abspath(root), project)
    shortfall = max(0, cue_ms - total)
    return {
        "scenes": scenes,
        "picture_total_ms": total,
        "cue_total_ms": cue_ms,
        "shortfall_ms": shortfall,
        "ok": shortfall == 0,
        "advice": (f"画面比字幕短 {shortfall/1000:.1f} 秒：把最后一幕（或某幕）的 "
                   f"sceneDurationMs 加大到合计 ≥ {cue_ms}ms 再渲染"
                   if shortfall else "时间轴够长，可渲染"),
    }


def _tmp_min_project() -> str:
    """给 audition/voices 用的一次性最小工程（用完即留在 _out）。"""
    import tempfile
    d = os.path.join(BASE, "_out", "_studio_probe")
    os.makedirs(os.path.join(d, "source"), exist_ok=True)
    os.makedirs(os.path.join(d, "scenes"), exist_ok=True)
    os.makedirs(os.path.join(d, "audio"), exist_ok=True)
    os.makedirs(os.path.join(d, "output"), exist_ok=True)
    os.makedirs(os.path.join(d, "review"), exist_ok=True)
    pj = os.path.join(d, "project.json")
    if not os.path.isfile(pj):
        proj = {"schemaVersion": 1, "title": "音色试听", "subtitlePath": "captions-v1.srt",
                "timelineStatus": "estimated", "width": 1920, "height": 1080, "fps": 30,
                "gridEdge": 10, "canvasColor": CANVAS,
                "fonts": {"caption": None, "label": None, "fallbacks": []},
                "caption": {"fontSize": 40, "minFontSize": 28, "sideMargin": 96,
                            "bandHeight": 136},
                "voice": {"name": DEFAULT_VOICE, "rate": 0, "maxRate": 30,
                          "pitch": "+0Hz", "leadMs": 70, "tailMs": 110,
                          "spokenOverrides": {},
                          "recordsPath": "audio/voice-records-v1.json"},
                "music": {"mode": "synth", "bpm": 104, "normalGain": .65,
                          "duckGain": .26},
                "scenes": [], "finalPath": "output/final-v1.mp4"}
        with open(pj, "w", encoding="utf-8") as f:
            json.dump(proj, f, ensure_ascii=False, indent=2)
    return pj