# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - 全局配置"""
import os

APP_NAME = "frameseek"
APP_LABEL = "拾帧 FrameSeek"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 选中文件夹后，在该文件夹下生成的隐藏目录名
CACHE_DIR = ".frameseek"
DB_NAME = "index.db"
SHOT_DIR = "shots"        # 台词截图
GIF_DIR = "gifs"          # 视频动图
COVER_DIR = "covers"      # 视频封面
CLIP_DIR = "clips"        # 导出的前后 N 秒片段
UPLOAD_DIR = "uploads"    # 上传落盘目录
BOX_DIR = "boxaudit"      # 方框复查：按需抽的原帧（标注在内存里画，不落盘）

PORT = 8771
HOST = "127.0.0.1"

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
    ".m4v", ".ts", ".rmvb", ".rm", ".mpg", ".mpeg", ".3gp", ".vob", ".m2ts",
}

# 外挂字幕后缀（删素材时顺带一起删）
SUB_EXT = (".srt", ".ass", ".ssa", ".vtt")

# 语音识别（已停用：准确率不如 OCR，且慢）。保留代码作为最后兜底，默认关闭。
# 台词获取优先级：本地 SRT -> 内嵌字幕 -> 在线搜字幕 -> OCR 硬字幕识别
ASR_ENDPOINT = "https://hf-mirror.com"   # huggingface.co 直连会 502，走镜像
ASR_MODELS = ["small", "tiny"]           # 依次尝试，前一个加载/下载失败就降级
ASR_MODEL = "small"                      # 默认（small 中文够用；tiny 快 3 倍但差不少）
ASR_LANG = "zh"
ASR_DEVICE = "auto"        # auto / cuda / cpu；auto 先探 GPU，探不到自动回 CPU
ASR_COMPUTE = "auto"       # auto / float16 / int8_float16 / int8（GPU 上 fp16 最快）
ASR_ENABLED = False        # 无字幕时是否自动语音转写（默认关，走 OCR）
ASR_VAD = True             # 静音过滤，省时间
ASR_CHUNK = 900.0          # 分块转写（秒）：内存可控 + 能报真实进度
ASR_CHUNK_OVERLAP = 0.0    # 块与块之间重叠秒数

# 在线字幕源：已停用。
# 原因：字幕站结构变动频繁、反爬严格，本机外网也受限（Google 不通、
# 部分站 403），写死的解析器很快会失效，且下到的内容质量不可控。
# 台词获取优先级：本地 SRT -> 内嵌字幕 -> OCR 硬字幕识别
SUB_SEARCH_ENABLED = False
SUB_SEARCH_TIMEOUT = 15           # 单个源超时（秒）
SUB_SEARCH_SAVE = True            # 机器生成的台词保存成视频同名 .srt（方便你手工改）

# OCR 硬字幕识别（台词主要来源：纯看画面，不需要音频）
OCR_ENABLED = True
OCR_STEP = 0.2# 采样网格（秒）。0.2s = 每秒 5 帧，一句 1.5~3s 的台词被命中 8~15 次，
                  # 足以把短句（0.4s 起）完整捕获，且同句多帧可投票择优——
                  # 单帧偶发漏字不再污染整句文本。代价是帧数比 0.5s 多 2.5 倍
                  # （5 分钟片段约 50s，1 小时电影约 10 分钟），要更快就调回 0.5，
                  # 但 0.3~0.5s 的短对白会开始整句漏掉。不做帧差门控——实测帧差
                  # 挡不住电影背景自身的运动，触发率 50%+，等于没门控。
OCR_BAND = 0.30           # 只看画面底部 30%（省 3 倍算力）
OCR_WIDTH = 960         # 送 OCR 的图宽（太小字认不准，太大纯耗算力）
OCR_DET_LIMIT = 736     # 检测模型的像素上限（DBNet 会把图缩到最长边 ≤ 它再检测）。
                        # 调大 = 检测更准但更慢（耗时近似随像素线性涨）；
                        # 调小 = 更快但小字容易漏。736 是 rapidocr 默认值，
                        # 实测本片 960 宽送检会被缩到 736，再往上调收益<5%、耗时翻倍。
                        # 运行时改这个没用（子进程不继承内存），要改就设环境变量
                        # FRAMESEEK_DET_LIMIT，或直接编辑本文件重启服务。
OCR_WORKERS = 4           # 并行 OCR 进程数。实测 4 是最优点（40ms/帧），
                          # 8 进程只快 6% 却多一倍内存和 spawn 开销。
                          # 关键前提是每进程线程数被压住——见 ocr._apply_threads。
