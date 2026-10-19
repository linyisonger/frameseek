# -*- coding: utf-8 -*-
"""MCP server 回归测试（stdio JSON-RPC，真实子进程）。

用法： python tools/test_mcp.py
覆盖：
  - 服务能起来、tools/list 有 37 个工具、每个工具参数都有 description
  - resources/list 有 6 个资源，且都能读出来
  - 只读工具端到端：doctor / libraries / search / find_videos / list_clips
  - 新增能力：export_clip 的 subtitle（auto/off/force 三档）与批量参数进了 schema
  - 烧字幕策略：OCR 源在 auto 档自动跳过（core/media.decide_burn 的 12 个分支）
  - 出图能力：img_info 双后端摸底、入参校验、**真实出一张 sd15 图**、
    同 seed 可复现（md5 相同）、reroll 换种子出新图
  - 错误返回带 hint（LLM 撞得最多的场景：id 找不到 / 未指定库）
  - 查库类工具的错误一律带 root 相关提示

只读为主，不跑异步任务（不产生副作用）。两处会真的写文件，跑完自动清理：
export_clip 的片段（测试库 clips 目录，同名覆盖）、
出图测试的 _mcp_test*.png/.json（AI生成/绘画/，同名覆盖）。
"""
import json
import os
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
SERVER = os.path.join(BASE, "mcp_server.py")

_env = dict(os.environ)
_env["PYTHONIOENCODING"] = "utf-8"
_env["PYTHONUTF8"] = "1"
_env["PYTHONUNBUFFERED"] = "1"

proc = subprocess.Popen(
    [PY, SERVER], cwd=BASE, env=_env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")


def _drain():
    """stderr 必须持续抽干，否则管道写满会把服务端事件循环憋死。"""
    for _ in proc.stderr:
        pass


threading.Thread(target=_drain, daemon=True).start()

_id = [0]
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"   {detail}" if detail and not cond else ""))


def send(method, params=None, notify=False):
    if notify:
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
    else:
        _id[0] += 1
        msg = {"jsonrpc": "2.0", "id": _id[0], "method": method,
               "params": params or {}}
    proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    return msg.get("id")


def recv(want, timeout=240):
    while True:
        line = proc.stdout.readline()
        if not line:
            raise SystemExit("服务端关闭了 stdout")
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get("id") == want:
            return m


def boot():
    i = send("initialize", {"protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"}})
    r = recv(i).get("result", {})
    send("notifications/initialized", {}, notify=True)
    return r


def jrpc(method, params=None):
    i = send(method, params)
    r = recv(i)
    if "error" in r:
        return {"__rpc_error": r["error"]}
    return r.get("result", {})


def call(name, args=None):
    res = jrpc("tools/call", {"name": name, "arguments": args or {}})
    if "__rpc_error" in res:
        return {"ok": False, "err": "RPC " + str(res["__rpc_error"])}
    txt = "".join(c.get("text", "") for c in res.get("content", [])
                  if c.get("type") == "text")
    try:
        return json.loads(txt)
    except Exception:
        return {"ok": None, "raw": txt}


def tools():
    return [t.model_dump(by_alias=True, exclude_none=True)
            if hasattr(t, "model_dump") else t
            for t in jrpc("tools/list").get("tools", [])]


def resources():
    return jrpc("resources/list").get("resources", [])


def read_res(uri):
    res = jrpc("resources/read", {"uri": uri})
    txt = "".join(c.get("text", "") for c in res.get("contents", []))
    try:
        return json.loads(txt)
    except Exception:
        return {"__text": txt}


