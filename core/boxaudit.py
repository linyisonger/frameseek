# -*- coding: utf-8 -*-
"""台词帧 / 台词框 / 文字 —— 把入库的方框凭证画回画面上，供人工二次复查。

为什么要有这一层
----------------
`core/verify.py` 只能给出"通过 / 不通过 + 哪一项越界"。那是**数字**，
而"框到底对不对"最终是个**眼睛判断**：框是不是套住了那行字、是不是把
上下两行（双语／折行）圈在一起、是不是圈到了画面角落的台标上去。
所以这里做一件事——**把凭证还原成画面上看得见的东西**：

    台词帧  = 那一秒真实的画面（不是截图缓存，是按需现抽的原帧）
    台词框  = 库里的方框凭证，按全画面归一化坐标画上去
    文字    = 这一条入库的文本，同时给出这一帧现场重新 OCR 出来的文本

坐标口径（这里最容易搞错，务必看清）
------------------------------------
`lines.box` 里的 x0/y0/x1/y1 是**相对 OCR 字幕带**的归一化坐标，
不是相对整个画面。字幕带是画面底部 OCR_BAND（默认 30%）那一条，
由 `core/ocr.py: _frame_stream` 的 crop 参数切出来：

    vf = crop=iw:ih*0.30:0:ih*0.70      # 带子顶边在 70% 画面高度处

所以带内坐标 → 全画面坐标的换算是：

    y_全画面 = (1 - OCR_BAND) + y_带内 * OCR_BAND
    x_全画面 = x_带内                    # 带子占满宽度，横向不用换

直接把 box 里的 y 当成全画面坐标画，框会整体上移 70% 画面高度——
看上去就是"框飘到人物脸上去了"，而实际上框是对的。这是本模块
第一个要防的错，_band_rect() 就是干这个换算的。

三个动作
--------
  ① 看     GET  /api/boxaudit/frame     画好框的台词帧（图片）
  ② 对     POST /api/boxaudit/probe    现场重抽首/中/尾三帧，逐帧比对
  ③ 补     POST /api/boxaudit/backfill 现场框确认没问题 → 回填进库

第 ③ 步是给老库准备的：早期用语音转写入库的台词本来没有画面框
（verify 归类为"不适用"），在这里人工确认一遍就能把凭证补上，
从此这条台词也能过方框校验。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core import config as C
from core import media
from core import ocr as O
from core import store as ST
from core import verify as V


# --------------------------------------------------------------------------
# 调色（与 web/style.css 同一套，图片标注和页面观感一致）
# --------------------------------------------------------------------------
COL_SAVED = (77, 157, 255)      # 库里的凭证框 —— 主蓝
COL_LIVE = (46, 194, 126)       # 现场重检的框 —— 绿
COL_LIVE_BAD = (255, 123, 114)  # 现场没对上 —— 红
COL_TEXT = (231, 236, 244)

# 现场复核用的时间点（相对台词区间的位置）
_PROBE_AT = (0.12, 0.5, 0.88)

# 标定画像缓存：一部片 calibrate 要 10 秒（20 个采样点逐个 OCR），
# 复查时一行一次标定的话翻 100 行就要 17 分钟，纯浪费。
# 同一句话的字幕位置是死的，标定一次能管整部片。
_PROF_CACHE: Dict[str, "O.SubtitleProfile"] = {}
_PROF_MAX = 8


def _vendor() -> None:
    """把 vendor 插进 sys.path（PIL 在里面）。和 core/ocr.py: _add_vendor 同一个套路。"""
    import sys
    v = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "vendor")
    if os.path.isdir(v) and v not in sys.path:
        sys.path.insert(0, v)


# --------------------------------------------------------------------------
# 坐标换算
# --------------------------------------------------------------------------
def band_rect(box: Dict[str, Any]) -> Dict[str, float]:
    """方框凭证（带内归一化坐标）→ 全画面归一化坐标。

    字幕带 = 画面底部 OCR_BAND 那一段，横向占满宽度。所以只有 y 要换算。
    没有这一步，画出来的框会整体上移约 70% 画面高度，看着像"框飞了"，
    实际上框一点没错——这是复查时最容易被误判成 bug 的地方。
    """
    if not box:
        return {}
    b = float(C.OCR_BAND)
    top = 1.0 - b

    def g(k, d=0.0):
        try:
            return float(box.get(k, d))
        except (TypeError, ValueError):
            return d

    y0, y1 = g("y0"), g("y1")
    x0, x1 = g("x0"), g("x1")
    # 有些凭证只存了中心+宽高（老数据），补出边界，否则画不出来
    if x1 <= x0:
        cx, bw = g("cx", 0.5), g("bw")
        x0, x1 = cx - bw / 2, cx + bw / 2
    if y1 <= y0:
        cy, bh = g("cy", 0.7), g("bh")
        y0, y1 = cy - bh / 2, cy + bh / 2
    fy0, fy1 = top + y0 * b, top + y1 * b
    return {"x0": x0, "y0": fy0, "x1": x1, "y1": fy1,
            "cx": (x0 + x1) / 2, "cy": (fy0 + fy1) / 2,
            "bw": x1 - x0, "bh": fy1 - fy0}


def rect_px(rect: Dict[str, float], w: int, h: int,
            pad: float = 0.0) -> Tuple[int, int, int, int]:
    """全画面归一化矩形 → 像素矩形，可选按比例外扩。

    `pad` 是往外扩的比例。检测框是**贴**着字的（RapidOCR 给的是字的
    外接框），直接画会显得"压字"，人眼容易判成框歪了。默认扩一点
    （BOX_PAD），但保持在"框住这一行、不碰邻居"的范围内。
    """
    if not rect or w <= 0 or h <= 0:
        return (0, 0, 0, 0)
    x0 = (rect["x0"] - pad) * w
    y0 = (rect["y0"] - pad) * h
    x1 = (rect["x1"] + pad) * w
    y1 = (rect["y1"] + pad) * h
    # 夹在画面内，同时保证宽高至少 2px（否则退化成一个点，看不出来）
    x0 = max(0, min(w - 2, int(round(x0))))
    y0 = max(0, min(h - 2, int(round(y0))))
    x1 = max(x0 + 2, min(w, int(round(x1))))
    y1 = max(y0 + 2, min(h, int(round(y1))))
    return (x0, y0, x1, y1)


# --------------------------------------------------------------------------
# 取帧
# --------------------------------------------------------------------------
def cache_dir(root: str) -> str:
    d = os.path.join(os.path.abspath(root), C.CACHE_DIR, C.BOX_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def frame_jpg(video: str, root: str, t: float, width: int = None,
              tag: str = "", fresh: bool = False) -> Tuple[str, int, int]:
    """抽那一秒的原帧（已按 width 缩放），落盘缓存。返回 (路径, 宽, 高)。

    为什么不直接用 lines.shot 那张台词截图：那张是 480 宽的**缩略图**，
    用来在列表里认句子够了，拿来看"框有没有压到字的边缘"不够——差一个
    数量级的像素。这里按需抽一张 960 宽的，抽一次缓存一次，
    同一时刻反复翻不会重复解视频。

    缓存键用时间戳（毫秒）+ 宽度，所以改宽度或改时间就是新的一张。
    """
    w = int(width or C.BOX_FRAME_WIDTH)
    w -= w % 2
    safe = "".join(ch if ch not in '\\/:*?"<>|' else "_"
                   for ch in (tag or os.path.splitext(
                       os.path.basename(video))[0])[:40])
    ms = int(round(max(0.0, float(t or 0)) * 1000))
    out = os.path.join(cache_dir(root), f"{safe}_{ms}_{w}.jpg")
    if not fresh and os.path.exists(out) and os.path.getsize(out) > 0:
        try:
            from PIL import Image
            with Image.open(out) as im:
                return out, im.width, im.height
        except Exception:
            pass                      # 缓存坏了就重抽
    cmd = [C.FFMPEG, "-y", "-nostdin", "-v", "error", "-hide_banner",
           "-ss", f"{max(0.0, float(t or 0)):.3f}", "-i", video,
           "-frames:v", "1", "-vf", f"scale={w}:-2",
           "-q:v", "2", out + ".tmp.jpg"]
    try:
        media.run(cmd, timeout=120)
    except Exception:
        pass
    tmp = out + ".tmp.jpg"
    if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        try:
            os.replace(tmp, out)
        except Exception:
            pass
    if not os.path.exists(out):
        return "", 0, 0
    try:
        from PIL import Image
        with Image.open(out) as im:
            return out, im.width, im.height
    except Exception:
        return "", 0, 0


# --------------------------------------------------------------------------
# 标注
# --------------------------------------------------------------------------
def _dashed_rect(d, box, color, width=1, dash=7, on=True):
    x0, y0, x1, y1 = box
    for x in range(x0, x1, dash * 2):
        d.line([(x, y0), (min(x + dash, x1), y0)], fill=color, width=width)
        if on:
            d.line([(x, y1), (min(x + dash, x1), y1)], fill=color, width=width)
    for y in range(y0, y1, dash * 2):
        d.line([(x0, y), (x0, min(y + dash, y1))], fill=color, width=width)
        if on:
            d.line([(x1, y), (x1, min(y + dash, y1))], fill=color, width=width)


_CJK_FONT = None


def _font(size: int = 14):
    """中文字体：标签要写台词原文，默认位图字体中文全是方块。
    Windows 优先微软雅黑，找不到就退 DejaVu（此时中文仍会缺字，
    但不至于抛异常毁掉整张图）。"""
    global _CJK_FONT
    try:
        from PIL import ImageFont
        if _CJK_FONT is None:
            for p in (r"C:\Windows\Fonts\msyh.ttc",
                      r"C:\Windows\Fonts\simhei.ttf",
                      r"C:\Windows\Fonts\simsun.ttc",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
                if os.path.exists(p):
                    _CJK_FONT = ImageFont.truetype(p, size)
                    break
        if _CJK_FONT is not None and getattr(_CJK_FONT, "size", size) != size:
            _CJK_FONT = ImageFont.truetype(_CJK_FONT.path, size)
        return _CJK_FONT
    except Exception:
        return None


def _label(d, xy, text: str, color, size=13):
    """在框上方写一行小字（带深色底，压在亮暗画面上都读得清）。

    `d` 就是调用方已有的 ImageDraw 对象——不要对它再包一层
    ImageDraw.Draw(d)，那个调用会抛异常然后被 except 吞掉，
    标签无声消失（这个坑真实发生过）。
    """
    try:
        if not text:
            return
        fd = _font(size + 1)
        box = d.textbbox(xy, text, font=fd)
        pad = 3
        d.rectangle((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad),
                    fill=(17, 20, 26))
        d.text(xy, text, fill=color, font=fd)
    except Exception:
        pass


def annotate(src: str, saved_box: Dict[str, Any],
             live_box: Optional[Dict[str, Any]] = None,
             pad: float = None, stamp: str = "") -> bytes:
    """把框画到帧上，返回 JPEG 字节。

    画两层，从下到上：
      ① 蓝色     = 库里存的方框凭证（主对象）
      ② 绿色/红色 = 现场重检的框（叠在蓝框上，两个框分离就说明框不稳）
    """
    _vendor()
    from io import BytesIO
    from PIL import Image, ImageDraw
    p = pad if pad is not None else float(C.BOX_PAD)
    with Image.open(src) as im:
        img = im.convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)

    if saved_box:
        sb = rect_px(band_rect(saved_box), w, h, p)
        d.rectangle(sb, outline=COL_SAVED, width=2)
        if stamp:
            _label(d, (max(0, sb[0]), max(0, sb[1] - 15)), stamp, COL_SAVED)

    if live_box:
        lb = rect_px(band_rect(live_box), w, h, p)
        ok = live_box.get("__match__", True)
        col = COL_LIVE if ok else COL_LIVE_BAD
        _dashed_rect(d, lb, col, 2, 9)
        _label(d, (max(0, lb[0]), max(0, lb[1] - 15)),
               "live ok" if ok else "live NG", col)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# --------------------------------------------------------------------------
# 现场复核
# --------------------------------------------------------------------------
def profile_of(video: str, fresh: bool = False) -> "O.SubtitleProfile":
    """取这部片的字幕位置画像（带进程内缓存）。"""
    try:
        key = f"{os.path.abspath(video)}|{int(os.path.getmtime(video))}"
    except OSError:
        key = os.path.abspath(video)
    if not fresh and key in _PROF_CACHE:
        return _PROF_CACHE[key]
    prof = O.calibrate(video)
    _PROF_CACHE[key] = prof
    while len(_PROF_CACHE) > _PROF_MAX:
        _PROF_CACHE.pop(next(iter(_PROF_CACHE)))
    return prof


def probe_times(row: Dict[str, Any], n: int = None) -> List[float]:
    """一条台词内取 n 个探测时刻（首/中/尾）。"""
    n = int(n or C.BOX_PROBE_FRAMES)
    s = float(row.get("start") or 0)
    e = float(row.get("end") or 0)
    if e <= s:
        e = s + 0.5
    if n <= 1:
        return [s + (e - s) / 2]
    span = e - s
    return [s + span * _PROBE_AT[min(i, len(_PROBE_AT) - 1)] for i in range(n)]


def probe(video: str, row: Dict[str, Any], n: int = None,
          tol: float = None, progress=None,
          fresh_profile: bool = False) -> Dict[str, Any]:
    """回画面上重新检一次：这条台词的框，现在还画得准吗？

    对每一句取首/中/尾几个时刻，各抽一帧、重跑 OCR + select_subtitle，
    然后回答两个问题：
      · 现场还找得到字幕吗？找不到 = 帧抽错了或这帧字幕正好淡出
      · 现场框和库里的框在同一个位置吗？偏了 = 凭证和画面脱钩

    位置判据沿用 core/verify.py 的口径（|Δcx| + |Δcy| ≤ tol），
    文本判据用 ocr._similar（容忍 OCR 偶发错字）。

    `fresh_profile=True` 强制重做一次字幕位置标定（默认走进程内缓存）。
    标定结果不对时——比如某部片子刚改了压制、字幕位置挪了——用它重来。
    """
    tol = float(tol if tol is not None else C.BOX_LIVE_TOL)
    text = (row.get("text") or "").strip()
    saved = V._load_box(row)
    times = probe_times(row, n)
    out: Dict[str, Any] = {"ok": False, "text": text, "saved": saved,
                           "times": [round(t, 2) for t in times],
                           "frames": [], "matched": 0, "found": 0,
                           "verdict": "?", "detail": ""}

    eng = O.make_engine()
    if eng is None:
        out["detail"] = f"OCR 引擎不可用：{O._ENG_ERR}"
        return out
    prof = profile_of(video, fresh=fresh_profile)
    frames = V._frames_at(video, times)
    try:
        import numpy as np
    except Exception as e:
        out["detail"] = f"numpy 不可用：{e}"
        return out

    # _frames_at 自带时刻（返回 (t, buf, w, h)），不要再和 times 一起 zip——
    # 两者一起 zip 会把 4 元组当成单个元素解包，直接 ValueError。
    for t, buf, w, h in frames:
        rec: Dict[str, Any] = {"t": round(t, 2), "text": "", "box": None,
                               "match": None, "why": ""}
        if not buf or w <= 0 or h <= 0:
            rec["why"] = "抽帧失败"
            out["frames"].append(rec)
            continue
        img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w)
        try:
            res, _ = eng(np.repeat(img[:, :, None], 3, axis=2))
        except Exception as e:
            rec["why"] = f"OCR 异常：{type(e).__name__}"
            out["frames"].append(rec)
            continue
        lt, pf = O.select_subtitle(res, w, h, prof, want_box=True) \
            if res else ("", None)
        rec["text"] = lt
        rec["box"] = pf
        if not pf:
            rec["why"] = "这一帧没检出字幕框"
            out["frames"].append(rec)
            continue
        out["found"] += 1
        if not saved:
            # 无凭证可比 = 回填场景（库里本来没框 / 刚被清空）。
            # 此时判据只剩文本：画面上稳定检出同一句话，这些框就是
            # 合法的凭证来源。不能因为"没得比"就判 NG——否则
            # "先清空再回填"永远走不通（清空后一比一个 NG）。
            same = (not text) or O._similar(lt, text)
            rec["match"] = bool(same)
            rec["why"] = ("库里无凭证，画面检出同文本，可作凭证"
                          if same else "库里无凭证，且文本对不上")
            out["frames"].append(rec)
            continue
        sr = band_rect(saved)
        lr = band_rect(pf)
        d = abs(lr["cx"] - sr["cx"]) + abs(lr["cy"] - sr["cy"])
        rec["dx"] = round(lr["cx"] - sr["cx"], 4)
        rec["dy"] = round(lr["cy"] - sr["cy"], 4)
        rec["dist"] = round(d, 4)
        same_txt = (not lt) or O._similar(lt, text)
        if d <= tol and same_txt:
            rec["match"] = True
            rec["why"] = "框和文本都对得上"
            out["matched"] += 1
        else:
            rec["match"] = False
            rec["why"] = ("位置偏了" if d > tol else "") + \
                         ("文本对不上" if not same_txt else "")
            rec["why"] = rec["why"] or "对不上"
        if progress:
            progress(f"现场复核 {rec['t']:.2f}s · "
                     + ("对上了" if rec["match"] else rec["why"]))
        out["frames"].append(rec)

    n_ok = sum(1 for f in out["frames"] if f.get("match"))
    n_got = sum(1 for f in out["frames"] if f.get("box"))
    if n_got == 0:
        out["verdict"] = "NG"
        out["detail"] = "这三帧都没检出字幕框"
    elif n_ok == n_got:
        out["ok"] = True
        out["verdict"] = "OK"
        out["detail"] = f"{n_ok}/{n_got} 帧现场框与库里凭证一致"
    else:
        out["verdict"] = "NG"
        out["detail"] = f"{n_ok}/{n_got} 帧对得上"
    return out


def fold_probe(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把现场复核的多个框折成一条可入库的凭证。

    直接复用 ocr._fold_boxes 的聚合口径（中位数 + 跨帧漂移），这样
    "补录的框"和"索引时自动产出的框"是同一套统计，verify 的阈值
    对两者一视同仁，不会出现人工补的框过不了自己的校验。
    """
    boxes = []
    for f in frames or []:
        b = f.get("box")
        if b and f.get("match"):
            boxes.append(b)
    if not boxes:
        return {}
    return O._fold_boxes(boxes)