OCR_THREADS = 2           # 每进程 onnxruntime 线程数。4×2=8，别超过核数。
                          # 曾经这里是 8 进程但每进程没限线程（默认 16），
                          # 128 个线程抢 16 核，比单进程还慢 5 倍（625ms/帧）。
OCR_USE_DML = True        # 用 DirectML 把 GPU（4060Ti）拉进来分担 det/rec。
                          # 实测单帧：det 39→21ms（1.8×），rec 110→97ms（1.2×，
                          # rec 输入是 48×320 单行小图，GPU launch 开销吃掉收益），
                          # 总体 1.28×。依赖 vendor/ort_dml 里的
                          # onnxruntime-directml 包（1.24.4，pip --target 装的，
                          # 阴影掉 venv 里的 CPU-only 1.29）。识别文本与 CPU 一致。
                          # 包缺失/provider 不可用/onnxruntime 已被 import 时
                          # 自动退回 CPU，不影响正确性；运行时可用环境变量
                          # FRAMESEEK_OCR_DML=0 临时关掉对比。
OCR_DML_DEVICE_ID = 0     # 用哪块 GPU（单卡 4060Ti，0 即可）
OCR_MIN_LEN = 2            # 少于这个字的识别结果丢掉（噪声）
OCR_MAX_LEN = 60          # 单条超过这个字数基本是识别串行，切一刀
OCR_MAX_SPAN = 5.0         # 单条台词最长跨度（秒）。库里真实分布中位 1.36s / P90 3.16s，
                          # 超过 5 秒基本是"中间几帧没认出来导致区间一路延长"，要截断
OCR_FONT_DEV_MAX = 1.5     # 字体指纹偏离度上限（全片分布 MAD 尺度，见 ocr._rebase_font）。
                          # 超过的行**不删**，只在台词页标"字体异常"、方框校验里
                          # 降权——供纠错和二次验证时优先怀疑/忽略。1.5 是强异常档：
                          # 实测 5 分钟片段 102 条里约 5~8 条超标（OCR 串行句、
                          # 单帧短句、艺术字），不会满天飞。片内真有艺术字标题，
                          # 删了就丢台词，所以只标不删。

# ---------------- 字幕纯净化（只要字幕本身，画面里的杂字一律不要）----------------
# 问题：RapidOCR 是"画面里有什么字就认什么字"，一帧返回 N 个框，
# 老代码直接 "".join() 拼成一句。于是这些不是台词的东西全进了台词库：
#   - 中日双语片：同一帧两行（上中文/下日文），被拼成一句
#     实测《云之彼端》「开战已经是箭在弦上了…」+「决定の真面」
#   - 片内 UI / 数字屏：「0663 1.00」（实测《名侦探柯南》）
#   - 角落台标、底部压制组水印：贴边或横贯全宽
#   - 片头演职员表：「镇衡主演」「李麗麗友情客串」
# 解法分两层：①先用几何+置信度筛出"长得像字幕"的框（下面这些阈值）
#           ②开跑前先花十几秒标定这部片的字幕带（字幕位置是压制组决定的，
#             不同片子差很多，写死窗口会误杀，见 ocr.calibrate）。
OCR_PURE = True# 总开关。关掉 = 回到"一帧所有框直接拼一起"的老行为
OCR_CONF_MIN = 0.60          # 识别置信度门槛。实测正文 0.68~0.94，噪声 0.55~0.62
OCR_W_MIN = 0.08             # 框宽下限（占裁切带宽度）。太窄 = 单个噪声字符
OCR_W_MAX = 0.88             # 框宽上限。横贯 88% 以上必是水印，不是台词
OCR_H_MIN = 0.09             # 字高下限（占裁切带高度）
OCR_H_MAX = 0.34             # 字高上限
OCR_CX_TOL = 0.22            # 框中心距画面中线最远多远（0.5±0.22）
OCR_EDGE_MIN = 0.03          # 框边缘离画面左右边至少这么远，贴边的当台标丢掉
OCR_CALIB_SAMPLES = 20       # 标定采样帧数：整片均匀撒点，每点 OCR 一次
OCR_CALIB_CONF = 0.50        # 标定时用更宽松的置信度门槛，先粗定位字幕带
# 标定缓存：同一剧集各集的字幕带位置/字高/字体几乎相同（同压制组、同版式），
# 标定又是每部必付的固定税（实测约 54 秒/部），缓存后一部剧只付一次。
# 键含源分辨率 + 编码 + OCR_WIDTH/BAND/STEP，任一变了自动失效。
# 关掉这个开关就退回"每部都重新标定"的原行为。
OCR_CALIB_CACHE = True
OCR_MAX_SUB_LINES = 2# 一帧最多保留几行（长句折行会分两行）
# 区分"双语片的附加语种行"和"长句折行"用。两个条件要**同时**成立
# 才判为附加行——单条都会误杀折行字幕。阈值来自实测：
#   云之彼端(双语) 日文行/中文行：字高比 0.75、置信度比 0.82  → 两个都低于阈值，丢掉
#   天气之子(折行) 第二行/第一行：字高比 1.20、置信度比 0.90→ 都不低于，全部保留
OCR_H_RATIO_DROP = 0.85        # 字高比低于它算"字号更小"
OCR_CONF_RATIO_DROP = 0.88     # 置信度比低于它算"识别更差"

