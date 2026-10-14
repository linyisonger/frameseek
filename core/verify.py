"""方框校验：每一条入库字幕，都必须能回答"画面上真有这么一个框吗"。

为什么需要这一层
----------------
OCR 的几何筛选（core/ocr.py: select_subtitle）是**单帧决策**：
那一帧里有几个框，挑出像字幕的那几个拼成一句。但从单帧到入库这一步
还有很长一段路，中间有几处会让"文本"和"方框"脱钩：

  · 多帧合并靠 `_similar()`（字符集重合 ≥75%）。真实字幕在 0.5 秒网格上
    反复识别时会有偶发漏字，纯相等会把一句话拆成十几条；但重合度判据
    也会把**两句恰好用词相近的相邻台词**并成一条——文本被改写了，
    方框却还是前一句的。
  · 文本可能被换成更长的一条（`我是谁` → `我是谁啊`），而方框凭证如果
    不跟着换，就会出现"文本来自 A 帧、方框来自 B 帧"的对不上。
  · 边界帧：字幕淡入淡出时，检测框会套得比稳态松，字高偏大、置信度偏低。

所以校验分两级：

  ① **凭证自检**（`check_line`，纯读库，毫秒级）
     拿入库时存下的方框凭证，逐条核对它自身是否自洽、是否在合理区间。
     这一级不重跑 OCR，适合全库体检。

  ② **现场复核**（`recheck_video`，重新抽帧 OCR，分钟级）
     拿凭证里的时间点和几何，回到画面上重新检测一次，确认"那个位置
     确实有一个框，而且框里的字和库里这条对得上"。这是真正的证伪。

用法
----
    python -m core.verify "E:\\视频\\电影"              # 全库凭证自检
    python -m core.verify "E:\\视频\\电影" --recheck    # 抽样现场复核
    python -m core.verify "E:\\视频\\电影" --recheck --videos 功夫.mp4
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

from core import config as C
from core import media
from core import ocr as O


# --------------------------------------------------------------------------
# 判定阈值（全部来自 core/_probe_box.py 对 6 部影片 48 帧 95 个检测框的实测，
# 见 README「方框校验」一节的分布表）
# --------------------------------------------------------------------------
# 字宽比 ar = 框宽像素 /(字数 × 字高像素)。真实字幕是方形字，正样本
# P05~P95 = 0.73~1.22；负样本（识别串行/字形粘连）P25 就到 1.03、P50=1.12、
# P95=3.79。两段重叠区间 1.03~1.22，所以上界放到 1.45——宁可放过一点，
# 也不能为了滤干净而误杀真台词。下界 0.55 同理（正样本 P05=0.73，
# 留两成余量，因为长句里标点窄、拉丁字母窄都会拉低这个值）。
AR_MIN = 0.55
AR_MAX = 1.45
# 跨帧漂移 drift：一帧框心偏离共识框心的最大距离（归一化，相加）。
# 字幕常驻时每帧框几乎重合；飘得远说明这行是把不相干的文本并进来的。
DRIFT_MAX = 0.10
# 支持帧数 hits：字幕在画面上被多少个采样帧认到。
# 注意不能用"至少 2 帧"这种绝对阈值——只显示 0.8 秒的短台词在 0.5 秒
# 网格上本来就只该命中 1~2 帧，端到端实测《唐伯虎点秋香》60 秒片段里
# 「有见地」「不要这样啦」这类 3~5 字短台词就是 1 帧，硬卡 2 会全判死。
# 真正该看的是**覆盖率**：被认到的时长占它声称时长的比例。
# 一条声称显示 3 秒却只有 1 帧认到（覆盖率 0.17）的，才是真的可疑。
COVERAGE_MIN = 0.34
# 上限：覆盖率 >100% 只有两种可能——区间被拉得比证据长，或合并时串进了
# 别的句子的帧。端到端实测修 _same_place 之前出现过 120%~150%。
COVERAGE_MAX = 1.25
HITS_MIN = 1
# 置信度：正样本 P05 = 0.70，负样本 P50 = 0.66。卡在两者之间偏松。
CONF_MIN = 0.62
# 框中心距画面中线的偏移上限。字幕是水平居中的，实测正样本
# P75 = 0.07、P95 = 0.21；负样本 P50 = 0.19。卡 0.24。
CX_OFF_MAX = 0.24


class Verdict:
    """一条台词的校验结论。

    `skipped=True` 表示这条台词**不适用**方框校验（来源是外挂SRT/内嵌字幕/
    语音转写，本来就不靠画面框）。它不是"通过"也不是"失败"，报告里要单列
    —— 把"没框可查"混进"没通过"，会让真实的问题被淹没。
    """

    __slots__ = ("ok", "reasons", "text", "start", "end", "box", "detail",
                 "skipped")

    def __init__(self, ok: bool, reasons: List[str], text: str = "",
                 start: float = 0.0, end: float = 0.0,
                 box: Optional[Dict[str, Any]] = None,
                 detail: str = "", skipped: bool = False):
        self.ok = ok
        self.reasons = reasons
        self.text = text
        self.start = start
        self.end = end
        self.box = box or {}
        self.detail = detail
        self.skipped = skipped

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "reasons": self.reasons, "text": self.text,
                "start": round(self.start, 2), "end": round(self.end, 2),
                "box": self.box, "detail": self.detail,
                "skipped": self.skipped}

    def __repr__(self) -> str:
        mark = "OK " if self.ok else "NG "
        return (f"<{mark}{self.text[:20]!r} "
                f"{','.join(self.reasons) if self.reasons else '-'}>")


def _line_src(row: Dict[str, Any]) -> str:
    """这条台词的来源。lines.src 是新加的列，老库里是 NULL，
    这时回落到 videos.line_source（scan_library 会把它塞进 row 的
    `video_src`）。没有这个回落的话，老库会被整片判成"无方框凭证"，
    报告里全是无信息量的失败——那是迁移造成的，不是数据有问题。
    """
    s = (row.get("src") or "").strip().lower()
    if s:
        return s
    return (row.get("video_src") or "").strip().lower()


def check_line(row: Dict[str, Any], need_box: bool = True
               ) -> Verdict:
    """凭证自检：拿存下来的方框凭证核这一行，不重跑 OCR。

    `row` 是 lines 表的一行（需含 text/start/end/box 各列，见 store.py）。
    返回 Verdict；ok=False 时 reasons 列出具体哪一项没过。

    这里**不**重新判断"这条台词是不是噪声"——那是 OCR 净化层
    （core/ocr.py: is_watermark）的事。本层只回答一个问题：
    **这条文本和它记录的方框，对得上吗？**
    两层职责分开，才能在报告里说清是哪一步出的问题。
    """
    text = (row.get("text") or "").strip()
    start = float(row.get("start") or 0)
    end = float(row.get("end") or 0)
    box = row.get("box")
    if isinstance(box, str):
        try:
            box = json.loads(box)
        except Exception:
            box = None

    if not text:
        return Verdict(False, ["空文本"], text, start, end)

    # --- 凭证存在性 -----------------------------------------------------
    # 外挂 SRT / 内嵌字幕 / 语音转写是"不靠画面框"的来源，本来就没有
    # 画面证据，不该被判死——那不是质量问题，只是没有方框可查。
    # 判据用 lines.src，老库回落到 videos.line_source。
    src = _line_src(row)
    if not box:
        if src and src != "ocr":
            return Verdict(True, [], text, start, end, {},
                           detail=f"{src} 来源，不依赖画面框", skipped=True)
        return Verdict(False, ["无方框凭证"], text, start, end, {})

    b = box
    reasons: List[str] = []

    # --- 1. 字宽比：文本长度与方框尺寸是否自洽 --------------------------
    # 这是判别力最强的一项。文本被拼接、漏字、串行，都会让字数和框宽
    # 对不上。实测正样本 0.73~1.22，识别串行的噪声能到 4.18。
    ar = float(b.get("ar") or 0)
    if ar <= 0:
        reasons.append("字宽比缺失")
    elif not (AR_MIN <= ar <= AR_MAX):
        reasons.append(f"字宽比 {ar:.2f} 越界({AR_MIN}~{AR_MAX})")

    # --- 2. 跨帧一致性：多帧的框是否都落在同一位置 ----------------------
    drift = float(b.get("drift") or 0)
    if drift > DRIFT_MAX:
        reasons.append(f"跨帧漂移 {drift:.3f} > {DRIFT_MAX}")

    # --- 3. 支持帧数与覆盖率：字幕是不是真的在画面上待了那么久 ---------
    # 覆盖率 = 被认到的时长 / 声称的时长。短台词 1 帧很正常，
    # 但一条声称 4 秒却只在一帧里出现过，说明这个区间是猜出来的。
    hits = int(b.get("hits") or 0)
    span = max(0.0, end - start)
    if hits < HITS_MIN:
        reasons.append("无帧支持")
    elif span > 0.6:                 # 太短的区间谈覆盖率没意义
        # 区间内的采样点数 = span/步长 + 1（start 和 end 都是帧时刻，两端都含）。
        # 少写那个 +1 会让覆盖率系统性虚高 33~50%——实测同一条
        # 「10.00~13.00 有7帧支持」本该是 100%，算出来成了 117%。
        expect = span / float(C.OCR_STEP) + 1.0
        cov = hits / expect
        if cov < COVERAGE_MIN:
            reasons.append(
                f"覆盖率 {cov:.0%}({hits}帧/{span:.1f}s)<{COVERAGE_MIN:.0%}")
        elif cov > COVERAGE_MAX:
            # >100% 只有两种可能：区间被拉得比证据还长，或合并时串进了别的句的帧
            reasons.append(f"覆盖率 {cov:.0%} 异常偏高(区间可能过长)")

    # --- 4. 置信度 ------------------------------------------------------
    conf = float(b.get("conf") or 0)
    if conf < CONF_MIN:
        reasons.append(f"置信度 {conf:.2f} < {CONF_MIN}")

    # --- 5. 水平居中：字幕总是居中的，飘到边上的是画面杂字 --------------
    cx = float(b.get("cx") if b.get("cx") is not None else 0.5)
    cx_off = abs(cx - 0.5)
    if cx_off > CX_OFF_MAX:
        reasons.append(f"框心偏离中线 {cx_off:.2f}")

    # --- 6. 字高：太小是噪声，太大是标题字 ------------------------------
    bh = float(b.get("bh") or 0)
    if bh < 0.05:
        reasons.append(f"字高过小 {bh:.3f}")
    elif bh > 0.45:
        reasons.append(f"字高过大 {bh:.3f}")

    # --- 7. 帧内框数 ---------------------------------------------------
    nbox = int(b.get("nbox") or 0)
    if nbox < 1:
        reasons.append("无框")

    # --- 8. 字体指纹偏离（不判死，只标注"优先怀疑"）-------------------
    # 这部片的主流字幕字体/字号在片内高度稳定；偏离大的行可能是 OCR 串行、
    # 单帧误检，也可能只是片头大字号/艺术字场景。所以**不进 reasons**
    # （不判伪），而是写进 detail——纠错时先看这些行，二次验证时它们的
    # 证据权重降档。设计取舍见 core/ocr.py 的 _rebase_font 注释。
    font_note = ""
    fdev = float(b.get("font_dev") or 0)
    fnt = b.get("font") or {}
    if fdev > float(C.OCR_FONT_DEV_MAX):
        font_note = (f"字体异常 dev={fdev:.2f}"
                     f"（墨密度 {fnt.get('ink', 0):.2f} / "
                     f"笔画 {fnt.get('stroke', 0):.1f}px / "
                     f"字高占画面 {fnt.get('px_h', 0) * 100:.1f}%）"
                     f"——优先怀疑，复核时可忽略其方框证据")

    return Verdict(not reasons, reasons, text, start, end, b,
                   detail=font_note)


def check_video(st, vid: int, need_box: bool = True) -> List[Verdict]:
    """对一个素材的全部台词做凭证自检。

    把 videos.line_source 塞进每行的 `video_src`，这样老库（lines.src
    是 NULL）也能正确识别"这些台词本来就不是 OCR 来的"。
    """
    v = st.get_video(vid) or {}
    vsrc = v.get("line_source") or ""
    rows = []
    for r in st.lines_of(vid):
        r["video_src"] = vsrc
        rows.append(r)
    return [check_line(r, need_box) for r in rows]


# --------------------------------------------------------------------------
# ② 现场复核：回到画面上重找一遍
# --------------------------------------------------------------------------
def recheck_video(video: str, rows: List[Dict[str, Any]],
                  progress=None, tolerance: float = 0.14
                  ) -> List[Tuple[Dict[str, Any], Optional[Verdict]]]:
    """对一批台词逐条回画面复核。

    做法：对每条台词取它区间的中点（字幕最稳的时刻），在那一帧上重新跑
    OCR + select_subtitle，然后检查库里记录的方框中心，与这一次实际检测
    到的框中心是否吻合。

    为什么取中点而不是逐帧比对：字幕淡入淡出时框会套得松，端点帧不可靠；
    中点处字幕已经稳定，且 OCR_STEP=0.5 的网格通常正好命中。

    返回 [(row, Verdict|None), ...]，Verdict.detail 里写明现场看到了什么。
    进度回调收到字符串。
    """
    n = len(rows)
    prof = O.calibrate(video, progress=progress)
    # 把待查台词按时间排序，一次顺序走帧，避免反复 seek 同一段视频
    items = sorted(range(n), key=lambda i: float(rows[i].get("start") or 0))
    results: List[Optional[Verdict]] = [None] * n

    times = []
    for i in items:
        r = rows[i]
        s = float(r.get("start") or 0)
        e = float(r.get("end") or 0)
        mid = s + max(0.0, (e - s)) / 2 if e > s else s
        times.append(mid)

    frames = _frames_at(video, times)
    eng = O.make_engine()
    if eng is None:
        return [(rows[i], Verdict(False, [f"OCR 引擎不可用:{O._ENG_ERR}"],
                                 rows[i].get("text", ""),
                                 float(rows[i].get("start") or 0)))
                for i in range(n)]

    import numpy as np
    done = 0
    for i, (t, buf, w, h) in zip(items, frames):
        r = rows[i]
        row_box = _load_box(r)
        if not buf:
            results[i] = Verdict(False, ["抽帧失败"], r.get("text", ""),
                                 float(r.get("start") or 0))
            continue
        img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w)
        res, _ = eng(np.repeat(img[:, :, None], 3, axis=2))
        text, pf = O.select_subtitle(res, w, h, prof, want_box=True) \
            if res else ("", None)
        v = _compare(row_box, pf, text, r, prof, t, tolerance)
        results[i] = v
        done += 1
        if progress:
            progress(f"现场复核 {done}/{n} · "
                     f"{'对上了' if (v and v.ok) else '对不上'}")
    return [(rows[i], results[i]) for i in range(n)]


def _load_box(r: Dict[str, Any]) -> Dict[str, Any]:
    b = r.get("box")
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except Exception:
            return {}
    return b or {}


def _compare(saved: Dict[str, Any], live: Optional[Dict[str, Any]],
             live_text: str, row: Dict[str, Any],
             prof: "O.SubtitleProfile", t: float,
             tolerance: float = 0.14) -> Verdict:
    """把库里的凭证和现场重新检测的结果对上。

    现场框 = live（可能是 None，这一帧什么都没检到）
    判定：中心点距离在容忍范围内，且现场识别文本与库中文本相似。
    """
    text = (row.get("text") or "").strip()
    start = float(row.get("start") or 0)
    end = float(row.get("end") or 0)
    if not saved:
        return Verdict(False, ["无方框凭证"], text, start, end)
    if not live:
        return Verdict(False, ["画面上没找到框"], text, start, end, saved,
                       detail=f"t={t:.2f}s 该帧未检出字幕框")

    d = abs(float(live["cx"]) - float(saved.get("cx", live["cx"]))) + \
        abs(float(live["cy"]) - float(saved.get("cy", live["cy"])))
    reasons: List[str] = []
    if d > tolerance:
        reasons.append(f"位置对不上(差 {d:.3f}>{tolerance})")
    # 现场文本要和库里这条是同一句（OCR 偶发漏字，容忍错字）
    if live_text and not O._similar(live_text, text):
        reasons.append("文本对不上")
    detail = (f"t={t:.2f}s 现场框 cx={live['cx']:.3f} cy={live['cy']:.3f} "
              f"bw={live['bw']:.3f} ar={live['ar']:.2f} conf={live['conf']:.2f}"
              f" → {live_text!r}")
    return Verdict(not reasons, reasons, text, start, end, saved, detail)


def _frames_at(video: str, times: List[float]
               ) -> List[Tuple[float, bytes, int, int]]:
    """在给定时间点各抽一帧，口径与 ocr._frame_stream 完全一致
    （同样的裁切带、同样的缩放、同样的灰度），否则坐标对不上。"""
    probe = media.ffprobe_json(video)
    W = int(probe.get("width") or 1920)
    H = int(probe.get("height") or 1080)
    ow = int(C.OCR_WIDTH)
    oh = max(2, int(round(ow * (H * C.OCR_BAND) / max(1, W))))
    oh -= oh % 2
    vf = (f"crop=iw:ih*{C.OCR_BAND:.3f}:0:ih*(1-{C.OCR_BAND:.3f}),"
          f"scale={ow}:{oh},format=gray")
    out: List[Tuple[float, bytes, int, int]] = []
    for t in times:
        cmd = [C.FFMPEG, "-nostdin", "-v", "error", "-hide_banner",
               "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
               "-vf", vf, "-f", "rawvideo", "-"]
        try:
            raw = media.run(cmd, timeout=60).stdout
        except Exception:
            out.append((t, b"", ow, oh))
            continue
        if len(raw) < ow * oh:
            out.append((t, b"", ow, oh))
            continue
        out.append((t, raw[:ow * oh], ow, oh))
    return out


# --------------------------------------------------------------------------
# 素材库级体检
# --------------------------------------------------------------------------
def scan_library(root: str, only: str = "", verbose: bool = True,
                 progress=None) -> Dict[str, Any]:
    """扫一个素材库的 .frameseek/index.db，对每条台词做凭证自检。"""
    db = os.path.join(root, ".frameseek", "index.db")
    if not os.path.exists(db):
        raise FileNotFoundError(f"找不到索引库：{db}")
    from core import store as ST
    st = ST.Store(db)
    vids = st.list_videos(limit=5000)
    if only:
        vids = [v for v in vids if only in v["name"]]

    total = passed = skipped = 0
    bad_by_video: List[Dict[str, Any]] = []
    reason_count: Dict[str, int] = {}
    src_count: Dict[str, int] = {}

    for v in vids:
        verdicts = check_video(st, v["id"])
        v_total = len(verdicts)
        v_bad = [x for x in verdicts if not x.ok]
        # skipped 单独统计：这些行不是质量问题，是"来源本来就不看画面"
        v_skip = [x for x in verdicts if x.skipped]
        total += v_total
        passed += v_total - len(v_bad) - len(v_skip)
        skipped += len(v_skip)
        for x in v_skip:
            k = x.detail.split(" ")[0] or "无框"
            src_count[k] = src_count.get(k, 0) + 1
        for x in v_bad:
            for rr in x.reasons:
                key = rr.split(" ")[0]
                reason_count[key] = reason_count.get(key, 0) + 1
        if v_bad:
            bad_by_video.append({
                "id": v["id"], "name": v["name"], "total": v_total,
                "bad": len(v_bad),
                "samples": [{"text": x.text, "start": round(x.start, 1),
                             "reasons": x.reasons} for x in v_bad[:8]],
            })
        if progress:
            progress(f"体检 {v['name']} · "
                     f"{v_total - len(v_bad) - len(v_skip)}/{v_total} 通过")

    checked = total - skipped
    rep = {
        "root": root, "videos": len(vids), "lines": total,
        "checked": checked, "passed": passed,
        "skipped": skipped,
        "failed": checked - passed,
        "rate": round(passed / checked, 4) if checked else 0.0,
        "reasons": reason_count, "sources": src_count,
        "bad_videos": sorted(bad_by_video, key=lambda x: -x["bad"]),
    }
    if verbose:
        print_report(rep)
    return rep


def recheck_library(root: str, only: str = "", limit: int = 0,
                    progress=None) -> Dict[str, Any]:
    """对素材库做现场复核：重新抽帧，确认框真的在画面上。

    慢（每条台词一次 OCR），默认只抽前 `limit` 条做样本。
    """
    db = os.path.join(root, ".frameseek", "index.db")
    if not os.path.exists(db):
        raise FileNotFoundError(f"找不到索引库：{db}")
    from core import store as ST
    st = ST.Store(db)
    vids = st.list_videos(limit=5000)
    if only:
        vids = [v for v in vids if only in v["name"]]
    if limit:
        vids = vids[:max(1, limit // 10)]

    total = ok = 0
    details: List[Dict[str, Any]] = []
    for v in vids:
        rows = st.lines_of(v["id"])
        rows = [r for r in rows if _load_box(r)]
        if not rows:
            continue
        if limit:
            rows = rows[:limit]
        video = os.path.join(root, v["rel"])
        if not os.path.exists(video):
            continue
        res = recheck_video(video, rows, progress=progress)
        for row, verdict in res:
            total += 1
            if verdict and verdict.ok:
                ok += 1
            details.append({
                "video": v["name"], "text": row.get("text"),
                "start": row.get("start"), "ok": bool(verdict and verdict.ok),
                "reasons": verdict.reasons if verdict else ["?"],
                "detail": verdict.detail if verdict else "",
            })
    rep = {"root": root, "checked": total, "matched": ok,
           "rate": round(ok / total, 4) if total else 0.0,
           "details": details}
    return rep


def print_report(rep: Dict[str, Any]):
    print(f"\n素材库：{rep['root']}")
    print(f"素材 {rep['videos']} 个 · 台词 {rep['lines']} 条")
    print(f"  可校验 {rep['checked']} 条 → 通过 {rep['passed']} · "
          f"不达标 {rep['failed']} · "
          f"通过率 {rep['rate'] * 100:.1f}%")
    if rep.get("skipped"):
        parts = " ".join(f"{k} {v}" for k, v in
                         sorted(rep["sources"].items(), key=lambda x: -x[1]))
        print(f"  不适用 {rep['skipped']} 条（{parts}）"
              f" —— 这些来源本来就不看画面，不算质量问题")
    if rep["reasons"]:
        print("\n不达标原因分布：")
        for k, n in sorted(rep["reasons"].items(), key=lambda x: -x[1]):
            print(f"  {k:16} {n}")
    if rep["bad_videos"]:
        print("\n问题素材（前 10）：")
        for v in rep["bad_videos"][:10]:
            print(f"  {v['name']}  {v['total'] - v['bad']}/{v['total']} 通过")
            for s in v["samples"][:4]:
                print(f"      [{s['start']}s] {s['text'][:24]!r} "
                      f"→ {', '.join(s['reasons'])}")


# --------------------------------------------------------------------------
def main(argv: List[str]):
    import argparse
    ap = argparse.ArgumentParser(
        description="FrameSeek 方框校验：核对每条台词是否有画面上的方框支撑")
    ap.add_argument("root", help="素材库目录（含 .frameseek/index.db）")
    ap.add_argument("--videos", default="", help="只查名字包含此串的素材")
    ap.add_argument("--recheck", action="store_true",
                    help="现场复核（重新抽帧 OCR，慢但能真正证伪）")
    ap.add_argument("--limit", type=int, default=20,
                    help="现场复核每个素材最多查几条（默认 20）")
    ap.add_argument("--json", default="", help="把报告写到这个 json 文件")
    args = ap.parse_args(argv)

    if args.recheck:
        rep = recheck_library(args.root, only=args.videos,
                              limit=args.limit,
                              progress=lambda m: print(f"  {m}"))
        print(f"\n现场复核：查了 {rep['checked']} 条，"
              f"对得上 {rep['matched']} 条（{rep['rate'] * 100:.1f}%）")
        bad = [d for d in rep["details"] if not d["ok"]]
        if bad:
            print("\n对不上的：")
            for d in bad[:30]:
                print(f"  [{d['video']} {d['start']}s] {str(d['text'])[:24]!r}"
                      f" → {', '.join(d['reasons'])}")
                if d["detail"]:
                    print(f"      {d['detail']}")
    else:
        rep = scan_library(args.root, only=args.videos)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
