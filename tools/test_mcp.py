# -*- coding: utf-8 -*-
"""MCP server 回归测试（stdio JSON-RPC，真实子进程）。

用法： python tools/test_mcp.py
覆盖：
  - 服务能起来、tools/list 有 22 个工具、每个工具参数都有 description
  - resources/list 有 4 个资源，且都能读出来
  - 只读工具端到端：doctor / libraries / search / find_videos / list_clips
  - 新增能力：export_clip 的 subtitle（auto/off/force 三档）与批量参数进了 schema
  - 烧字幕策略：OCR 源在 auto 档自动跳过（core/media.decide_burn 的 12 个分支）
  - 错误返回带 hint（LLM 撞得最多的场景：id 找不到 / 未指定库）
  - 查库类工具的错误一律带 root 相关提示

只读为主，不跑异步任务（不产生副作用）。唯一会写文件的是 export_clip
（测试库 clips 目录，同名覆盖）。
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
    check(f"工具数 = 22（实际 {len(ts)}）", len(ts) == 22)
    must = {"frameseek_doctor", "frameseek_find_videos", "frameseek_video_summary",
            "frameseek_list_clips", "frameseek_export_clip", "frameseek_search"}
    check("新增/核心工具都在", must <= names, str(must - names))

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
            "frameseek://clips", "frameseek://tool-catalog"}
    check(f"4 个资源都在（实际 {len(rs)}）", want <= uris, str(want - uris))
    for u in sorted(uris):
        d = read_res(u)
        check(f"资源可读 {u}", bool(d) and "__text" not in d)



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