# 判断"这一帧认的还是不是同一句话"的位置容差（归一化）。
# 多帧合并原本只靠 _similar()（字符集重合 ≥75%），够松——相邻两句用词相近
# 就会串味，实测覆盖率一度算出 120%~150%（后一句的帧被算进前一句的凭证）。
# 字幕位置是死的：同一部片里同一句话在连续帧里只待一个地方，所以位置对不上
# 就说明认的是另一句。横向容差比纵向宽，因为不同句子字数不同→框宽不同→
# 框心会左右挪（实测同一句内框心抖动 < 0.02，跨句可差 0.05 以上）。
OCR_SAME_PLACE_TOL = 0.05      # 横向：框心 x 之差
OCR_SAME_CY_TOL = 0.60         # 纵向相对横向的收紧倍数（纵向本就极稳）

# ---------------- 1080p 代理（高分辨率片先压一份再抽帧/OCR）----------------
# 问题：ffmpeg 解码器永远吐**原生分辨率**帧，scale 滤镜在解码**之后**，
# 所以滤镜链里压得再狠，4K 该解的像素一帧都不会少。实测 OCR 流式管道是
# 全帧解码（-i → -vf crop,scale → rawvideo，一帧不跳），Python 端再按
# OCR_STEP 网格丢帧——也就是说 4K 片有 80% 的帧"解完就扔"。真正省时间的
# 唯一形态是：**先转一份 1080p 代理**，之后 OCR 流 + 台词截图 + 重扫全部读代理。
#
# 触发条件只看分辨率（不是所有片都值得压）：
#   - > PROXY_TRIGGER_PIXELS 的片建代理；1080p 及以下直接跳过。
#   - 触发后压到 PROXY_MAX_W（长边），保持比例，不裁切、不拉伸。
#
# 画质基本无损（这是能放心压的前提）：OCR 送检带宽固定 OCR_WIDTH=960，
# 3840×2880 的底部 30% 裁带缩到 960，与 1920×1440 裁带缩到 960 **尺寸完全相同**，
# 只是多了一次重采样（3840→1920→960 vs 3840→960），对 OCR 文本结果无实质影响；
# 台词截图输出固定 SHOT_WIDTH=480，源是 4K 还是 1080p 出来的图基本一样。
# 所以代理只影响**解码耗时**，不影响识别/截图画质。
PROXY_ENABLED = True
PROXY_DIR = "proxy"          # 放在 .frameseek/proxy/ 下（缓存目录，list_video_files 已排除整个 CACHE_DIR，代理天然不会被扫进库）
PROXY_MAX_W = 1920           # 代理最大宽度（长边），等比缩放
PROXY_TRIGGER_PIXELS = 1920 * 1080   # 原片像素数超过这个才值得建代理（严格大于）
PROXY_CRF = 23               # 代理只用于抽帧/OCR，不追求视觉无损，压低码率省磁盘
PROXY_MAXRATE = "2500k"      # 码率天花板（-maxrate/-bufsize）。**必须有**，否则 nvenc
                             # 的 VBR 会一路推高码率：实测 43 分钟 4K 压出 2.9GB
                             # 的代理，大文件 + 高解码量把服务压到段错误。
                             # 2.5Mbps 对 1080p 字幕识别绰绰有余（字幕带才 960 宽）
PROXY_BUFSIZE = "5000k"      # 码率缓冲（bufsize = 2× maxrate）
PROXY_MAX_MB = 1500          # 代理体积硬上限（MB）。第二道保险：超过判死并重建，
                             # 防止任何编码器失控又安静产出一个几 GB 的文件
