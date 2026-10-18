#!/bin/bash
cd "D:/project/AIProjects/frameseek"
for i in $(seq 1 60); do
  echo "=== 尝试 $i $(date +%H:%M:%S) ==="
  python -c "
import sys; sys.path.insert(0,'.')
from core import gen as G
try:
    r=G.generate(prompt='一只橘猫在窗台上打哈欠，午后阳光斜照，暖色调，写实',
                 duration=5, aspect='16:9 (Widescreen)', backend='agnes',
                 name='后端接入验证', root='E:/视频/电视剧', timeout=600)
    print('SUCCESS', r['file'], r['seconds'], r['w'], r['h'], r['size_mb'])
except Exception as e:
    print('FAIL', str(e)[:200])
" 2>/dev/null | grep -E "SUCCESS|FAIL"
  if ls "AI生成/后端接入验证.mp4" >/dev/null 2>&1; then
    echo "=== 成片已存在 $(date +%H:%M:%S) ==="
    ls -la "AI生成/后端接入验证.mp4"
    exit 0
  fi
  sleep 100
done
echo "=== 60次都失败 ==="
exit 1