# --------------------------------------------------------------------------
# 汇总：给页面左侧列表用
# --------------------------------------------------------------------------
def audit_line(st, vid: int, row: Dict[str, Any]) -> Dict[str, Any]:
    """一条台词的复查摘要（不含图片）。"""
    v = st.get_video(vid) or {}
    r = dict(row)
    r["video_src"] = v.get("line_source") or ""
    verdict = V.check_line(r)
    box = verdict.box or {}
    has = bool(box)
    span = max(0.0, float(row.get("end") or 0) - float(row.get("start") or 0))
    hits = int(box.get("hits") or 0)
    cov = hits / (span / float(C.OCR_STEP) + 1.0) if span > 0.6 else None
    return {
        "id": row.get("id"),
        "text": row.get("text") or "",
        "start": round(float(row.get("start") or 0), 2),
        "end": round(float(row.get("end") or 0), 2),
        "start_str": ST.fmt_ts(row.get("start")),
        "end_str": ST.fmt_ts(row.get("end") or row.get("start")),
        "ok": verdict.ok, "skipped": verdict.skipped,
        "reasons": verdict.reasons,
        "has_box": has,
        "src": V._line_src(r),
        "metrics": {
            "cx": box.get("cx"), "cy": box.get("cy"),
            "bw": box.get("bw"), "bh": box.get("bh"),
            "ar": box.get("ar"), "conf": box.get("conf"),
            "hits": hits, "drift": box.get("drift"),
            "nbox": box.get("nbox"), "nchar": box.get("nchar"),
            "h_ratio": box.get("h_ratio"),
            "cov": round(cov, 3) if cov is not None else None,
            # 字体指纹（可能为空：SRT 来源、老库没有这组字段）
            "font": box.get("font") or None,
            "font_dev": box.get("font_dev"),
        },
    }


def audit_video(st, vid: int) -> Dict[str, Any]:
    """一个素材的复查清单 + 汇总。"""
    v = st.get_video(vid) or {}
    rows = st.lines_of(vid)
    items = [audit_line(st, vid, r) for r in rows]
    checked = [x for x in items if not x["skipped"]]
    bad = [x for x in checked if not x["ok"]]
    nobox = [x for x in items if not x["has_box"]]
    reason: Dict[str, int] = {}
    for x in bad:
        for rr in x["reasons"]:
            k = rr.split(" ")[0]
            reason[k] = reason.get(k, 0) + 1
    return {
        "video": {"id": vid, "name": v.get("name", ""),
                  "duration": v.get("duration") or 0,
                  "width": v.get("width") or 0, "height": v.get("height") or 0,
                  "line_source": v.get("line_source") or ""},
        "total": len(items), "checked": len(checked),
        "passed": len(checked) - len(bad), "failed": len(bad),
        "skipped": len(items) - len(checked),
        "no_box": len(nobox),
        "rate": round((len(checked) - len(bad)) / len(checked), 4)
        if checked else 0.0,
        "reasons": reason,
        "items": items,
    }
