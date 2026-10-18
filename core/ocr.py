# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - OCR 硬字幕识别

台词来源的最后兜底，也是本项目里唯一的"纯看画面"方案。

**为什么用它而不是语音转写**：
  1. 粤语老片、方言片 -> whisper 输出简体字，文本没法直接用；画面字幕是原文字
  2. 音乐/环境音盖过人声 -> whisper 整段漏掉；画面字幕照旧
  3. 台词时间戳来自画面，比ASR 的 segment 边界准

**性能是这个模块的全部难点**，实测数据（1920宽片，960 宽送 OCR）：
  - 流式解码+ 帧扫描：0.9ms/帧，整部 92 分钟只要 5 秒（可忽略）
  - RapidOCR 单帧：827ms（默认配置）→ **170ms（调优后）**
  - 8 进程并行：吞吐 8 倍

单帧调优是最大的一笔收益。RapidOCR 的 `config.yaml` 里 `Det.limit_type: min` +
`limit_side_len: 736`，意思是"把图缩放到最短边至少 736 像素再检测"。
我们只送960×162 的字幕带，被硬生生放大4.5 倍面积去检测，白烧 5 倍算力。
改成 `limit_type: max`（最长边封顶、不放大）再关掉方向分类模型
（字幕永远是正的，180度分类器纯属浪费），**827ms → 170ms，识别结果完全一致**。

采样策略：不靠帧差判断"字幕变没变"。试过三种判据（整块像素差、边缘掩码、
白色文字掩码 XOR），实测帧差 p50 就在 0.2~0.67 —— **电影背景本身一直在动，
根本挡不住**，触发率都在 50% 以上，等于没门控。所以改为固定网格采样 +
按识别文本去重：一条字幕常驻 1.5~3 秒，0.5 秒网格会命中它3~6 次，
识别文本相同就跳过，天然去重。逻辑简单且不会漏。

