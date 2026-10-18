#!/bin/bash
cd "D:/project/AIProjects/frameseek"
PROMPT='车内驾驶舱第一人称视角，透过前挡风玻璃往外看，夜晚大雪。密集的雪片被车速迎面拍打在玻璃上，雪粒顺着玻璃横向斜飞成一道道白线，水珠和雪水混在一起挂在玻璃上，把远处的灯光晕成模糊光斑。黑色雨刮器有节奏地在挡风玻璃上来回扫动，刮过的地方留下一道清晰干净的水弧，刚刮完雪点立刻又重新聚成水珠。前方是一条没有路灯的乡村窄水泥路，两侧是漆黑起伏的农田，路面和路肩上已经铺了薄薄一层白雪，路边高草的顶上压着雪，被风吹得歪斜。远处零星几家村庄的暖黄窗光。汽车前灯在雪幕中切出一道明亮的光锥，光里的雪片清晰可见，湿滑的柏油路面把灯光拉成一条条长长的反光，车轮碾过积雪溅起的雪雾扑到镜头前。电影感写实摄影，浅景深，深蓝夜色对比前灯的暖黄，高细节'
for i in $(seq 1 80); do
  echo "=== 尝试 $i $(date +%H:%M:%S) ==="
  PROMPT="$PROMPT" python -c "
import os,sys; sys.path.insert(0,'.')
from core import gen as G
try:
    r=G.generate(prompt=os.environ['PROMPT'], duration=10, aspect='16:9 (Widescreen)',
                 backend='agnes', name='夜雪乡村路', root='E:/视频/电视剧', timeout=780)
    print('SUCCESS', r['file'], r['seconds'], r['w'], r['h'], r['size_mb'])
except Exception as e:
    print('FAIL', str(e)[:180])
" 2>/dev/null | grep -E "SUCCESS|FAIL"
  if ls "AI生成/夜雪乡村路.mp4" >/dev/null 2>&1; then
    echo "=== 队列通了，成片已落盘 $(date +%H:%M:%S) ==="
    ls -la "AI生成/夜雪乡村路.mp4"
    exit 0
  fi
  sleep 100
done
echo "=== 80次都失败 ==="
exit 1