PROXY_PRESET = "p1"          # nvenc preset（4060Ti，p1 很快，够用）
PROXY_CRF_CODEC = "h264_nvenc"
PROXY_AUDIO = "copy"         # 代理只为抽帧和 OCR，不需要音频，原样复制省时间
PROXY_TIMEOUT = 3600         # 单部转码超时（秒）。走 GPU 全链路 45 分钟片约 1.5~2 分钟
PROXY_DUR_TOL = 0.5          # 代理与原片时长差超过这个秒数视为过期，重建
PROXY_USE_GPU = True         # 优先 nvenc 硬编；失败自动回落 libx264 ultrafast

# ---------------- 代理转码的 GPU 全链路（这是代理提速 4.7 倍的关键）----------------
# 原来只有**编码**在 GPU：解码 4K HEVC 10bit 和 lanczos 缩放都还在 CPU 上跑，
# 而这两步恰好是慢的。实测（3136×2160 HEVC Main10，180 秒片段，4060Ti/16核）：
#   CPU解码 + lanczos + nvenc ......... 34.0s  (x5.3 实时)
#   CPU解码 + bicubic + nvenc ......... 31.8s（换插值只快 6%，证明瓶颈不在缩放）
#   NVDEC + scale_cuda + nvenc .........  7.2s  (x24.9 实时)   ← 全程不落 CPU
# 也就是 45 分钟整集从 8.5 分钟压到 1.8 分钟。**瓶颈是 CPU 软解码，不是缩放**。
#
# 画质实测（亮剑 3840×2880 真烧录字幕片，OCR 走真实入口 ocr_lines）：
#   scale_cuda 默认插值是 nearest，**会丢台词**（14 条丢 2 条），绝对不能用；
#   interp_algo=bicubic 时 OCR 14/14 与 CPU lanczos 完全一致（零丢失零新增），
#   像素 PSNR 41.1 dB。所以显式指定 bicubic。
PROXY_HW_DECODE = True       # NVDEC 硬解 + scale_cuda 硬缩放；失败自动回落 CPU 软解
PROXY_CUDA_INTERP = 3        # scale_cuda 的 interp_algo。0/1(nearest) 会丢字幕，3=bicubic 安全
PROXY_CUDA = "cuda"          # -hwaccel 取值。d3d11va 实测与 nvenc 组合会锁不上 surface，勿改

# ---------------- 方框复查（台词帧 / 台词框 / 文字）----------------
# 「这条台词的框到底圈对了没有」光看数字不够，必须把框画回那一帧上看。
# 这几个参数控制复查页的取帧与标注：
BOX_FRAME_WIDTH = 960          # 复查帧宽度（比台词截图 480 大，够看清字形边缘）
BOX_PAD = 0.012                # 标注框往外扩的比例：检测框是贴字的，
                               # 不留余量画出来会显得"压字"，看着像框歪了
BOX_LIVE_TOL = 0.14            # 现场复核：库里的框心与这一帧实测框心的容差
                               # （归一化，x+y 相加），沿用 core/verify.py 的口径
BOX_PROBE_FRAMES = 3           # 一条台词抽几帧做现场复核：首/中/尾。
                               # 3 帧能看出"框是不是稳的"，再多只是慢


# ctranslate2 的 GPU 后端要 CUDA 12 运行时（cublas64_12.dll 等）。
# 本机没装 CUDA Toolkit，但 Ollama 自带一套，按序找，找到就注进 DLL 搜索路径。
ASR_CUDA_DLL_DIRS = [
    r"C:\Users\Administrator\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12",
]

# 动图预览
GIF_WIDTH = 320
GIF_FPS = 8
GIF_SEC = 4

# 台词截图
SHOT_WIDTH = 480
SHOT_QUALITY = 3           # ffmpeg -q:v
SHOT_WORKERS = 8           # 并发抽帧数（批量模式下 = 分段数）
SHOT_HWACCEL = True        # 抽帧用 -hwaccel cuda 硬解（失败自动回落软解）
SHOT_BATCH_MIN = 24        # 台词少于这个数就走逐帧 seek（批量要解整片，反而亏）

# 剪辑导出（台词前后各多少秒）
CLIP_PRE = 5.0
CLIP_POST = 5.0
SUB_FONT = "Microsoft YaHei"   # 导出片段烧字幕用的字体（libass 按名字找系统字体）

# 大文件 md5 采样字节数（全量 md5 对几 G 的视频太慢）
MD5_SAMPLE = 2 * 1024 * 1024
MD5_FULL_LIMIT = 512 * 1024 * 1024
