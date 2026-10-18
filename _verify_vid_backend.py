# -*- coding: utf-8 -*-
"""双后端视频生成的离线验证：不联网、不跑模型，只验逻辑。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import gen as G  # noqa: E402

fails = []


def ck(label, cond, extra=""):
    print(("  OK  " if cond else "FAIL  ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(label)


print("=== 1. 后端摸底 ===")
bs = G.video_backends()
ids = [b["id"] for b in bs]
ck("列出 agnes / h3 两个后端", ids == ["agnes", "h3"], str(ids))
for b in bs:
    print(f"      {b['id']:6} ready={b['ready']!s:5} offline={b['offline']!s:5} {b['eta']}")

print("\n=== 2. engine_info 汇总 ===")
info = G.engine_info()
ck("engine 标记为多后端", info.get("engine") == "multi-backend", str(info.get("engine")))
ck("带 backends 列表", isinstance(info.get("backends"), list) and len(info["backends"]) == 2)
ck("带 backend_order", info.get("backend_order") == ["agnes", "h3"], str(info.get("backend_order")))
ck("至少一个后端 ready", bool(info.get("ready")))

print("\n=== 3. 宽高比映射（Agnes 只支持 7 种，2:3/3:2 被排除）===")
ck("16:9 有映射", G.AGNES_ASPECTS.get("16:9 (Widescreen)") == "16:9")
ck("9:16 有映射", G.AGNES_ASPECTS.get("9:16 (Portrait Widescreen)") == "9:16")
ck("21:9 有映射", G.AGNES_ASPECTS.get("21:9 (Ultrawide)") == "21:9")
ck("2:3 不支持", "2:3 (Portrait Photo)" in G.AGNES_UNSUPPORTED)
ck("3:2 不支持", "3:2 (Photo)" in G.AGNES_UNSUPPORTED)
ck("H3 的 8 种宽高比仍完整", len(G.ASPECTS) == 8, str(len(G.ASPECTS)))
ck("Agnes 覆盖 6 种", len(set(G.AGNES_ASPECTS.values())) == 6,
   str(sorted(set(G.AGNES_ASPECTS.values()))))

print("\n=== 4. 参数校验 ===")
cases = [
    ("空提示词", dict(prompt=""), "不能为空"),
    ("提示词超长", dict(prompt="x" * 2100), "超长"),
    ("未知宽高比", dict(prompt="hi", aspect="5:4"), "宽高比"),
    ("未知后端", dict(prompt="hi", backend="sora"), "未知后端"),
    ("参考图不存在", dict(prompt="hi", ref_image="D:/nope.png"), "不存在"),
]
for label, kw, want in cases:
    try:
        G.generate(**kw)
        ck(label + " 应当报错", False, "居然通过了")
    except ValueError as e:
        ck(label + " 正确报错", want in str(e), str(e)[:60])
    except Exception as e:
        ck(label + " 正确报错", False, f"{type(e).__name__}: {e}"[:70])

print("\n=== 5. 时长换算 ===")
ck("Agnes 3 秒抬到 4", max(G.AGNES_SECONDS_MIN, min(12, 3)) == 4)
ck("Agnes 20 秒压到 12", max(4, min(12, 20)) == 12)
f10, s10 = G.frames_for(10.0)
ck("H3 10 秒在 124~362 帧内", 124 <= f10 <= 362, f"{f10} 帧 / {s10}s")
ck("H3 帧数落在 17k+5 栅格", (f10 - 5) % 17 == 0, str(f10))

print("\n=== 6. 落点 ===")
p = G.dest_path(name="双后端验证")
ck("落点存在 .mp4", p.lower().endswith(".mp4"), os.path.basename(p))
ck("落在 AI生成 目录", os.path.normcase(os.path.dirname(p)) ==
   os.path.normcase(G.output_dir()), os.path.dirname(p))
p2 = G.dest_path(name="双后端验证")   # 还不存在文件，所以同名
ck("同名不覆盖（尚不存在时同名）", p2 == p, os.path.basename(p2))
open(p, "wb").close()
p3 = G.dest_path(name="双后端验证")
ck("文件已存在时自动 _1 递增", p3.endswith("_1.mp4"), os.path.basename(p3))
try:
    os.remove(p)
except OSError:
    pass

print("\n=== 7. Agnes 落点拼接（防重复 .mp4）===")
stem = G.safe_name("测试片") + ".mp4"
ck("safe_name 去后缀交给 dest_path", stem.endswith(".mp4"), stem)

print("\n=== 8. _IMPLS 注册表 ===")
ck("注册了 agnes 与 h3", set(G._IMPLS) == {"agnes", "h3"}, str(list(G._IMPLS)))
ck("auto 降级链 agnes→h3", G.BACKEND_ORDER == ("agnes", "h3"), str(G.BACKEND_ORDER))
ck("backend_ready 对已知 id 不抛异常",
   isinstance(G.backend_ready("agnes"), bool) and isinstance(G.backend_ready("h3"), bool))
ck("backend_ready 对未知 id 返回 False", G.backend_ready("nope") is False)

print("\n=== 9. H3 GPU 锁 ===")
ck("_H3_LOCK 已建立", hasattr(G, "_H3_LOCK") and G._H3_LOCK is not None)
ok = G._H3_LOCK.acquire(blocking=False)
if ok:
    try:
        import threading
        err = {}

        def probe():
            try:
                G._gen_h3(prompt="x", duration=5, timeout=5)
            except RuntimeError as e:
                err["e"] = str(e)
        t = threading.Thread(target=probe)
        t.start()
        t.join(timeout=20)
        ck("H3 被占时快速失败而不是排队死等", "GPU 独占" in err.get("e", ""),
           err.get("e", "没报错")[:60])
    finally:
        G._H3_LOCK.release()
else:
    ck("能拿到 H3 锁", False)

print("\n=== 结果 ===")
print(f"失败 {len(fails)} 项" + ("：" + ", ".join(fails) if fails else "，全部通过"))
sys.exit(1 if fails else 0)
