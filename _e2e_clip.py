# -*- coding: utf-8 -*-
"""端到端：5 分钟片段 -> OCR 索引 -> 方框复查全链路（清单/现场复核/回填/画框图）

注意：Windows spawn 多进程会重新 import 本文件，所有执行代码必须
包在 main() 里由 __main__ 守卫调用，否则 OCR 会递归启动失败。
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "_testlib")


def sec(t):
    print("\n" + "=" * 64 + "\n" + t + "\n" + "=" * 64, flush=True)


def main():
    from core import config as C
    # 沙箱会拦截 >50 个文件的批量删除（grab_shots_batch 的临时目录清理），
    # 测试改走单线程截图路径；batch 分支此前已在整片《唐伯虎》上验证过。
    C.SHOT_WORKERS = 1
    from core import store as ST
    from core import boxaudit as BA
    from core import verify as V
    from core import indexer as IX

    # ---------- 1. 索引 ----------
    sec("1. OCR 索引 5 分钟片段")
    IX.ensure_layout(ROOT)
    st = IX.open_store(ROOT)
    t0 = time.time()
    idx = IX.Indexer(ROOT, use_asr=True, with_gif=False)
    res = idx.run(cb=lambda i, n, m: None)
    print("索引耗时 %.1fs" % (time.time() - t0))
    print("结果:", {k: v for k, v in res.items() if k != "items"})

    vids = st.list_videos()
    print("素材数:", len(vids))
    assert vids, "库里没素材"
    v = vids[0]
    vid = v["id"]
    print("目标:", v["name"], "id", vid, "lines", v["line_count"],
          "src", v["line_source"])
    assert v["line_count"], "没有台词，后续测试无意义"

    # ---------- 2. 复查清单 ----------
    sec("2. 方框复查清单 (audit_video)")
    rep = BA.audit_video(st, vid)
    print("总数 %d | 可校验 %d | 通过 %d | 可疑 %d | 无框 %d | 通过率 %s"
          % (rep["total"], rep["checked"], rep["passed"], rep["failed"],
             rep["no_box"], rep["rate"]))
    print("失败原因分布:", rep["reasons"])
    for x in rep["items"][:5]:
        m = x["metrics"]
        print("  %s~%s %-22r ok=%s cov=%s conf=%s cy=%s bh=%s"
              % (x["start_str"], x["end_str"], x["text"][:20], x["ok"],
                 m["cov"], m["conf"],
                 round(m["cy"], 3) if m["cy"] else None,
                 round(m["bh"], 3) if m["bh"] else None))
    bad = [x for x in rep["items"] if not x["ok"] and not x["skipped"]]
    print("可疑条数:", len(bad))
    for x in bad[:6]:
        print("   !", x["start_str"], repr(x["text"][:28]), x["reasons"])

    # ---------- 3. 现场复核 probe ----------
    sec("3. 现场复核 probe（回画面重抽帧比对）")
    video_path = os.path.join(ROOT, v["rel"])
    rows = st.lines_of(vid)
    picks = [rows[0], rows[len(rows) // 2], rows[-1]]
    for r in picks:
        t1 = time.time()
        p = BA.probe(video_path, r, n=C.BOX_PROBE_FRAMES)
        print("  %s %-24r -> %s | 命中 %d/%d | %.1fs"
              % (ST.fmt_ts(r.get("start")), (r.get("text") or "")[:22],
                 p["verdict"], p["matched"], len(p["frames"]),
                 time.time() - t1))
        print("     ", p["detail"])
        for f in p["frames"]:
            print("       t=%.2f text=%r match=%s dist=%s why=%s"
                  % (f.get("t", 0), (f.get("text") or "")[:16],
                     f.get("match"), f.get("dist"), f.get("why")))

    # ---------- 4. 回填方框凭证 ----------
    sec("4. 回填方框凭证（把一条清空后重新补）")
    target = rows[len(rows) // 2]
    st.set_box(target["id"], None)
    print("  目标:", ST.fmt_ts(target.get("start")), repr(target["text"][:28]))
    p = BA.probe(video_path, target, n=max(3, C.BOX_PROBE_FRAMES))
    folded = BA.fold_probe(p["frames"])
    print("  fold:", {k: folded.get(k) for k in
                      ("cx", "cy", "bw", "bh", "hits", "nbox")})
    st.set_box(target["id"], folded)
    again = st.get_line(target["id"])
    print("  回填后 has_box:", bool(again.get("box")),
          "| 再校验 ok:", BA.audit_line(st, vid, again)["ok"])

    # ---------- 5. 台词帧 + 台词框 + 文字（标注图） ----------
    sec("5. 台词帧 / 台词框 / 文字（标注图）")
    os.makedirs("_out", exist_ok=True)
    for r in picks:
        mid = (float(r.get("start") or 0)
               + float(r.get("end") or r.get("start") or 0)) / 2
        src, w, h = BA.frame_jpg(video_path, ROOT, mid)
        box = V._load_box(r)
        if not box:
            print("  无框，跳过:", repr(r["text"][:20]))
            continue
        live = None
        p = BA.probe(video_path, r, n=3)
        okf = [f for f in p["frames"] if f.get("box")]
        if okf:
            mid_f = okf[len(okf) // 2]
            live = dict(mid_f["box"])
            live["__match__"] = bool(mid_f.get("match"))
        out = os.path.join("_out", "box_%d.jpg" % r["id"])
        data = BA.annotate(src, box, live, stamp=(r.get("text") or "")[:22])
        with open(out, "wb") as fh:
            fh.write(data)
        print("  ->", out, "| 帧 %dx%d | 框 cy=%s bh=%s | 文字: %s"
              % (w, h, round(box.get("cy") or 0, 3),
                 round(box.get("bh") or 0, 3), (r.get("text") or "")[:34]))

    sec("完成")


if __name__ == "__main__":
    main()