最终成本（92 分钟片）：0.5 秒网格 = 11000 帧× 170ms ÷ 8 进程 ≈ 4 分钟/部。
"""
import multiprocessing as mp
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from typing import List, Dict, Any, Tuple, Optional

from core import config as C
from core import media

_ENG = None
_ENG_ERR = ""
_LOCK = None
# 当前这部片的字幕位置画像。开跑前 calibrate() 填好，
# 通过 _worker_init 的 initargs 传给各个子进程（spawn 不继承父进程内存）。
# 类型是 SubtitleProfile，但那个类定义在文件后半段，所以这里只能先置 None，
# 用的时候走 _prof() 兜底构造一个"未标定"的默认画像。
_PROFILE = None

# worker 子进程里 onnxruntime 每个进程允许开几个线程。
# 由 _worker_init 从启动参数带进来；主进程（只做标定）保持 0，走 C.OCR_THREADS。
# 必须走全局变量：spawn 出来的子进程会重新 import 本模块，调用方在父进程里
# 改 config 传不进去，而 _worker_ocr 调 make_engine() 是不带参数的。
_W_THREADS = 0
# 引擎调优结果（给诊断用）：{"threads": 每进程线程数, "sessions": 成功设置
# 线程数的 session 个数（0 表示这个 onnxruntime 版本不支持，退回默认）,
# "provider": "dml"/"cpu", "dml_note": DML 失败原因（成功时为空）}
_ENG_TUNE = {}
# DirectML provider 开关。由 _setup_dml() 在引擎构建前一次性置位：
# venv 里装的是 CPU-only 版 onnxruntime，要上 GPU 必须把
# vendor/ort_dml（onnxruntime-directml 发行版）插到 sys.path 最前阴影掉它。
# 只有 _DML_ON=True 时 TunedSession 才往 providers 里注入 DML。
_DML_ON = False
_DML_TRY = ""

# 送进pool 前先把一批帧的原始字节攒好，凑够 CHUNK 才提交，
# 避免每帧一次 pool 往返（11000 帧就是 11000 次 IPC，纯浪费）。
CHUNK = 24

# 解码/OCR 并行：生产线程预读帧塞进这个有界队列，主线程消费。
#
# 为什么必须这么做（实测数据，亮剑 26.mp4 / 3840x2880 HEVC / 2736 秒）：
#   - ffmpeg 单独跑管道：46 帧/秒；4 进程 DML 做 OCR：22 帧/秒。管道富余一倍。
#   - 但原来 `for t, buf, w, h in _frame_stream(...)` 里调 _flush() 是**阻塞**的
#     pool.imap —— OCR 算这24 帧的工夫，主线程根本没去读管道，ffmpeg 的 stdout
#     缓冲一满就自己阻塞等着。于是「解码 278 秒 + OCR 397 秒」变成串行相加。
#   - 拆成生产/消费两线程后，管道在后台一直解，主线程只等队列，
#     两者重叠，耗时趋向 max(解码, OCR) 而不是相加。
#
# 队列必须有界：帧是 960×288 灰度 = 276KB/帧，攒满 96 帧约 26MB。
# 无界的话 4K 长片会把内存吃光（实测整部 13680 帧 = 3.8GB）。
PREFETCH = 96


def engine_info() -> dict:
    """给前端看：OCR 依赖在不在（不真加载模型）"""
    if _ENG is not None:
        return {"ok": True, "err": "", "loaded": True}
    vendor = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "vendor")
    if not os.path.isdir(vendor):
        return {"ok": False, "err": "vendor 目录不存在", "loaded": False}
    if "rapidocr_onnxruntime" not in os.listdir(vendor):
        return {"ok": False, "err": "vendor 下没有 rapidocr_onnxruntime",
                "loaded": False}
    return {"ok": True, "err": "", "loaded": False}


def _add_vendor():
    vendor = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "vendor")
    if os.path.isdir(vendor) and vendor not in os.sys.path:
        os.sys.path.insert(0, vendor)


def _dml_enabled() -> bool:
    """DML 总开关（config 默认开，环境变量可临时反转）。"""
    v = os.environ.get("FRAMESEEK_OCR_DML")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "off", "no")
    return bool(getattr(C, "OCR_USE_DML", False))


def _dml_device() -> int:
    try:
        return int(os.environ.get("FRAMESEEK_OCR_DML_DEVICE")
                   or getattr(C, "OCR_DML_DEVICE_ID", 0))
    except (TypeError, ValueError):
        return 0


def _setup_dml() -> bool:
    """准备 DirectML provider，让 GPU 分担 det/rec。幂等，任何失败都退回 CPU。

    **为什么要换包**：onnxruntime 的 provider 只能在创建 session 那一刻定，
    而 venv 里装的是 CPU-only build（get_available_providers() 只有 CPU 和
    Azure）。vendor/ort_dml 里是 pip --target 装的 onnxruntime-directml
    1.24.4（DML 独立发行版的最后版本），插到 sys.path 最前即可阴影掉旧版——
    但必须在**任何代码 import onnxruntime 之前**做：sys.modules 一旦缓存，
    换 sys.path 也静默失效，所以函数开头就检查。

    **收益边界**（实测，4060Ti）：det 39→21ms（1.8×），rec 110→97ms（1.2×）——
    rec 输入是 48×320 的单行小图，GPU 的 kernel launch 开销吃掉大部分收益，
    总体 1.28×。模型太小（det 4.5M / rec 10M 参数），不是 GPU 擅长的活。
    识别文本与 CPU 完全一致（数值差异远低于置信度门槛）。
    """
    global _DML_ON, _DML_TRY
    if _DML_ON:
        return True
    if not _dml_enabled():
        _DML_TRY = "disabled"
        return False
    if "onnxruntime" in sys.modules:
        # 已经被别的模块 import 过了，换包只会静默失效，不如老实 CPU
        _DML_TRY = "onnxruntime already imported"
        return False
    dml_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "vendor", "ort_dml")
    if not os.path.isdir(os.path.join(dml_dir, "onnxruntime")):
        _DML_TRY = "vendor/ort_dml 不存在"
        return False
    sys.path.insert(0, dml_dir)
    try:
        import onnxruntime as ort
        if "DmlExecutionProvider" not in ort.get_available_providers():
            # 系统不支持（Win10 老版本/驱动问题）——build 已是可用的 CPU 版，
            # 不注入 provider 就是纯 CPU，正确性不受影响
            _DML_TRY = "DmlExecutionProvider 不可用"
            return False
        _DML_ON = True
        _DML_TRY = f"dml build={ort.__version__} dev={_dml_device()}"
        return True
    except Exception as e:
        _DML_TRY = f"{type(e).__name__}: {e}"
        return False


def make_engine(threads: int = 0):
    """构造调优后的 RapidOCR（进程内单例）。

    调优五点，对应上面注释里的 827ms → 170ms → 116ms(DML)：
      1. limit_type: max—— 只缩不放，字幕带不再被撑到 736 短边
      2. limit_side_len: 736 —— 上限，防止异常大图拖慢
      3. use_angle_cls: False —— 字幕不会倒着，180 度分类器纯属浪费
      4. **限制每进程的 onnxruntime 线程数**（见 _apply_threads）——这一条
         实测比前三条加起来还重要：不限制时 8 个进程各开 16 个线程，
         128 个线程在 16 核上互相抢，并行加速比只剩 1.7 倍。
      5. DML provider（见 _setup_dml）—— vendor/ort_dml 换包后把 GPU
         拉进来：det 1.8×、rec 1.2×，总体 1.28×。失败自动退回 CPU。
    """
    global _ENG, _ENG_ERR, _LOCK
    if _ENG is not None:
        return _ENG
    if _LOCK is None:
        import threading
        _LOCK = threading.Lock()
    with _LOCK:
        if _ENG is not None:
            return _ENG
        _add_vendor()
        # 顺序要紧：_setup_dml 要抢在 rapidocr 建 session（=import
        # onnxruntime）之前把 DML 版 onnxruntime 插到 sys.path 最前
        _setup_dml()
        try:
            from rapidocr_onnxruntime import RapidOCR
            # 线程数：worker 里 _worker_init 存进 _W_THREADS；
            # 主进程（只做标定）没设，就退回配置值。
            if threads <= 0:
                threads = _W_THREADS or int(C.OCR_THREADS)
            # 线程数只能在建 session 那一刻定，所以必须先 patch 再构造引擎
            ok = _apply_threads(None, threads)
            eng = RapidOCR()
            try:
                eng.use_angle_cls = False
                rs = eng.text_detector.preprocess_op[0]
                rs.limit_type = "max"
                # 子进程（spawn）不继承父进程内存，改 C.OCR_DET_LIMIT 传不进去，
                # 所以这里同时认环境变量——基准测试/调参时才方便逐档试。
                rs.limit_side_len = int(os.environ.get(
                    "FRAMESEEK_DET_LIMIT", C.OCR_DET_LIMIT))
            except Exception:
                pass        # 属性名随版本变，调优失败就退回默认性能，不影响正确性
            _ENG_TUNE.clear()
            _ENG_TUNE.update({"threads": int(threads), "sessions": ok,
                              "provider": "dml" if _DML_ON else "cpu",
                              "dml_note": "" if _DML_ON else _DML_TRY})
            _ENG = eng
            _ENG_ERR = ""
        except Exception as e:
            _ENG = None
            _ENG_ERR = f"{type(e).__name__}: {e}"
    return _ENG


def _apply_threads(eng, threads: int) -> int:
    """给 onnxruntime 的 session 限线程数（0=失败，版本不支持）。

    **为什么必须限**：vendor 里 OrtInferSession 建 session 时没传
    intra_op_num_threads，onnxruntime 的默认线程池 = 逻辑核数（本机 16）。
    单进程无所谓，但我们是多进程并行——8 进程 × 16 线程 = 128 个线程抢
    16 核，实测 120 帧要 75 秒；同样的帧单进程串行只要 15.5 秒，
    **并行反而慢 5 倍**。

    **为什么不能事后设**：onnxruntime 1.29/1.31 的 InferenceSession 都
    没有 set_session_options 方法（实测 hasattr=False），线程数只能在
    创建 session 那一刻定。而 vendor 的 OrtInferSession 是
    `from onnxruntime import InferenceSession` 直接绑名字，patch
    onnxruntime 模块属性没用，得把它自己模块里的那个引用换掉。
    """
    n = int(threads or 0)
    if n <= 0:
        return 0
    try:
        import onnxruntime as ort
        from rapidocr_onnxruntime import utils as _ru
        base = _ru.InferenceSession
        if getattr(base, "_fs_threads", None) == n:
            return 1
        so_tpl = ort.SessionOptions()
        so_tpl.intra_op_num_threads = n
        so_tpl.inter_op_num_threads = 1
        try:
            # 自旋等待在多线程抢核时会把 CPU 空转烧掉，关掉
            so_tpl.add_session_config_entry(
                "session.intra_op.allow_spinning", "0")
        except Exception:
            pass

        class TunedSession(base):
            _fs_threads = n

            def __init__(self, model_path, sess_options=None,
                         providers=None, **kw):
                so = sess_options or ort.SessionOptions()
                so.intra_op_num_threads = n
                so.inter_op_num_threads = 1
                try:
                    so.add_session_config_entry(
                        "session.intra_op.allow_spinning", "0")
                except Exception:
                    pass
                eps = list(providers or [])
                if _DML_ON and not any(
                        "Dml" in str(p) for p in eps):
                    # vendor 传进来的列表里 CPU 在最后；DML 插到最前即优先。
                    # ORT 会按 providers 顺序逐个初始化，成功的生效，
                    # 所以就算 DML init 失败也会自动落到 CPU，安全。
                    eps.insert(0, ("DmlExecutionProvider",
                                   {"device_id": _dml_device()}))
                super().__init__(model_path, sess_options=so,
                                 providers=eps, **kw)

        _ru.InferenceSession = TunedSession
        return 1
    except Exception:
        return 0


# ---------------- 帧流 ----------------
def _frame_stream(video: str, start: float = 0.0, dur: float = 0.0):
    """流式产出字幕带的灰度像素，(t秒, bytes, w, h)。

    `fps=` 固定输出网格是必需的：不加时ffmpeg 输出原生帧率（本片 24fps），
    会比OCR_STEP 网格多送 2.4 倍帧，且推算出的时间戳全是错的。
    """
    probe = media.ffprobe_json(video)
    W = int(probe.get("width") or 1920)
    H = int(probe.get("height") or 1080)
    ow = int(C.OCR_WIDTH)
    oh = max(2, int(round(ow * (H * C.OCR_BAND) / max(1, W))))
    oh -= oh % 2
    step = float(C.OCR_STEP)

    vf = (f"crop=iw:ih*{C.OCR_BAND:.3f}:0:ih*(1-{C.OCR_BAND:.3f}),"
          f"scale={ow}:{oh},format=gray,fps={1.0/step:.4f}")
    cmd = [C.FFMPEG, "-nostdin", "-v", "error", "-hide_banner"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", video]
    if dur > 0:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vf", vf, "-vsync", "0", "-f", "rawvideo", "-"]

    fb = ow * oh
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=fb * 8)
    t = start
    try:
        while True:
            buf = proc.stdout.read(fb)
            if not buf or len(buf) < fb:
                break
            yield t, buf, ow, oh
            t += step
    finally:
        for h in (proc.stdout, proc.stderr):
            try:
                h.close()
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass


class _FrameFeeder:
    """把 _frame_stream 搬到独立生产线程，和主线程的 OCR 真并行。

    为什么需要它：原来主循环 `for t, buf, w, h in _frame_stream(...)` 里，
    每攒满 CHUNK 帧就调一次阻塞的 `pool.imap`。那 24 帧 OCR 的工夫里主线程
    没去读管道，ffmpeg 的 stdout 缓冲一满就自己阻塞——解码和 OCR 变成串行相加
    （实测亮剑一部：解码 278 秒 + OCR 397 秒 ≈ 10 分钟）。

    改成后台线程预读、主线程消费后，管道一直在解，两者重叠。

    用法：

        feeder = _FrameFeeder(video, start, dur)
        feeder.start()
        for t, buf, w, h in feeder:   # 迭代到尾部自动收尾
            ...
        feeder.stop()                # 提前退出（取消/异常）时务必调

    **有界队列**是必须的：帧 960×288 灰度约 276KB，无界的话 4K 长片
    （13680 帧 = 3.8GB）会把内存吃光。有界之后 ffmpeg 会被反压，
    恰好形成天然的流控。

    错误处理：生产线程里 ffmpeg 挂了不抛给主线程（跨线程传异常太脆），
    而是**当作流结束**——队列里那个 None 哨兵之后不再有东西，
    主线程正常收尾并保留已经识别出的台词。这比整段失败更符合预期：
    解码到 80% 崩了，前 80% 的结果仍然有用。
    """

    def __init__(self, video: str, start: float = 0.0, dur: float = 0.0,
                 maxsize: int = PREFETCH):
        self.video = video
        # 字段名不能叫 start —— 会覆盖同名 start() 方法（踩过）。
        self.t0 = start
        self.dur = dur
        self.q: queue.Queue = queue.Queue(maxsize=max(1, int(maxsize)))
        self._stop = threading.Event()
        self._th = None

    def _run(self):
        try:
            for item in _frame_stream(self.video, self.t0, self.dur):
                if self._stop.is_set():
                    break
                # 阻塞 put = 反压：队列满时生产线程自然停下，
                # ffmpeg 那边缓冲满了也就自己等着——这正是我们要的节奏。
                while not self._stop.is_set():
                    try:
                        self.q.put(item, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except Exception:
            # 生产线程的异常不当异常抛（跨线程传太脆），当作流结束。
            # 主线程会看到哨兵后正常收尾，保留已识别的台词。
            pass
        finally:
            try:
                self.q.put(None)
            except Exception:
                pass

    def start(self):
        self._th = threading.Thread(target=self._run, name="ocr-feeder",
                                     daemon=True)
        self._th.start()
        return self

    def stop(self):
        """提前收工（用户按停止 / 主循环异常退出时调用）。

        _frame_stream 挂在 yield 上，它自己的 finally 会 kill 掉 ffmpeg；
        这里只要让生产线程跳出 put 循环、并把队列排空避免它卡在满队列上。
        """
        self._stop.set()
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break

    def __iter__(self):
        while True:
            try:
                item = self.q.get()
            except Exception:
                return
            if item is None:
                return
            yield item

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


# 压制组水印/台标：出现在画面上但不是台词，必须滤掉。
# 这类文本特征很固定（含网址、版权字样、"请访问"之类），且通常横贯整幅画面。
_WATERMARK = re.compile(
    r"(?i)(https?://|www\.|\.com|\.cn\b|请访问|最新电影|高清\d*p|"
    r"影视由|荣誉出品|出品方|发行|字幕组|压制|片源|www|\bcom\b)")
# 版权符号、水印里常见的装饰性重复字符
_NOISE_CH = re.compile(r"^[\W_\-—…·、。，,.!！?？~～*#]+$")

# 纯拉丁字母的片段：多半是画面里的英文招牌/洋酒名（Tequila 之类），
# 对剪辑找台词没用，直接丢。
# 注意必须"一个汉字都没有"才丢——一句正常台词里夹个英文词
# （"他是我的music老师"）很常见，只看整句是不是拉丁会误杀。
_HAS_HAN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_LATIN_ONLY = re.compile(r"^[A-Za-z0-9\s\-_'&.,:!?()]+$")

# 片头演职员表：不是台词，但位置和普通字幕一样（都在画面底部），
# 光看几何分不掉，得看内容。实测《一本漫画闯天涯》识别出
# 「镇衡主演」「李麗麗友情客串」「猛龍特技」「EMI提供主唱齊秦」这类。
#
# 片头演职员表里出现的工种词（含繁体，港片/台片常见）。
# 只是个词表，判定逻辑在 _is_credit() 里——正则做不了这个判断，
# 正则的回溯会让 `.{0,8}?` 贪婪吃掉前缀，最后整句都匹配上（试过）。
_JOB_WORDS = (
    "主演", "領銜", "领衔", "客串", "友情", "出演", "特別出演", "特别出演",
    "導演", "导演", "編劇", "编剧", "監製", "监制", "製片", "制片",
    "攝影", "摄影", "錄音", "录音", "剪輯", "剪辑",
    "配樂", "配乐", "音樂", "音乐", "主題曲", "主题曲", "插曲",
    "特效", "美術", "美术", "造型", "道具", "服裝", "服装",
    "化妝", "化妆", "動作指導", "动作指导", "武打", "武術", "武术",
    "龍虎", "龙虎", "特技", "宣傳", "宣传", "出品", "發行", "发行",
)
# 片头字幕里不带人名、直接以说明词打头的（"原著：亦舒""提供： EMI唱片"）
_CREDIT_HEAD = re.compile(
    r"^(原著|原載|原载|改編|改编|翻譯|翻译|提供|贊助|赞助|"
    r"鳴謝|鸣谢|致謝|致谢|片商|發行商|发行商|特別鳴謝|特别鸣谢)"
    r"[　\s:：]")
# 英文片头字样单独一个 pattern：(?i) 全局标志只能出现在表达式开头，
# 塞在中文分支中间会报 "global flags not at the start of the expression"。
_CREDIT_EN = re.compile(
    r"(?i)(OFFICIAL|PRESENTS|PRODUCTION|DISTRIBUT|PRESENTED|"
    r"STARRING|CAST|DIRECTED|SCREENPLAY|MUSIC|PRODUCED|EDITED)")

# 纯数字/编号：片内 UI、比分、日期、号码牌（《名侦探柯南》实测出
# 「0663 1.00」这种）。允许夹一两个中文单位（"第3集""1080P"）。
_NUMERIC = re.compile(r"^[\d\s.,:：/\\|%\-—*#]+$")

# 台词里常见、但片头字幕绝不会出现的字。用来判断"工种词后面跟的是
# 人名还是句子"——人名短且不含这些字，句子会含。
# 注意：这里面**不能有空格**。加进去之后 `re.search(_SENTI, "張國榮 領銜主演")`
# 会命中分隔人名和工种的那个空格，把 8 字的演员表行全放过去了
# （踩过，漏掉 6 条人工噪声样本）。
_SENTI_CHARS = frozenset(
    "的了  吗呢吧啊呀哦麼么很太最要想会能说讲看买卖去来进多"
    "少个们你我他她它这那什么怎样继续喜名书师品影问谁啥如果所以"
    "个讲是的摄妆乐师".replace(" ", "")
)
_SENTI = re.compile("[" + re.escape("".join(sorted(_SENTI_CHARS))) + "]")
# 工种词后面跟着这些二字词时判为"句尾"而不是"人名"：
# 「繼續進修音乐」「你可喜欢摄影」里的 音乐/摄影 是名词，
# 而「導演　陳可辛」后面是人名，不会是这些词。
_COMMON_TAIL = frozenset((
    "音乐", "音樂", "摄影", "攝影", "化妆", "化妝", "造型", "特技",
    "美术", "美術", "道具", "服装", "服裝", "出品", "发行", "發行",
    "监制", "監製", "制片", "製片", "统筹", "統籌", "宣传", "宣傳",
    "客串", "友情", "主演", "领衔", "領銜", "编剧", "編劇", "导演", "導演",
))


def _is_credit(t: str) -> bool:
    """这条是不是片头演职员表。

    分两层，第二层是兜底：
      第一层（精确）：工种词 + 人名/说明词的结构判定，用于长短句混排。
      第二层（兜底）：**整行 ≤9 字且含工种词**，直接丢。
        这个长度刚好覆盖"人名+空格+工种"的排版
        （「張國榮 領銜主演」「王寶強 友情客串」都是 8 字），
        而台词到这个长度必然含句子成分（"我""你""了""吗"），
        再用 _SENTI 挡一道，就不至于伤到真台词。
        2309 条真实语料里，短到 9 字以内又带工种词的只有
        「我要继续进修音乐」「你可喜欢摄影」这种，会被 _SENTI 拦下。

    第一层为什么要这么讲究：整句搜索工种词会误杀台词里随口提到的
    "摄影/音乐/化妆"。拿库里 2309 条真实语料回归，
    「老太太你不是喜欢摄影吗」「这本书讲的是一个摄影师」
    「买这么多化妆品」「我要继续进修音乐」「你可喜欢摄影」
    会被全部丢掉。所以必须要求"工种词 + 人名"的结构成立。
    （中途试过用正则 `^(.{0,8}?)(工种词)(.{0,4})$`，没用——
      正则回溯让 `.{0,8}?` 贪婪吃掉前缀，等于放行整句。）
    """
    if _CREDIT_HEAD.match(t) or _CREDIT_EN.search(t):
        return True

    # 第二层兜底：短行 + 含工种词。句子成分（"我""要""喜欢"）一律放过——
    # 台词到这个长度一定有，而演员表行只有人名和工种。
    # 阈值 9 是"人名+空格+工种"的长度（实测「張國榮 領銜主演」8 字、
    # 「猛龍特技 李子雄」8 字）。10 字以上留给上面按结构判断。
    if len(t) <= 9 and not re.search(_SENTI, t.replace(" ", "")):
        for j in _JOB_WORDS:
            if j in t:
                return True

    if len(t) > 16:          # 真正的片头行都很短，超长直接放行
        return False
    # 长词优先：「張國榮 領銜主演」要先用"領銜主演"整体去切，
    # 否则先用短词"領銜"命中，尾巴"主演"看着也像人名，判断就歪了。
    for j in sorted(_JOB_WORDS, key=len, reverse=True):
        i = t.find(j)
        if i < 0 or i > 8:            # 没找到，或工种词前面太长 → 是句子不是工种行
            continue
        head = t[:i]
        tail = t[i + len(j):]         # 工种词后面（片头行里是人名）
        if len(tail) > 4:             # 后面太长 → 人名没那么长
            continue
        if re.search(_SENTI, tail):   # 后面是句子成分 → 放行
            continue
        # 工种词后面的字如果本身是常用名词（"音乐""摄影""化妆品"），那多半是
        # 句尾而不是人名。这是区分「導演陳可辛」和「繼續進修音乐」的一条。
        if len(tail) >= 2 and any(
                tail[k:k + 2] in _COMMON_TAIL for k in range(len(tail) - 1)):
            continue
        # 关键：工种词前面得是"人名"而不是"句子"。人名 2~4 字，
        # 且不含任何句子成分（「我可喜欢摄影」的前缀"你可喜欢"就是反例）。
        # 前面太长（≥6 字）同样说明是句尾挂了工种词。
        if len(head) >= 6:
            continue
        if re.search(_SENTI, head):
            continue
        return True
    return False


def is_watermark(s: str) -> bool:
    """判断是不是压制组水印/台标/片头职员表，而不是真台词"""
    t = (s or "").strip()
    if not t:
        return True
    if _WATERMARK.search(t):
        return True
    # 单字、纯符号、或超短无意义的碎片
    if len(t) < int(C.OCR_MIN_LEN):
        return True
    # 同一个字符连续出现 4 次以上（"-----"、""、、），必是噪声
    if re.search(r"(.)\1{3,}", t):
        return True
    # 纯英文/数字：画面招牌、洋酒名之类，不是台词。
# 有汉字就放过——"他是我的music老师"这种夹英文词的正常台词要留。
    if _LATIN_ONLY.match(t) and not _HAS_HAN.search(t):
        return True
    # 纯数字/编号：画面里的比分、时间码、UI 数字
    if _NUMERIC.match(t):
        return True
    # 片头演职员表
    if _is_credit(t):
        return True
    return False


def _tidy(s: str) -> str:
    """清洗 OCR 文本。

    做了三件事（都是实测踩出来的）：
    1. 去空格和标点——字幕文本本身不需要
    2. **剥掉重复次数不同的噪声字符**。实测同一条字幕连续识别会得到
       "二二本影片..." 和 "三三本影片..."，只差首字重复次数，导致去重失效、
       一句话被拆成 20 条。做法：把连续重复的单字压成一个再去比较。
    3. 滤掉水印
    """
    s = (s or "").replace(" ", "").replace("　", "")
    s = (s.replace("，", "").replace("。", "").replace("？", "")
         .replace("！", "").replace("、", "").replace("…", ""))
    s = "".join(ch for ch in s if ch.strip())
    # 连续重复字符压缩：只压 3 次及以上的（那是 OCR 把同一个字重复吐出来的噪声）。
    # 压 2 次会误伤"爸爸/妈妈/看看"这些真实叠词。
    s = re.sub(r"(.)\1{2,}", lambda m: m.group(1), s)
    if len(s) < int(C.OCR_MIN_LEN) or is_watermark(s):
        return ""
    return s[:int(C.OCR_MAX_LEN)] if len(s) > int(C.OCR_MAX_LEN) else s


# ================= 字幕纯净化：几何标定 + 选框 =================
# RapidOCR 认的是"画面里所有字"，但我们要的只是"那一条字幕"。
# 两者差别很实在，实测 8 部片子遇到的干扰有四类：
#   1. 双语片同帧两行（上中/下日）。《云之彼端》实测一帧两个框：
#      「开战已经是箭在弦上了，他们居然还这么卖力」conf=0.91 y=0.61~0.77
#      「決定の真面」                   conf=0.62 y=0.77~0.94
#      老代码 join 一下就变成一句鬼话。
#   2. 片内 UI/数字屏。《名侦探柯南》实测「0663 1.00」conf=0.82。
#   3. 角落台标、压制组水印：几何上贴边，或横贯整幅。
#   4. 片头演职员表：「镇衡主演」「李麗麗友情客串」——位置跟字幕一模一样，
#      几何分不掉，只能靠内容词表。
#
# 选框判据（全部基于实测分布，不是拍脑袋）：
#  - 字幕**水平居中**。上面所有正文样例的框中心都落在 0.38~0.62。
#    台标在角落、画面文字随便在哪，这一刀能干掉大半。
#  - 字幕**不贴边**。OCR_W_MAX=0.88 干掉横贯全宽的水印；
#    OCR_EDGE_MIN=0.03 干掉贴在左右两侧的台标。
#  - 字幕**有稳定的纵向位置**。这是最关键的一条：同一部片子、同一个压制组，
#    字幕基线几乎固定。所以开跑前先标定出这部片子的字幕带，
#    之后只认落在带子里的框。实测各片字幕带中心差异明显
#    （港片 y≈0.77、日漫 y≈0.66~0.7），写死一个窗口会误杀。

class SubtitleProfile:
    """一部片子的"字幕长什么样"：纵向位置 + 字高 + 置信度基线 + 字体指纹。

    标定阶段（宽松门槛，只要求"像个文本框"）统计得到，
    识别阶段（严格门槛）据此把不像字幕的框剔掉。
    """
    __slots__ = ("y0", "y1", "h", "conf", "ok", "n", "kana", "font")

    def __init__(self):
        self.y0 = 0.0            # 字幕带上沿（带内归一化坐标，下同）
        self.y1 = 1.0            # 字幕带下沿
        self.h = 0.22            # 典型字高
        self.conf = float(C.OCR_CONF_MIN)
        self.ok = False# 标定成功了吗（失败就退回宽松模式）
        self.n = 0               # 标定命中框数
        # 标定出的字幕假名率。日语原声片字幕含大量假名（1.0），
        # 中文配音片接近 0。这一项决定"含假名就丢"这条规则开不开——
        # 对前者开就是自伤，对后者开才是剔双语字幕的日文行。
        self.kana = 0.0
        # 字体指纹基准：主字幕的墨密度/笔画宽度中位数。
        # 一部片子的字幕是同一套字体（同一个压制组），所以这两个量在片内
        # 高度稳定；而水印、台标、画面里的招牌字往往是另一套字体，
        # 指纹会对不上。对不上不直接丢（艺术字标题也是台词），但记下来，
        # 供纠错和二次验证时忽略。
        self.font: Dict[str, float] = {}

    def to_dict(self) -> Dict[str, Any]:
        d = {"y0": round(self.y0, 3), "y1": round(self.y1, 3),
             "h": round(self.h, 3), "conf": round(self.conf, 3),
             "kana": round(self.kana, 3),
             "ok": self.ok, "n": self.n}
        if self.font:
            d["font"] = {k: (round(v, 3) if isinstance(v, float) else v)
                         for k, v in self.font.items()}
        return d

    def __repr__(self) -> str:
        return (f"<SubtitleProfile y={self.y0:.2f}~{self.y1:.2f} h={self.h:.2f} "
                f"conf={self.conf:.2f} kana={self.kana:.2f} n={self.n}"
                f"{'' if self.ok else ' 未标定'}>")


_KANA = re.compile(r"[\u3040-\u30ff\uff66-\uff9d]")


def _kana_ratio(s: str) -> float:
    """假名（平假名/片假名/半角假名）占字符数比例。"""
    t = (s or "").strip()
    if not t:
        return 0.0
    return len(_KANA.findall(t)) / len(t)


class _Box:
    """一个通过几何筛选的检测框：几何 + 文本 + 置信度。"""
    __slots__ = ("g", "t", "c")

    def __init__(self, g: Dict[str, float], t: str, c: float):
        self.g = g
        self.t = t
        self.c = c


def _box_geom(box, w: int, h: int) -> Dict[str, float]:
    """把检测框换算成归一化几何量（0~1，相对裁切带）。"""
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    x0, x1 = min(xs) / w, max(xs) / w
    y0, y1 = min(ys) / h, max(ys) / h
    return {"x0": x0, "x1": x1, "y0": y0, "y1": y1,
            "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
            "bw": x1 - x0, "bh": y1 - y0}


def _otsu(gray) -> int:
    """Otsu 阈值：让类间方差最大的那个灰度值。

    为什么不能用 (max+min)/2 这种固定比例：字幕是白字黑描边，而框里
    还裹着背景（柱子、人脸，灰度中等）。(max+min)/2 会把阈值放到背景
    之上，mask 只剩字芯，墨密度被系统性低估（实测"将军令呀！"这种
    大字幕被算成 0.11，正常应该 0.2+）。Otsu 按直方图自分断层，不受
    背景亮度影响。
    """
    import numpy as np
    hist = np.bincount(gray.reshape(-1), minlength=256).astype(np.float64)
    total = float(gray.size)
    omega = np.cumsum(hist)                       # 各类像素数
    mu = np.cumsum(hist * np.arange(256))
    mu_t = mu[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = omega * (total - omega)
        diff = (mu_t - mu)                        # 后类均值×总数
        sigma_b = np.where(denom > 0,
                           (mu * (total - omega) - diff * omega) ** 2
                           / np.where(denom > 0, denom, 1.0), 0.0)
    return int(np.argmax(sigma_b))


def _font_feat_px(img, x0: int, y0: int, x1: int, y1: int) -> Dict[str, float]:
    """从一个检测框的像素区域提取**字体指纹**：墨密度 + 笔画宽度。

    为什么需要它：字高、置信度、位置这些量都只管"这个框像不像字幕"，
    管不了"这个框里的字是不是同一套字体"。一部片子的字幕由同一个压制组
    用同一套字体做成，指纹在片内高度稳定；而水印、台标、画面里的招牌/
    UI 文字往往是另一套字体（更细、更花、墨密度不同）。指纹对不上就记
    下来（`font_dev`），供纠错和二次验证时忽略——不直接丢，因为片内
    偶尔真有艺术字标题，那也是台词。

    两个特征都只用 numpy（Otsu 二值化 + 游程统计），不引新依赖：

    - `ink` 墨密度：文字像素占框区面积比。绝大多数字幕是亮字，
      极性由 Otsu 阈值自动分（哪边像素少哪边是字——汉字墨密度
      0.15~0.4，永远少于背景）。
    - `stroke` 笔画宽度（像素）：二值图逐行统计"字像素游程"长度的
      **均值**（不是中位数——中位数会被大量 1~2px 的抗锯齿毛边主导，
      实测全体退化成 2.0，没有判别力；只统计 ≥2px 的游程，均值才
      反映真实笔画粗细）。黑体 3~5px、宋体 2~3px（960 宽送检口径）。
    """
    import numpy as np
    try:
        crop = np.asarray(img[y0:y1, x0:x1])
    except Exception:
        return {"ink": 0.0, "stroke": 0.0}
    if crop.size < 16:
        return {"ink": 0.0, "stroke": 0.0}
    if int(crop.max()) - int(crop.min()) < 20:      # 没有对比度 = 没有文字
        return {"ink": 0.0, "stroke": 0.0}

    def _stats(mask):
        ink = float(mask.mean())
        # 游程统计向量化：给每行两侧各补一列 0，一次 np.diff 就能把
        # 所有行的"字像素游程"边界全找出来（行内 diff=1 起点、-1 终点，
        # 行间被补的 0 天然切断，不会跨行合并游程）。
        # 原来按行 python 循环跑 12 万次小 numpy 调用，1500 帧多花 17 秒。
        import numpy as np
        h, w = mask.shape
        padded = np.zeros((h, w + 2), dtype=np.int8)
        padded[:, 1:-1] = mask
        d = np.diff(padded, axis=1).reshape(-1)
        starts = np.flatnonzero(d == 1)
        ends = np.flatnonzero(d == -1)
        if not len(starts):
            return ink, 0.0
        runs = (ends - starts)
        runs = runs[runs >= 2]                   # 滤掉抗锯齿毛边
        stroke = float(runs.mean()) if len(runs) else 0.0
        return ink, stroke

    thr = _otsu(crop)
    light = crop > thr
    ink_l, st_l = _stats(light)
    ink_d, st_d = _stats(~light)
    # 汉字墨密度 0.10~0.45，且永远是少数派（字比背景少）。两种极性里
    # 谁更接近正常字幕墨密度就信谁；都不像就取墨密度更接近 0.25 的。
    def _score(ink):
        return 0.0 if 0.10 <= ink <= 0.45 else abs(ink - 0.25)
    if _score(ink_l) <= _score(ink_d):
        return {"ink": round(ink_l, 3), "stroke": round(st_l, 2)}
    return {"ink": round(ink_d, 3), "stroke": round(st_d, 2)}


def _rough(g: Dict[str, float], conf: float) -> bool:
    """宽松判据：标定阶段用，只要"像个正常的文本框"就收。"""
    if conf < float(C.OCR_CALIB_CONF):
        return False
    if g["bw"] < float(C.OCR_W_MIN) * 0.5:
        return False                # 太窄，多半是误检的小点
    if g["bw"] > 0.985 or g["bh"] > 0.9:
        return False                # 几乎铺满整幅 = 边框/水印带，不是字幕
    if g["bh"] < 0.04:
        return False                # 扁成一条线
    return True


def _strict(g: Dict[str, float], conf: float, p: SubtitleProfile) -> bool:
    """严格判据：识别阶段用，这个框到底是不是字幕。"""
    # 1) 置信度
    if conf < max(p.conf * 0.85, float(C.OCR_CONF_MIN)):
        return False
    # 2) 字幕是水平居中的。这一条最狠——台标贴边、画面文字乱飘，都过不了。
    if abs(g["cx"] - 0.5) > float(C.OCR_CX_TOL):
        return False
    # 3) 不贴画面左右边（台标）
    if g["x0"] < float(C.OCR_EDGE_MIN) or g["x1"] > 1.0 - float(C.OCR_EDGE_MIN):
        return False
    # 4) 宽度合理：太窄是噪声，太宽（横贯）是水印
    if not (float(C.OCR_W_MIN) <= g["bw"] <= float(C.OCR_W_MAX)):
        return False
    # 5) 字高接近这部片的典型字高（容忍 0.55~1.9 倍）
    if p.ok:
        hmin, hmax = p.h * 0.55, p.h * 1.9
    else:
        hmin, hmax = float(C.OCR_H_MIN), float(C.OCR_H_MAX)
    if not (hmin <= g["bh"] <= hmax):
        return False
    # 6) 纵向落在字幕带内。这一条专治双语片：日文那行在带外（实测 y0=0.77
    #    超出中文行 0.77 上沿后被判出带），也治画面里其它位置的字。
    if p.ok and not (p.y0 <= g["cy"] <= p.y1):
        return False
    return True


def calibrate(video: str, samples: int = 0, progress=None,
              use_cache: bool = True) -> SubtitleProfile:
    """开跑前标定这部片的字幕带：整片均匀撒 N 个点，每点 OCR 一次，
    看框都堆在哪个纵向区间。

    成本：20 个点× 单帧 OCR(约 170ms) + 20 次 seek 解码(约 0.3s)
         ≈ 10 秒。相对整片 4 分钟可以忽略，但收益很大——
         实测港片字幕带中心 y≈0.77、日漫 y≈0.66，不标定就只能写死一个
         折中窗口，两头都不讨好。

    标定失败（引擎不可用/没抽到框）时返回 ok=False，调用方退回宽松模式，
    不会因为标定失败而什么都识别不出来。
    """
    n = int(samples or C.OCR_CALIB_SAMPLES)
    p = SubtitleProfile()
    if n <= 0:
        return p
    try:
        dur = float((media.video_meta(video) or {}).get("duration") or 0.0)
    except Exception:
        dur = 0.0
    if dur <= 0:
        return p

    probe = media.ffprobe_json(video)
    W = int(probe.get("width") or 1920)
    H = int(probe.get("height") or 1080)

    # 标定缓存：同一剧集各集的字幕带位置/字高/字体几乎相同，
    # 命中就省掉整段 20 点撒点 OCR（实测约 54 秒/部，27 部里能省 26 份）。
    # 键含分辨率与编码，见 _calib_key 的注释。
    if use_cache:
        hit = calib_load(video, W, H)
        if hit is not None:
            return hit

    eng = make_engine()
    if eng is None:
        return p

    ow = int(C.OCR_WIDTH)
    oh = max(2, int(round(ow * (H * C.OCR_BAND) / max(1, W))))
    oh -= oh % 2
    vf = (f"crop=iw:ih*{C.OCR_BAND:.3f}:0:ih*(1-{C.OCR_BAND:.3f}),"
          f"scale={ow}:{oh},format=gray")
    fb = ow * oh

    # 撒点：从 6% 到 92%，跳过片头字幕区（片头字幕位置和正文一样，
    # 但那是职员表，会把标定带往下拽）
    times = [dur * (0.06 + 0.86 * i / max(1, n - 1)) for i in range(n)]
    boxes: List[Dict[str, float]] = []
    scores: List[float] = []
    kanas: List[float] = []
    fonts: List[Dict[str, float]] = []
    for i, t in enumerate(times):
        if progress:
            progress(f"标定字幕位置 {i + 1}/{n}")
        cmd = [C.FFMPEG, "-nostdin", "-v", "error", "-hide_banner",
               "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
               "-vf", vf, "-f", "rawvideo", "-"]
        try:
            raw = media.run(cmd, timeout=60).stdout
        except Exception:
            continue
        if len(raw) < fb:
            continue
        try:
            import numpy as np
            img = np.frombuffer(raw[:fb], dtype=np.uint8).reshape(oh, ow)
            res, _ = eng(np.repeat(img[:, :, None], 3, axis=2))
        except Exception:
            continue
        for x in (res or []):
            if len(x) < 3 or x[1] is None:
                continue
            conf = float(x[2]) if len(x) > 2 else 0.0
            g = _box_geom(x[0], ow, oh)
            if _rough(g, conf):
                # 三元组一起收，后面按"主峰带内"筛完再统计
                boxes.append(g)
                scores.append(conf)
                kanas.append(_kana_ratio(str(x[1])))
                xs = [float(q[0]) for q in x[0]]
                ys = [float(q[1]) for q in x[0]]
                fonts.append(_font_feat_px(
                    img, int(min(xs)) + 1, int(min(ys)) + 1,
                    int(max(xs)), int(max(ys))))

    if not boxes:
        return p

    # 找主字幕行：取 cy 直方图的**最高桶**，再用该桶附近 ±1桶的框算中位数。
    #
    # 为什么不用全体中位数（原来的做法）：双语片一帧两行，上中文下日文，
    # 纵坐标各占一个峰，中位数正好卡在两峰中间，带子被撑成一倍宽，
    # 日文行照样落在带内（实测云之彼端：中文行 cy=0.68、日文行 cy=0.86，
    # 中位数算出 y=0.60~1.00，两行都过）。改取最高桶后带心落在 0.68。
    #
    # 为什么不用"最宽的框"当主行：片内偶尔会有大号标题字（比字幕宽），
    # 会被当成主行。直方图看的是"哪个高度上框最密集"，抗离群值。
    NB = 32
    hist = [0] * NB
    for g in boxes:
        hist[min(NB - 1, int(g["cy"] * NB))] += 1
    peak = max(range(NB), key=lambda i: hist[i])
    lo_b, hi_b = max(0, peak - 1), min(NB, peak + 2)
    sel = [k for k in range(len(boxes))
           if lo_b <= int(boxes[k]["cy"] * NB) < hi_b]
    if len(sel) < 3:
        sel = list(range(len(boxes)))

    band = [boxes[k] for k in sel]
    med_cy = sorted(g["cy"] for g in band)[len(band) // 2]
    med_h = sorted(g["bh"] for g in band)[len(band) // 2]
    # 带宽：中心 ±1.4 倍字高。容得下折行字幕（实测两行 cy 差约 0.9~1.1 倍字高），
    # 又不会把相邻的附加语种行一起圈进来。
    p.y0 = max(0.0, med_cy - med_h * 1.4)
    p.y1 = min(1.0, med_cy + med_h * 1.4)
    p.h = med_h
    # 置信度和假名率**必须只用主峰带内的框**统计，不能用全部。
    # 踩过的坑：双语片里日文框也进了样本，把 p.kana 的中位数抬到 0.15 以上，
    # "主字幕不含假名时，含假名的行判为附加语种"这条规则就整条失效了——
    # 结果日文行原样混进台词（实测「这位最年轻的外部研究员热情满满
    # 最年少の外部研究熟心」）。同理 conf 混进日文框的 0.70 也会拉低基线。
    p.conf = sorted(scores[k] for k in sel)[len(sel) // 2]
    p.kana = sorted(kanas[k] for k in sel)[len(sel) // 2]
    # 字体指纹基准：同样只用主峰带内的框。水印/台标的字体和字幕不同，
    # 混进样本会把基准拽歪，之后真字幕反而全被判"异常"。
    # 同时存 MAD（中位数绝对偏差），_font_dev 用它做尺度——见那边注释。
    if len(sel) >= 4:
        inks = sorted(fonts[k]["ink"] for k in sel)
        strs_ = sorted(fonts[k]["stroke"] for k in sel)
        m = len(sel) // 2
        med_ink, med_str = inks[m], strs_[m]
        p.font = {
            "ink": round(med_ink, 3),
            "ink_mad": round(sorted(abs(v - med_ink) for v in inks)[m], 4),
            "stroke": round(med_str, 2),
            "stroke_mad": round(sorted(abs(v - med_str) for v in strs_)[m], 4),
            "n": len(sel),
        }
    # 样本太少的坏处不只是位置不准：双语片里中文行和日文行会各占一桶，
    # 主峰选错就等于把日文当成了主字幕。所以门槛要 ≥6 个框才认这次标定，
    # 不足就退回宽松模式（只做几何筛选，宁可多留也不整片认不出）。
    p.ok = len(sel) >= 6
    p.n = len(boxes)
    # 标定成功才写缓存（失败不写，避免把坏结果固化下来反复命中）。
    # 放在函数末尾而不是 p.ok 判定里，是因为整段计算后 p 可能仍不可用，
    # 这里再确认一次更清楚。
    if use_cache:
        calib_save(video, W, H, p)
    return p


# ---------------- 标定缓存 ----------------
# 为什么值得做：标定是**每部片必付的固定税**——20 个撒点，每次一次
# `-ss seek + 单帧 OCR`，实测约 54 秒。而同一剧集的字幕带位置/字高/字体
# 是**整季一样的**（同一压制组、同一版式），26~29 集标定结果几乎相同。
# 27 部 × 54 秒 ≈ 24 分钟纯重复劳动，缓存后只付一次。
#
# 键怎么设计（这里最容易做错）：
#   键 = 库目录 + 源分辨率宽高 + OCR_WIDTH/OCR_BAND
# 而不是文件 md5 —— 那样每部片都是独立键，缓存等于没用。
# 也不是单纯的目录 —— 同目录下可能有不同分辨率的片，带位置会差。
# 分辨率进键，是因为字幕带的**归一化**坐标依赖裁带比例：
# 4:3（3840x2880）和 16:9（1920x1080）裁同样30% 带，字高归一化后不同。
def _calib_cache_dir(video: str) -> str:
    """从视频路径往上找它所属素材库的 .frameseek 目录。

    calibrate() 只拿到文件路径、拿不到库根目录，而缓存要按库隔离
    （不同库的片不该共享一份标定）。这里从文件所在目录逐级上溯，
    找到第一个含 .frameseek 的目录就用它。找不到就返回空串——
    调用方据此跳过缓存，功能不受影响。
    """
    try:
        d = os.path.dirname(os.path.abspath(video))
    except Exception:
        return ""
    root = os.path.dirname(d) if d else ""
    for _ in range(4):                      # 最多上溯 4 层，够覆盖库/剧/文件
        if not d or d == root:
            break
        cand = os.path.join(d, C.CACHE_DIR)
        if os.path.isdir(cand):
            return cand
        d, root = os.path.dirname(d), d
    return ""


def _calib_key(video: str, W: int, H: int) -> str:
    codec = ""
    try:
        # ffprobe_json 返回的是完整结构：streams[0].codec_name，
        # 不是平铺的 codec 字段（写错过一次，键会退化成 "?"，
        # 于是所有片的键都一样，缓存失去区分度）。
        probe = media.ffprobe_json(video) or {}
        for s in (probe.get("streams") or []):
            if isinstance(s, dict) and s.get("codec_type") == "video":
                codec = str(s.get("codec_name") or "")
                break
        if not codec and isinstance(probe.get("video"), dict):
            codec = str(probe["video"].get("codec_name") or "")
    except Exception:
        codec = ""
    # 编码格式进键：H.264 与 HEVC 的字幕压制位置常差几行，
    # 混用会让标定带偏，直接导致整片认不出（比慢更糟）。
    # 取不到编码就退化成空串——仍按分辨率区分，不会退化成"全库共用一个键"。
    key = "%dx%d|%s|%d|%.3f|%s" % (int(W), int(H), codec or "-",
                                   int(C.OCR_WIDTH), float(C.OCR_BAND),
                                   C.OCR_STEP)
    # 键是要当**文件名**用的：Windows 保留字符 | < > : " / \ ? * 一律
    # 不能出现（踩过："|" 直接 PermissionError，缓存永远写不出来，
    # 而 calib_save 的 try/except 把异常吞了，表面看只是"没生效"）。
    for ch in '|<>:"/\\?*':
        key = key.replace(ch, "_")
    return key


def _calib_cache_file(video: str) -> str:
    cd = _calib_cache_dir(video)
    if not cd:
        return ""
    d = os.path.join(cd, "calib")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return ""
    return d


def calib_load(video: str, W: int, H: int) -> Optional["SubtitleProfile"]:
    """读标定缓存；没有/损坏/标定本身失败过，都返回 None（走完整标定）。"""
    d = _calib_cache_file(video)
    if not d:
        return None
    fp = os.path.join(d, _calib_key(video, W, H) + ".json")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            d2 = json.load(f)
    except Exception:
        return None
    # ok=False 的缓存没有复用价值（等于没标定），直接当没有。
    if not d2.get("ok"):
        return None
    p = SubtitleProfile()
    p.y0 = float(d2.get("y0") or 0.0)
    p.y1 = float(d2.get("y1") or 1.0)
    p.h = float(d2.get("h") or 0.22)
    p.conf = float(d2.get("conf") or C.OCR_CONF_MIN)
    p.kana = float(d2.get("kana") or 0.0)
    p.n = int(d2.get("n") or 0)
    p.ok = True
    p.font = d2.get("font") or {}
    return p


def calib_save(video: str, W: int, H: int, p: "SubtitleProfile") -> None:
    """把成功的标定写进缓存。失败（ok=False）不写——避免把坏结果固化。"""
    if p is None or not p.ok:
        return
    d = _calib_cache_file(video)
    if not d:
        return
    try:
        payload = p.to_dict()
        payload["saved_at"] = int(time.time())
        fp = os.path.join(d, _calib_key(video, W, H) + ".json")
        # 原子写：多个 worker 可能同时写同一个键（并发扫同一部剧），
        # 先写临时文件再 rename，避免读到半个 JSON。
        tmp = fp + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, fp)
    except Exception as e:
        # 不静默：缓存写不出来是个静默故障（功能降级成每部都标定，
        # 用户只会觉得"怎么没变快"，查不到原因）。打一行日志即可定位。
        try:
            print(f"[ocr] 标定缓存写入失败({type(e).__name__}: {e})：{fp}")
        except Exception:
            pass


def calib_cache_clear(video: str) -> int:
    """清掉某素材库的标定缓存，返回删掉几个文件。

    参数改了 OCR_WIDTH / OCR_BAND / OCR_STEP 之后要调它——键变了，
    旧缓存自然失效，但留着是垃圾。
    """
    d = _calib_cache_file(video)
    if not d or not os.path.isdir(d):
        return 0
    n = 0
    try:
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                try:
                    os.remove(os.path.join(d, fn))
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


def _prof() -> "SubtitleProfile":
    """拿当前画像；没有就造一个未标定的（几何筛选走宽松档）。"""
    global _PROFILE
    if _PROFILE is None:
        _PROFILE = SubtitleProfile()
    return _PROFILE


def select_subtitle(res, w: int, h: int, p: SubtitleProfile,
                    want_box: bool = False, img=None):
    """从一帧的多个检测框里，挑出"那一条字幕"。

    返回清洗后的文本；挑不出来返回 ""。
    这是"只要字幕文字"这个需求的落点——原来这里是 join 全部框。

    `want_box=True` 时返回 (text, proof)，proof 是这条字幕的**方框凭证**：
    它由哪些检测框拼成、在画面哪个位置、字高多少、置信度多少、字宽比多少、
    字体指纹是什么。没挑到框时 proof 为 None——文本可能来自别的路径
    （SRT/内嵌字幕），没有画面证据，这样下游能区分"有框的台词"和
    "没框的台词"。

    方框凭证存在的意义：让入库的每一行都能回答"画面上真有这么一个框吗"。
    光看文本无法判断，但方框能——而且是可复核的（见 core/verify.py）。

    `img` 是整幅字幕带灰度图（numpy），只有要算字体指纹时才需要传入。
    """
    if not res:
        return ("", None) if want_box else ""
    keep: List["_Box"] = []
    for x in res:
        if len(x) < 3 or x[1] is None:
            continue
        conf = float(x[2]) if len(x) > 2 else 0.0
        try:
            g = _box_geom(x[0], w, h)
        except Exception:
            continue
        if _strict(g, conf, p):
            keep.append(_Box(g=g, t=str(x[1]), c=conf))
    if not keep:
        return ("", None) if want_box else ""

    # 同一行可能被切成多个框（字间距大时检测器会分），纵向重叠的合并成一行；
    # 纵向分开的另起一行。
    keep.sort(key=lambda b: (b.g["cy"], b.g["x0"]))
    rows: List[List[_Box]] = []
    for b in keep:
        for r in rows:
            ry0 = min(k.g["y0"] for k in r)
            ry1 = max(k.g["y1"] for k in r)
            # 纵向重叠超过一半字高算同一行
            ov = min(ry1, b.g["y1"]) - max(ry0, b.g["y0"])
            if ov > b.g["bh"] * 0.5:
                r.append(b)
                break
        else:
            rows.append([b])

    if len(rows) > 1:
        # 一帧出现多行，只有两种可能，处理方式完全相反：
        #   A) 长句折行 —— 两行都是同一部片的主字幕，**都要留**
        #   B) 双语片 —— 上面中文译文、下面日文原声，**只留主字幕**
        #
        # 判据一（独立、优先）：**混排即附加语种**。
        # 双语片的定义就是同帧两行语言不同。只要这一帧里有的行是纯中文
        # （假名率≈0）、有的行含假名，那含假名的行必然是附加语种——
        # 不管它的字号和置信度像不像字幕。
        # 这条不依赖标定，标定样本不足时（整片只抽到 8 框，ok=False）
        # 也能生效。实测云之彼端「这位最年轻的外部研究员热情满满
        # 最年少の外部研究熟心」就是靠这条切干净的。
        # 日语原声片不会误伤：那种片子两行都是日文，假名率都高，
        # 不存在"纯中文行"做对照。
        #
        # 阈值为什么是 0.04 不是 0.15：日文行往往是「漢語 kana 混排」
        # （实测「最年少の外部研究熟心」11 字只有 1 个假名 = 0.09），
        # 定 0.15 就漏了。纯中文行假名率恒为 0，有 has_pure_cn 兜底，
        # 阈值可以压得很低——"含假名"本身就是异常信号。
        kana_ratios = [_kana_ratio("".join(
            b.t for b in sorted(r, key=lambda b: b.g["x0"]))) for r in rows]
        has_pure_cn = any(k < 0.02 for k in kana_ratios)
        # 判据二：字号更小 **且** 置信度更低，两个条件同时成立。
        # 单条都不够——只看字号会误杀折行里字号略小的行，
        # 只看置信度会把折行第二行（也常因模糊而低分）误杀。
        # 实测：云之彼端(日文行) 字高比 0.75、置信度比 0.82 → 两个都低于阈值；
        #       天气之子(折行) 字高比 1.20、置信度比 0.90 → 都不低于，全部保留。
        # 比较时取每行**最大**字高（不是第一个框的）：一行可能被切成多个框，
        # 用第一个框会低估字号，导致日文行被判成"不小"而留下（实测踩过）。
        h_max = max(b.g["bh"] for r in rows for b in r)
        c_max = max(b.c for r in rows for b in r)
        kept: List[List[_Box]] = []
        for ri, r in enumerate(rows):
            txt = "".join(b.t for b in sorted(r, key=lambda b: b.g["x0"]))
            extra_lang = has_pure_cn and kana_ratios[ri] > 0.04
            h_row = max(b.g["bh"] for b in r)
            c_row = max(b.c for b in r)
            smaller = h_max > 0 and h_row / h_max < float(C.OCR_H_RATIO_DROP)
            weaker = c_max > 0 and c_row / c_max < float(C.OCR_CONF_RATIO_DROP)
            if extra_lang or (smaller and weaker):
                continue
            kept.append(r)
        rows = kept or rows[:1]

        # 按纵坐标从上到下排序再拼。这里曾经漏了排序，导致折行字幕两行的
        # 先后是乱的（实测天气之子输出成「-但要是我回去了--站在太阳的死角」，
        # 和画面上下的顺序正好相反）。
        rows.sort(key=lambda r: min(b.g["y0"] for b in r))

    if len(rows) > int(C.OCR_MAX_SUB_LINES):
        rows = rows[:max(1, int(C.OCR_MAX_SUB_LINES))]

    parts: List[str] = []
    picked: List[_Box] = []
    for r in rows:
        line = sorted(r, key=lambda b: b.g["x0"])
        parts.extend(b.t for b in line)
        picked.extend(line)
    text = _tidy("".join(parts))
    if not want_box:
        return text
    return text, _proof(picked, text, w, h, p, img=img)


def _font_dev(font: Dict[str, float], base: Dict[str, float]) -> float:
    """这条字幕的字体指纹离这部片的基准有多远（0 = 正中）。

    尺度用 **MAD**（中位数绝对偏差）而不是固定比例：指纹在片内的自然
    波动本来就不小（淡入淡出、画面明暗、压缩噪声），固定比例会把一半
    正常行都判成异常（实测固定比例下 42% 的行超阈值，徽标满天飞，
    "异常"就不再是异常）。MAD 随分布宽度自适应——分布紧时敏感，
    分布宽时放松，只抓真正的离群点。

    没有基准（标定样本不足）时返回 0——不冤枉任何一条。

    这个量的用途是**纠错和二次验证时忽略**：台词页面上字体偏离大的行
    可以标出来让人先怀疑；方框校验时它的证据权重降档。不拿来直接删行——
    片内真有艺术字标题，删了就丢台词。
    """
    if not base or not font:
        return 0.0
    d = 0.0
    for key, floor in (("ink", 0.03), ("stroke", 0.8)):
        med = float(base.get(key) or 0.0)
        mad = float(base.get(key + "_mad") or 0.0)
        if med <= 0:
            continue
        # 至少给 1.5 倍 MAD 或一个绝对下限，取大者——防止基准极稳时
        # 一点点抖动就被放大成巨大偏离。
        scale = max(1.5 * mad, floor)
        d = max(d, abs(float(font.get(key) or 0.0) - med) / scale)
    return round(d, 3)


def _proof(boxes: List[_Box], text: str, w: int, h: int,
           p: SubtitleProfile, img=None) -> Dict[str, Any]:
    """把选中的一组检测框压成一条"方框凭证"。

    字段全部是归一化坐标（相对字幕带裁切区，0~1），可以直接存库、
    也可以直接拿去和下一次识别同一时刻的结果比对。

    `ar`（字宽比）是这里最有判别力的一个量：真实字幕是方形字，
    每字宽度≈字高，所以 框宽像素/(字数×字高像素) ≈ 1。实测正样本
    P05~P95 = 0.73~1.22；识别串行/字形粘连时会飙到 2 以上。

    `font` 子对象是字体指纹（墨密度 + 笔画宽度），`font_dev` 是它相对
    本部片基准的偏离度——见 _font_dev。传入 img（整幅字幕带灰度图）
    才算得出来，没有 img（比如走 SRT 路径）就为空。
    """
    x0 = min(b.g["x0"] for b in boxes)
    x1 = max(b.g["x1"] for b in boxes)
    y0 = min(b.g["y0"] for b in boxes)
    y1 = max(b.g["y1"] for b in boxes)
    bh = max(b.g["bh"] for b in boxes)
    n = len([c for c in text if c.strip()])
    # 归一化坐标的宽高比不是 1:1（裁切带是 960×162，宽是高的 5.93 倍），
    # 算字宽比必须先换算成像素，否则正样本会聚在 0.17 而不是 1.0 附近。
    ar = ((x1 - x0) * w) / (n * bh * h) if n > 0 and bh > 0 and h > 0 else 0.0
    font: Dict[str, float] = {}
    if img is not None:
        try:
            px = int(x0 * w) + 1
            py = int(y0 * h) + 1
            pw = max(2, int(round((x1 - x0) * w)))
            ph = max(2, int(round((y1 - y0) * h)))
            font = _font_feat_px(img, px, py, px + pw, py + ph)
        except Exception:
            font = {}
    d: Dict[str, Any] = {
        "x0": round(x0, 4), "y0": round(y0, 4),
        "x1": round(x1, 4), "y1": round(y1, 4),
        "cx": round((x0 + x1) / 2, 4), "cy": round((y0 + y1) / 2, 4),
        "bw": round(x1 - x0, 4), "bh": round(bh, 4),
        "conf": round(min(b.c for b in boxes), 4),
        "nbox": len(boxes), "nchar": n,
        "ar": round(ar, 3),
        "h_ratio": round(bh / p.h, 3) if (p.ok and p.h > 0) else 0.0,
    }
    if font:
        # px_h：字高占整幅画面的比例（bh 是相对 30% 裁切带的，乘回去才对上
        # 原片分辨率，跨视频可比）。存证用，让人一眼看出"这片字幕多大"。
        d["font"] = {"ink": font.get("ink", 0.0),
                     "stroke": font.get("stroke", 0.0),
                     "px_h": round(bh * float(C.OCR_BAND), 4)}
        d["font_dev"] = _font_dev(font, p.font)
    return d


# ---------------- 并行 worker ----------------
def _worker_init(threads: int, profile: SubtitleProfile = None):
    """子进程初始化：每进程限制线程数，避免 N 个进程互相抢满 16 核。

    字幕位置画像也在这里灌进去：spawn 出来的子进程不继承父进程内存，
    不显式传就会退回未标定状态（几何筛选变松，双语片又开始混行）。
    """
    _add_vendor()
    global _PROFILE, _W_THREADS
    _W_THREADS = int(threads or C.OCR_THREADS)
    # 让每个 worker 把自己实际生效的线程数和 provider 打出来，方便诊断并行异常
    if make_engine() is not None:
        print(f"[worker pid={os.getpid()}] threads={_W_THREADS} "
              f"sessions={_ENG_TUNE.get('sessions')} "
              f"provider={_ENG_TUNE.get('provider')}", flush=True)
    if profile is not None:
        _PROFILE = profile
    try:
        import cv2
        cv2.setNumThreads(1)
    except Exception:
        pass
    os.environ.setdefault("OMP_NUM_THREADS", str(max(1, threads)))


def _worker_ocr(item):
    """子进程任务：识别一帧原始灰度字节。item = (idx, bytes, w, h)

    返回 (idx, text, proof)。proof 是方框凭证（dict）或 None——
    非净化模式下没有凭证（没有"选出字幕框"这一步）。
    """
    import numpy as np
    idx, buf, w, h = item
    eng = make_engine()
    if eng is None:
        return idx, "", None
    try:
        img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w)
        rgb = np.repeat(img[:, :, None], 3, axis=2)
        res, _ = eng(rgb)
        if not res:
            return idx, "", None
        if not C.OCR_PURE:
            # 关掉净化 = 老行为，一帧所有框直接拼一起
            return idx, _tidy("".join(str(x[1]) for x in res if len(x) > 1 and x[1])), None
        return (idx,) + tuple(select_subtitle(res, w, h, _prof(),
                                              want_box=True, img=img))
    except Exception:
        return idx, "", None


def _run_batch(items, pool, threads: int):
    """跑一批帧，返回 [(t, text, proof)]，按时间排序。pool=None 时单进程"""
    if not items:
        return []
    if pool is None:
        _worker_init(threads, _PROFILE)
        res = [_worker_ocr((i, b, w, h)) for i, (_, b, w, h) in
               enumerate(items)]
    else:
        res = list(pool.imap(_worker_ocr,
                             [(i, b, w, h) for i, (_, b, w, h) in
                              enumerate(items)], chunksize=2))
    out = [(items[i][0], t, pf) for i, t, pf in res]
    out.sort(key=lambda x: x[0])
    return out


def _same_place(pf: Dict[str, Any], boxes: List[Dict[str, Any]],
                tol: float = None) -> bool:
    """这一帧的框，是不是和已收集的框在同一个位置。

    这是"方框凭证"真正开始起作用的地方：`_similar()` 靠字符集重合度判断
    "是不是同一句"，够松——相邻两句只要用词相近就会被判成同一句
    （实测「祝兄你隔几天就一百几十两地输」后面紧跟「输光了又走来叫救命」，
    两句共享「输/了/来」等字，重合度够高，结果后一句的帧被算进了前一句的凭证，
    覆盖率一度算出 150%）。

    但字幕的位置是死的：同一部片里，同一句话在连续几帧里只可能待在同一个
    地方。所以判据从"文本像不像"换成"框在不在原地"——位置对不上就说明
    这是另一句话的框，不能当成本句的证据。

    `boxes` 为空时（还没有参照）一律接受，第一帧总要有个起点。
    """
    if not boxes:
        return True
    if tol is None:
        tol = float(C.OCR_SAME_PLACE_TOL)
    for b in boxes:
        if abs(pf["cx"] - b["cx"]) <= tol and \
                abs(pf["cy"] - b["cy"]) <= tol * float(C.OCR_SAME_CY_TOL):
            return True
    return False


def _fold_boxes(boxes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把同一条字幕在多帧里的方框凭证压成一条。

    取**中位数**而不是均值或众数：OCR 偶尔会把某帧的框套歪一点
    （画面在动、字在淡入淡出），均值会被这几个坏值拽偏。
    中位数只关心"多数帧都同意的那个框"在哪。

    `hits` 记录有几个帧支持这条——只有 1 帧支持的话，说明这条字幕
    是从一堆相似文本里被误并进来的，方框校验会拿它开刀。
    跨帧一致性 `drift` 是同一时刻不同帧之间的最大几何偏差（归一化），
    实测真字幕 P95 < 0.05，飘到 0.1 以上说明框在乱跳。
    """
    if not boxes:
        return {}
    def med(key):
        v = sorted(b[key] for b in boxes)
        return v[len(v) // 2]
    cx, cy = med("cx"), med("cy")
    xs = [b["x0"] for b in boxes] + [b["x1"] for b in boxes]
    ys = [b["y0"] for b in boxes] + [b["y1"] for b in boxes]
    x0 = min(xs)
    y0 = min(ys)
    drift = 0.0
    for b in boxes:
        # 每一帧的框心离"共识框心"有多远。真实字幕常驻时每帧都该几乎重合，
        # 差得多说明这一行不是同一块字幕在反复出现，而是把不相干的文本并进来了。
        drift = max(drift, abs(b["cx"] - cx) + abs(b["cy"] - cy))
    out = {
        "x0": round(x0, 4), "y0": round(y0, 4),
        "x1": round(max(xs), 4), "y1": round(max(ys), 4),
        "cx": cx, "cy": cy,
        "bw": round(max(xs) - x0, 4), "bh": med("bh"),
        "conf": round(med("conf"), 4),
        "nbox": med("nbox"), "nchar": med("nchar"),
        "ar": round(med("ar"), 3),
        "h_ratio": round(med("h_ratio"), 3),
        "hits": len(boxes),
        "drift": round(drift, 4),
    }
    # 字体指纹也折进去：同一条字幕在多帧里的墨密度/笔画宽度取中位数
    # （个别帧字在淡入淡出、边缘发虚，指纹会抖，中位数扛得住）。
    # font_dev 取**最大**而不是中位数——偏离是异常信号，只要有一帧明显
    # 不对劲就该让人看见（比如这句有 8 帧正常、2 帧窜进了水印的框）。
    fs = [b["font"] for b in boxes if isinstance(b.get("font"), dict)]
    if fs:
        out["font"] = {
            "ink": round(sorted(f["ink"] for f in fs)[len(fs) // 2], 3),
            "stroke": round(sorted(f["stroke"] for f in fs)
                            [len(fs) // 2], 2),
            "px_h": round(sorted(f.get("px_h", 0.0) for f in fs)
                          [len(fs) // 2], 4),
        }
        devs = [b["font_dev"] for b in boxes
                if isinstance(b.get("font_dev"), (int, float))]
        if devs:
            out["font_dev"] = round(max(devs), 3)
    return out


def _similar(a: str, b: str) -> bool:
    """两条文本是否"其实是同一句"。

    不能只比相等——实测同一条字幕在0.5s 网格上反复识别时，OCR 会有
    偶发漏字/错字（"二二本影片"vs "三本影片"），纯相等判断会把一句话
    拆成十几条。判据：字符集合交集占较小者的比例够高就算同一条。
    """
    if a == b:
        return True
    if not a or not b:
        return False
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    return inter >= 0.75 * min(len(sa), len(sb))


def _pick_best(variants: Dict[str, int]) -> str:
    """同一条字幕的多个识别变体里挑一个最靠谱的当整句文本。

    为什么要投票：0.2s 网格下一句台词会被命中 8~15 次，OCR 偶发漏字/错字
    会产生变体（"他在思念什么" / "他在思什么"）。如果像以前那样拿**第一帧**
    的识别结果当整句，第一帧正好漏字这句就永远缺字。正确文本出现次数最多，
    按计数取。

    平票时取**短**的那个：漏字只会产生更短的变体，而多出来的字往往是
    上一句的尾字或画面元素串入（实测 0.2s 下出现过"祗可惜同床异梦"、
    "哗八个一起上吊"——都是不该有的前缀）。两个变体出现次数一样多时，
    更长的那个有更高概率带着串入噪声。
    """
    if not variants:
        return ""
    if len(variants) == 1:
        return next(iter(variants))
    return sorted(variants.items(),
                  key=lambda kv: (-kv[1], len(kv[0])))[0][0]


def structured(fn):
    """把 progress 回调包成"要结构化 dict"的模式。

    ocr_lines 默认发字符串（老调用方都在用）；页面要帧级完成度，
    就用这个包一层：ocr_lines(video, progress=ocr_mod.structured(my_fn))
    """
    def wrapper(msg):
        return fn(msg)
    wrapper.want_dict = True
    wrapper.raw = fn
    return wrapper


def ocr_lines(video: str, start: float = 0.0, dur: float = 0.0,
              progress=None, cancel=None) -> Tuple[List[Dict[str, Any]], str]:
    """OCR 识别台词，返回 (台词列表, 错误信息)。

    cancel 是无参可调用对象，返回 True 则提前收工。
    progress 收到字符串；若用 structured() 包装，则收到带帧级完成度的 dict。
    """
    global _PROFILE
    if not C.OCR_ENABLED:
        return [], "OCR 已关闭"

    # 整片模式（dur<=0）下帧完成度的分母是未知的，但用户要的就是这个百分比。
    # 这里探一次片长（ffprobe 读容器头，约几十毫秒），换来能算的百分比。
    if dur <= 0:
        try:
            meta = media.video_meta(video)
            if meta and meta.get("duration"):
                dur = float(meta["duration"])
        except Exception:
            pass
        if start > 0 and dur > 0:
            dur = max(0.0, dur - start)

    t_all = time.time()
    workers = int(C.OCR_WORKERS)
    threads = int(C.OCR_THREADS)
    pool = None
    rows: List[Dict[str, Any]] = []
    cur_text = ""
    cur_start = 0.0
    cur_boxes: List[Dict[str, Any]] = []
    cur_end = 0.0
    cur_variants: Dict[str, int] = {}   # 同句的识别变体计数（投票择优用）
    n_seen = n_batch = 0
    last_push = time.time()
    buf_items: List[Tuple[float, bytes, int, int]] = []
    was_cancelled = False

    def _close():
        """把当前这句收进结果表，文本用投票择优后的版本。"""
        nonlocal cur_text, cur_start, cur_boxes, cur_end, cur_variants
        if not cur_text:
            return
        rows.append({"start": cur_start,
                     "end": max(cur_end, cur_start + 0.3),
                     "text": _pick_best(cur_variants) or cur_text,
                     "box": _fold_boxes(cur_boxes)})
        cur_text = ""
        cur_boxes = []
        cur_variants = {}

    def _emit(got):
        """把一批识别结果并进台词表。

        同一句话的多个识别变体按出现次数投票（见 _pick_best）——0.2s 网格下
        一句被命中十几次，拿第一帧的文本当整句会把它偶发的漏字固定下来。

        同时攒**方框凭证**：一条字幕常驻 1.5~3 秒，在 0.2s 网格上会被命中
        8~15 次，每次都给出同一个位置的一个框。这些框的几何必须互相吻合
        （这是校验的基础）；只有一帧支持、位置还飘忽的行，就是可疑的。

        `cur_end` 只在**真的检到框**时推进（认歪的那几帧 txt 非空但 pf 为 None，
        不能算"这句还在"）。字幕淡出/转场时中间会空掉几帧，此时 end 停在
        最后一次有框的时刻——这正是方框能告诉我们的信息：这条字幕实际可见到哪。
        """
        nonlocal cur_text, cur_start, cur_boxes, cur_end, cur_variants
        for t, txt, pf in got:
            if not txt:
                continue
            if _similar(txt, cur_text):
                # 同一条字幕还在。这一帧的框要收不收，取决于**位置对不对得上**——
                # 这正是方框凭证的价值。`_similar` 是字符集重合度判据，
                # 够松：相邻两句用词相近时也会被判成"同一句"（实测覆盖率一度
                # 算出 120%~150%，就是前后句的帧串进来了）。但字幕位置是死的：
                # 同一句话在同一时刻只可能出现在一个地方，框心飘了就说明
                # 这帧认的是另一句，不能算作本句的证据。
                if pf and _same_place(pf, cur_boxes):
                    cur_boxes.append(pf)
                    cur_end = t
                cur_variants[txt] = cur_variants.get(txt, 0) + 1
                continue
            _close()
            cur_text = txt
            cur_start = t
            cur_end = t
            cur_boxes = [pf] if pf else []
            cur_variants = {txt: 1}

    def _flush():
        nonlocal buf_items, n_batch
        if not buf_items:
            return
        _emit(_run_batch(buf_items, pool, threads))
        n_batch += 1
        buf_items = []

    try:
        # 先花十几秒标定这部片的字幕位置。标定结果决定几何筛选的松紧，
        # 拿不到就退回宽松档（ok=False），不影响出结果。
        if C.OCR_PURE:
            # 标定阶段发的是字符串进度，调用方 structured() 的包装器
            # 原样转发即可（它对 str 和 dict 都兼容）。
            prof = calibrate(video, progress=progress,
                             use_cache=bool(C.OCR_CALIB_CACHE))
            _PROFILE = prof
            if progress:
                d = prof.to_dict()
                progress({"pos": 0, "total": dur, "frames": 0,
                          "frames_total": 0, "batches": 0, "lines": 0,
                          "eta_min": 0.0, "calib": True,
                          "text": (f"字幕位置标定{'完成' if prof.ok else '跳过'}"
                                   f" · y {d['y0']}~{d['y1']}"
                                   f" · 字高 {d['h']}"
                                   f" · 样本 {d['n']} 框")})
        if workers > 1:
            ctx = mp.get_context("spawn")
            pool = ctx.Pool(processes=workers, initializer=_worker_init,
                            initargs=(threads, _PROFILE))
        else:
            _worker_init(threads, _PROFILE)
        if make_engine() is None:
            return [], f"OCR 引擎不可用：{_ENG_ERR}"

        # 帧总数 = 时长 / 步长，用来算"帧完成度"百分比。
        # dur>0 时直接算；整片模式已在函数开头探测过片长，所以这里基本总是 >0。
        n_total = int(max(1, dur / float(C.OCR_STEP))) if dur > 0 else 0

        # 是否发结构化 dict：调用方用 ocr_mod.structured(fn) 包装即可。
        # （不能用 isinstance(progress, dict) 判断——那是在看回调对象本身，
        #   不是它收到的参数，永远为 False。这个坑我踩了一次。）
        want_dict = getattr(progress, "want_dict", False)

        # 解码与 OCR 并行：生产线程后台解帧，主线程只消费队列。
        # 队列有界（见 _FrameFeeder），满了会反压 ffmpeg，不会吃内存。
        #
        # 实测结论（亮剑 26.mp4 整部，3840x2880 HEVC）：
        #   并行 935 秒 vs 串行 624 秒 —— **并行反而更慢**，已默认关闭。
        # 原因不是解码带不动（管道单独跑 46 帧/秒，是 OCR 的两倍），而是
        # 解码和 OCR 抢的是**同一批 CPU + 内存带宽**：4K 解码要搬运 1057GB
        # 像素，DML 的 4 进程×2 线程已经把 16 核占掉 8 核，再插一个满负荷
        # 的解码线程进来，两边都变慢，净效果为负。
        # 代码保留但默认走串行（env FRAMESEEK_OCR_PARALLEL=1 可开），
        # 因为换 DML 的瓶颈分布（GPU 占用率低）或换更快的解码器后可能反转。
        if os.environ.get("FRAMESEEK_OCR_PARALLEL") == "1":
            feeder = _FrameFeeder(video, start, dur).start()
            frames = iter(feeder)
        else:
            feeder = None
            frames = _frame_stream(video, start, dur)
        try:
            for t, buf, w, h in frames:
                buf_items.append((t, buf, w, h))
                n_seen += 1
                # 进度推送节流：每 8 帧或每 2 秒推一次就够，再密没有意义
                # （前端只显示一个百分比，人眼看不出区别，但事件量会翻几倍）。
                now = time.time()
                if len(buf_items) >= CHUNK or (
                        n_seen % 8 == 0 and now - last_push >= 2.0):
                    _flush()
                    last_push = time.time()
                    if progress:
                        el = time.time() - t_all
                        pos = t - start
                        rate = pos / el if el > 0 else 0
                        if n_total <= 0 and rate > 0:
                            # 用"已扫时间 / 实际耗时"外推总长，随进度逐步收敛
                            n_total = int(pos / rate / float(C.OCR_STEP)) or 0
                        left = ((dur - pos) / rate / 60.0
                                if rate and dur > 0 else 0.0)
                        if want_dict:
                            progress({
                                "pos": pos, "total": dur, "frames": n_seen,
                                "frames_total": n_total, "batches": n_batch,
                                "lines": len(rows), "eta_min": max(0.0, left),
                                "text": (f"OCR {pos:.0f}/"
                                         f"{'?' if dur <= 0 else format(dur, '.0f')}s"
                                         f" · {len(rows)} 条"
                                         + (f" · 剩约 {left:.0f} 分钟"
                                            if left > 0 else ""))})
                        else:
                            progress(f"OCR {pos:.0f}/"
                                     f"{'?' if dur <= 0 else format(dur, '.0f')}s"
                                     f" · {len(rows)} 条"
                                     + (f" · 剩约 {left:.0f} 分钟"
                                        if left > 0 else ""))
                    if cancel and cancel():
                        was_cancelled = True
                        break
        finally:
            # 无论读完、用户取消还是中途抛异常，都得让生产线程退出，
            # 否则它卡在 put/队列上，ffmpeg 进程也留着不放。
            # 串行模式下 feeder 为 None，但这里仍要关掉生成器触发其
            # finally 里的 proc.kill()（break 之后生成器不会自动收尾）。
            if feeder is not None:
                feeder.stop()
            else:
                try:
                    frames.close()
                except Exception:
                    pass
        _flush()
        _close()
    except KeyboardInterrupt:
        _close()
    except Exception as e:
        _close()
        return rows, f"OCR 失败：{type(e).__name__}: {e}"
    finally:
        if pool is not None:
            try:
                pool.terminate()
                pool.join()
            except Exception:
                pass

    if was_cancelled:
        # 用户中途按了停止。**绝不能**把扫了一半的结果当成完整台词返回——
        # 下游会拿它落库、写同名 .srt，那部片就永远只剩前半句台词了。
        # 返回空 + 专用错误码，indexer 据此把这条留成"还没识别"，
        # 下次索引会自动重跑它；已经跑完的素材不受影响。
        return [], "__cancelled__"

    if not rows:
        return [], (f"没识别到字幕（扫 {n_seen} 帧 / {n_batch} 批 / "
                    f"{time.time()-t_all:.0f}s）")
    rows = _merge(rows)
    _rebase_font(rows)
    if progress:
        done = f"OCR 完成 · {len(rows)} 条 · {n_seen} 帧 · {time.time()-t_all:.0f}s"
        if want_dict:
            progress({"pos": dur, "total": dur, "frames": n_seen,
                      "frames_total": n_seen, "batches": n_batch,
                      "lines": len(rows), "eta_min": 0.0, "text": done,
                      "finished": True})
        else:
            progress(done)
    return rows, ""


def _rebase_font(rows: List[Dict[str, Any]]) -> None:
    """识别完之后，用**全片台词**的字体指纹分布重算每行的 font_dev。

    为什么不能用标定阶段的基准：标定只抽 17 个框，且集中在少数几帧、
    字幕状态一致，MAD 小到 0.01；而识别阶段跨几百秒，镜头运动、淡入淡出、
    背景明暗让指纹自然波动大得多（实测库内 ink 横跨 0.09~0.49）。拿小样本
    的 MAD 当尺度，正常行全部被放大成巨大偏离（实测 100% 超标，徽标满天飞，
    "异常"就不再是异常）。全片 100+ 条台词的分布才是这片字幕字体的真实离散度。

    尺度 = max(1.5×MAD, 绝对下限)。绝对下限防"分布极稳时一点抖动被放大"，
    也防"分布极散时什么都能洗白"。中位数+MAD 抗离群——真有水印/艺术字
    混在台词里，也拉不歪基准。
    """
    fs = [(r["box"]["font"]) for r in rows
          if isinstance(r.get("box"), dict)
          and isinstance(r["box"].get("font"), dict)]
    if len(fs) < 8:
        return
    for key, floor in (("ink", 0.06), ("stroke", 1.2)):
        vals = sorted(f[key] for f in fs if f.get(key))
        if len(vals) < 8:
            continue
        med = vals[len(vals) // 2]
        mad = sorted(abs(v - med) for v in vals)[len(vals) // 2]
        scale = max(1.5 * mad, floor)
        for r in rows:
            b = r.get("box") if isinstance(r.get("box"), dict) else None
            f = b.get("font") if b else None
            if not isinstance(f, dict) or not f.get(key):
                continue
            # 直接覆盖，不取 max：_proof 里按标定基准算的那个初值不可靠
            # （标定 MAD 太小，初值普遍虚高），留 max 会把虚高值带进来。
            b["font_dev"] = round(abs(float(f[key]) - med) / scale, 3)


def _merge(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并相邻重复 + 修正时间区间。

    同时做一件事：**切断异常超长区间**。实测出现过一条 `80.5-149.5`（69 秒）
    的记录——那是因为中间有几帧没识别出字幕（画面转场/字幕淡出），`cur_text`
    一直没被换掉，区间就一路延长了。字幕不会停留那么久（库里的真实分布是
    中位 1.36s、P90 3.16s），所以超过 OCR_MAX_SPAN 就截断，截断后如果
    后面又认出同一句，会被下面的相似合并重新接上。

    合并方框凭证时有个必须注意的点：文本可能被换成更长的那一条
    （"我是谁"→"我是谁啊"），此时**方框凭证也要换成新文本对应的那个**，
    否则就会出现"文本来自 A 帧、方框来自 B 帧"的对不上——这种行
    看着有凭证，其实经不起复核。
    """
    out: List[Dict[str, Any]] = []
    for r in rows:
        span = r["end"] - r["start"]
        if span > C.OCR_MAX_SPAN:
            r["end"] = r["start"] + C.OCR_MAX_SPAN
        if out and _similar(out[-1]["text"], r["text"]) \
                and r["start"] - out[-1]["end"] < 0.8:
            out[-1]["end"] = max(out[-1]["end"], r["end"])
            if len(r["text"]) > len(out[-1]["text"]):
                out[-1]["text"] = r["text"]
                # 文本换了，凭证跟着换（不是合并——新文本来自新框）
                out[-1]["box"] = r.get("box") or {}
            else:
                # 保留原文本，就把支持它的帧数累加上来
                _merge_hits(out[-1], r)
            # 不管走哪个分支，异常信号都要传播：src 的 font_dev 大，
            # 合并后的行也得大（取最大），不能悄悄抹掉。
            d1 = (out[-1].get("box") or {}).get("font_dev") or 0.0
            d2 = (r.get("box") or {}).get("font_dev") or 0.0
            if d2 > d1 and out[-1].get("box"):
                out[-1]["box"]["font_dev"] = round(d2, 3)
            continue
        if out and r["end"] <= out[-1]["end"] and \
                _similar(out[-1]["text"], r["text"]):
            _merge_hits(out[-1], r)
            continue
        out.append(r)
    for i, r in enumerate(out):
        if r["end"] > r["start"]:
            continue
        nxt = out[i + 1]["start"] if i + 1 < len(out) else r["start"] + 1.5
        # 有方框凭证时，end 以**证据**为准：最后一个被认到框的时刻 + 一个采样步长。
        # 为什么不能一律用 min(nxt, start+6)：字幕只闪 0.5 秒、中间空两帧的情况很常见
        # （实测《唐伯虎点秋香》「有见地」只在 231.0/231.5 两帧有框，后面直接跳下一句），
        # 用下一句的 start 当 end 会把 0.5 秒的短句撑成 1.5 秒，
        # 覆盖率校验随之误判成"证据不足"。方框告诉我们它到哪就结束了。
        # 步长 0.2s 时给两个步长（0.4s）：单帧命中至少覆盖 [t-0.1, t+0.1]，
        # 只补 0.2s 会把 0.4s 的短句截掉一半。
        b = r.get("box") or {}
        if b.get("hits"):
            r["end"] = min(nxt, r["start"] + 2.0 * float(C.OCR_STEP))
        else:
            r["end"] = max(r["start"] + 0.3, min(nxt, r["start"] + 6.0))
    return out


def _merge_hits(dst: Dict[str, Any], src: Dict[str, Any]):
    """把 src 的方框凭证并进 dst（文本相同、只是又认到一次）。

    只在**框位置对得上**时才累加：`_similar` 的字符集判据太松，
    会把位置完全不同的另一句话并进来，直接加 hits 会把覆盖率算虚高
    （实测出现过 150%）。位置对不上就只更新 end（时间上确实是连续的），
    不动 hits。
    """
    b, s = dst.get("box") or {}, src.get("box") or {}
    if not s:
        return
    if not b:
        dst["box"] = dict(s)
        return
    if _same_place(s, [b]):
        b["hits"] = int(b.get("hits") or 0) + int(s.get("hits") or 0)
    b["drift"] = max(float(b.get("drift") or 0), float(s.get("drift") or 0))