def main():
    print("== MCP server 回归测试 ==\n")
    init = boot()

    print("[1] 协议握手")
    check("initialize 返回 capabilities", bool(init.get("capabilities")))
    check("声明了 tools 能力", "tools" in (init.get("capabilities") or {}))
    check("声明了 resources 能力", "resources" in (init.get("capabilities") or {}))
    ins = init.get("instructions") or ""
    check(f"有 instructions（{len(ins)} 字）", len(ins) > 200)
    check("instructions 提到 root 撞号", "撞号" in ins)
    check("instructions 提到异步轮询", "frameseek_task" in ins and "轮询" in ins)
    check("instructions 提到 doctor", "frameseek_doctor" in ins)
    check("instructions 提到资源", "frameseek://" in ins)

    print("\n[2] 工具面")
    ts = tools()
    names = {t["name"] for t in ts}
    check(f"工具数 = 28（实际 {len(ts)}）", len(ts) == 28)
    must = {"frameseek_doctor", "frameseek_find_videos", "frameseek_video_summary",
            "frameseek_list_clips", "frameseek_export_clip", "frameseek_search",
            # 出图能力（曾经整块漏掉，导致 LLM 以为拾帧只能出视频）
            "frameseek_img_info", "frameseek_generate_image",
            "frameseek_reroll_image", "frameseek_list_images"}
    check("新增/核心工具都在", must <= names, str(must - names))
    check("instructions 提到出图", "frameseek_generate_image" in ins
          and "静态图" in ins)

    print("\n[3] 参数级 description（LLM 选参型的唯一依据）")
    missing = []
    for t in ts:
        props = (t.get("inputSchema") or {}).get("properties") or {}
        for k, v in props.items():
            if not (v.get("description") or "").strip():
                missing.append(f"{t['name']}.{k}")
    check(f"所有参数都有 description（缺 {len(missing)}）", not missing,
          ", ".join(missing[:8]))

    print("\n[4] 关键参数语义")
    ec = next(t for t in ts if t["name"] == "frameseek_export_clip")["inputSchema"]
    props = ec["properties"]
    check("export_clip 有 subtitle", "subtitle" in props)
    check("export_clip 有 line_ids（批量）", "line_ids" in props)
    check("export_clip 的 root 说明了撞号", "撞号" in props["root"]["description"])
    check("export_clip 的 pre/post 有范围",
          props["pre"].get("maximum") == 120)
    check("export_clip 的 subtitle 是三档字符串（不是布尔开关）",
          props["subtitle"].get("type") == "string"
          and props["subtitle"].get("default") == "auto")
    check("subtitle 说明里点明 OCR 自动跳过",
          "OCR" in props["subtitle"]["description"]
          and "跳过" in props["subtitle"]["description"])

    sc = next(t for t in ts if t["name"] == "frameseek_scan")["inputSchema"]
    check("scan.use_asr 说明了历史命名",
          "历史遗留" in sc["properties"]["use_asr"]["description"])

    gl = next(t for t in ts if t["name"] == "frameseek_get_lines")["inputSchema"]
    check("get_lines.limit 有上限", gl["properties"]["limit"].get("maximum") == 5000)

    print("\n[5] 资源层")
    rs = resources()
    uris = {r.get("uri") for r in rs}
    want = {"frameseek://libraries", "frameseek://tasks/running",
            "frameseek://images/backends",
            "frameseek://tool-catalog"}
    check(f"5 个资源都在（实际 {len(rs)}）", want <= uris, str(want - uris))
    for u in sorted(uris):
        d = read_res(u)
        check(f"资源可读 {u}", bool(d) and "__text" not in d)

    ibk = read_res("frameseek://images/backends")
    check("生图资源给了双后端",
          {"agnes", "sd15"} <= set(ibk.get("parameter_matrix") or {}))
    check("生图资源说明了哪些参数被忽略",
          any("忽略" in v for v in (ibk.get("parameter_matrix") or {}).values()))
    check("生图资源写了重掷流程",
          len(ibk.get("how_to_reroll") or []) >= 3)
    check("生图资源标了联网后端",
          any(b.get("offline") is False
              for b in (ibk.get("backends") or [])))


    print("\n[6] 只读工具端到端")
    doc = call("frameseek_doctor")
    check("doctor ok=true", doc.get("ok") is True, json.dumps(doc, ensure_ascii=False)[:200])
    check("doctor 带 problems 数组", isinstance(doc.get("problems"), list))
    check("doctor 带 engines", bool(doc.get("engines")))
    check("doctor 带 libraries", bool(doc.get("libraries")))

    libs = call("frameseek_list_libraries")
    check("list_libraries 有库", len(libs.get("libraries", [])) >= 1)
    first = (libs.get("libraries") or [{}])[0]
    check("库条目带 path", bool(first.get("path")))

    libres = read_res("frameseek://libraries")
    check("资源 libraries 与工具一致",
          len(libres.get("libraries", [])) == len(libs.get("libraries", [])))

    taskres = read_res("frameseek://tasks/running")
    check("tasks 资源有 count", "count" in taskres)

    print("\n[7] 搜索与找片")
    se = call("frameseek_search", {"query": "的", "limit": 3})
    check("search 返回 ok", se.get("ok") is True)
    if (se.get("results") or [{}])[0].get("root"):
        hit = se["results"][0]
        check("search 结果带 root", bool(hit.get("root")))
        check("search 结果带 line_id", hit.get("line_id") is not None)
        check("search 结果带时间码", bool(hit.get("start_str")))

    fv = call("frameseek_find_videos", {"q": "的", "limit": 2})
    check("find_videos 返回 ok", fv.get("ok") is True)
    check("find_videos 空结果带 hint 或有视频",
          fv.get("count") or fv.get("hint"))

    print("\n[8] 错误返回带 hint（这是本轮的核心改动）")
    e1 = call("frameseek_video_info", {"video_id": 99999999,
                                       "root": "E:/不存在的库xyz"})
    check("库不存在时报错", e1.get("ok") is False)
    check("库不存在时有 hint", bool(e1.get("hint")), json.dumps(e1, ensure_ascii=False))

    libs2 = call("frameseek_list_libraries").get("libraries") or []
    if libs2:
        p0 = libs2[0]["path"]
        e2 = call("frameseek_video_info", {"video_id": 99999999, "root": p0})
        check("视频 id 不存在时报错", e2.get("ok") is False)
        check("视频 id 不存在时提到「唯一」", "唯一" in (e2.get("hint") or ""),
              json.dumps(e2, ensure_ascii=False))
        e3 = call("frameseek_export_clip", {"line_id": 99999999, "root": p0})
        check("台词 id 不存在时报错", e3.get("ok") is False)
        check("台词 id 不存在时有 hint", bool(e3.get("hint")))
        e4 = call("frameseek_export_clip", {"root": p0})
        check("没给台词时提示怎么给", e4.get("ok") is False and
              "line_id" in (e4.get("err", "") + e4.get("hint", "")))

    e5 = call("frameseek_search", {"query": "绝对不可能存在的台词xyz123",
                                   "all_libraries": True})
    check("搜不到时有排查 hint", bool(e5.get("hint")))

    print("\n[9] 片段清单")
    lc = call("frameseek_list_clips", {"limit": 3})
    check("list_clips 返回 ok", lc.get("ok") is True)
    check("list_clips 带 total", "total" in lc)
    for c in (lc.get("clips") or [])[:1]:
        check("片段条目带绝对路径", os.path.isabs(c.get("path", "")))

    print("\n[10] 烧字幕策略：OCR 源自动不烧（core/media.decide_burn 是唯一判定点）")
    sys.path.insert(0, BASE)
    from core.media import decide_burn
    # OCR 源：画面上本来就有硬字幕，再烧一层会重影 → auto 不烧
    check("auto + ocr 源不烧", decide_burn("auto", "ocr")[0] is False)
    check("auto + ocr 源有说明为什么跳过", bool(decide_burn("auto", "ocr")[1]))
    # 画面没字幕的来源照旧烧（asr / 外挂 SRT / 内嵌字幕）
    for s in ("asr", "external", "embedded"):
        check(f"auto + {s} 源照旧烧", decide_burn("auto", s)[0] is True)
    check("auto + 来源未知不擅自跳过", decide_burn("auto", "")[0] is True)
    # off / force 是显式覆盖
    check("off 一律不烧", decide_burn("off", "asr")[0] is False)
    check("force 突破 OCR 跳过（纠错后重烧）",
          decide_burn("force", "ocr")[0] is True)
    # 脏输入不能炸也不能绕开规则
    check("缺省（None）= auto", decide_burn(None, "ocr")[0] is False)
    check("大写 AUTO 照样识别", decide_burn("AUTO", "ocr")[0] is False)
    check("来源大写 OCR 也认", decide_burn("auto", "OCR")[0] is False)
    check("非法档位回落 auto", decide_burn("瞎传", "ocr")[0] is False)
    check("旧布尔 True = force", decide_burn(True, "ocr")[0] is True)
    check("旧布尔 False = off", decide_burn(False, "asr")[0] is False)

    print("\n[13] 出图：双后端摸底与参数校验（不真出图，只读+入参校验）")
    ii = call("frameseek_img_info")
    check("img_info 正常", ii.get("ok") is True, str(ii)[:100])
    bs = {b["id"]: b for b in (ii.get("backends") or [])}
    check("双后端齐备", set(bs) == {"agnes", "sd15"}, str(set(bs)))
    check("每个后端都报了 ready/eta",
          all("ready" in b and "eta" in b for b in bs.values()))
    check("产物目录是 AI生成/绘画",
          str(ii.get("out_dir", "")).replace("\\", "/").endswith("AI生成/绘画"),
          str(ii.get("out_dir")))

    gi = next(t for t in ts if t["name"] == "frameseek_generate_image")
    gp = (gi.get("inputSchema") or {}).get("properties") or {}
    for k in ("prompt", "backend", "negative", "refs", "width", "height",
              "ratio", "seed", "steps", "cfg", "name"):
        check(f"generate_image 有参数 {k}", k in gp)
    check("generate_image 的 backend 说明了双后端",
          all(x in gp["backend"]["description"] for x in ("agnes", "sd15")))
    check("generate_image 的 refs 说明要绝对路径",
          "绝对路径" in gp["refs"]["description"])

    # 入参校验（这些在起进程前就拦掉，不花 GPU 时间）
    e1 = call("frameseek_generate_image", {"prompt": ""})
    check("空提示词被拦", e1.get("ok") is False)
    e2 = call("frameseek_generate_image", {"prompt": "x", "backend": "nope"})
    check("未知后端被拦", e2.get("ok") is False)
    check("未知后端报出可选值", "agnes" in (e2.get("err", "") + e2.get("hint", "")))
    e3 = call("frameseek_generate_image",
              {"prompt": "x", "width": 500, "height": 512})
    check("非 8 倍数宽度被拦", e3.get("ok") is False)
    e4 = call("frameseek_generate_image",
              {"prompt": "x", "refs": ["Z:/绝对不存在.png"]})
    check("不存在的参考图被拦", e4.get("ok") is False)
    e5 = call("frameseek_reroll_image", {"file": "Z:/不存在.png"})
    check("重掷文件不存在被拦", e5.get("ok") is False)
    check("重掷报错提示用 list_images", "list_images" in e5.get("hint", ""))

    # 产物列表是纯读，安全
    li = call("frameseek_list_images", {"limit": 5})
    check("list_images 正常", li.get("ok") is True)
    check("list_images 返回目录", "dir" in li)
    check("list_images 最多给 limit 条",
          len(li.get("images") or []) <= 5,
          str(len(li.get("images") or [])))

    print("\n[14] 出图：真实出一张（sd15，本地不联网）")
    if bs.get("sd15", {}).get("ready"):
        t0 = time.time()
        gen = call("frameseek_generate_image",
                   {"prompt": "minimalist ink wash mountain, sumi-e style",
                    "backend": "sd15", "width": 256, "height": 256,
                    "seed": 12345, "steps": 8, "name": "_mcp_test"})
        check("真实出图成功", gen.get("ok") is True, str(gen)[:140])
        if gen.get("ok"):
            f = gen.get("file") or ""
            check("产物文件真实存在", bool(f) and os.path.isfile(f), f)
            check("PNG 尺寸正确",
                  (gen.get("w"), gen.get("h")) == (256, 256),
                  f"{gen.get('w')}x{gen.get('h')}")
            check("留痕 sidecar 存在",
                  os.path.isfile(os.path.splitext(f)[0] + ".json"))
            check("返回里带进度轨迹", bool(gen.get("progress")))
            check("提示了重掷入口", "reroll_image" in gen.get("reroll_hint", ""))
            # 种子可复现：同 seed 再出一次应得到同一张
            gen2 = call("frameseek_generate_image",
                        {"prompt": "minimalist ink wash mountain, sumi-e style",
                         "backend": "sd15", "width": 256, "height": 256,
                         "seed": 12345, "steps": 8, "name": "_mcp_test2"})
            if gen2.get("ok"):
                import hashlib
                h1 = hashlib.md5(open(f, "rb").read()).hexdigest()
                h2 = hashlib.md5(open(gen2["file"], "rb").read()).hexdigest()
                check("同 seed 出同一张（可复现）", h1 == h2,
                      f"{h1[:8]} vs {h2[:8]}")
            # 重掷
            rr = call("frameseek_reroll_image", {"file": f})
            check("重掷成功", rr.get("ok") is True, str(rr)[:140])
            if rr.get("ok"):
                check("重掷产新文件", rr.get("file") != f)
                check("重掷记了来源", rr.get("rerolled_from") == f)
        print(f"     （真实出图耗时 {time.time() - t0:.0f} 秒）")
    else:
        check("sd15 未就绪（跳过真实出图）", True,
              "本机没装 SD1.5 权重")

    # 清理测试产物
    for pat in ("_mcp_test.png", "_mcp_test.json",
                "_mcp_test2.png", "_mcp_test2.json"):
        try:
            pd = call("frameseek_list_images", {"limit": 200}).get("dir") or ""
            os.remove(os.path.join(pd, pat))
        except OSError:
            pass

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)}　失败 {len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
    sys.exit(rc)