# -*- coding: utf-8 -*-
"""MCP Server 端到端测试（stdio JSON-RPC）。

用法： python _mcp_e2e.py
覆盖：initialize → notifications/initialized → tools/list →
      只读工具（state / libraries / search / list_videos / get_lines /
      verify / boxaudit）→ 异步工具（scan）→ task 轮询 → 退出。
只动 _testlib（测试库，内容已入库，扫描按 md5 全部跳过）。
"""
import json
import os
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SERVER = os.path.join(BASE, "mcp_server.py")
TESTLIB = os.path.join(BASE, "_testlib")

_env = dict(os.environ)
_env["PYTHONIOENCODING"] = "utf-8"
_env["PYTHONUTF8"] = "1"
_env["PYTHONUNBUFFERED"] = "1"

proc = subprocess.Popen(
    [PY, SERVER], cwd=BASE, env=_env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

# 持续抽干 stderr：SDK 打 traceback 很啰嗦，管道写满会把服务端事件循环
# 憋死（表现就是客户端干等不到响应），必须有人一直读。
_stderr_lines: list = []


def _drain_stderr():
    for line in proc.stderr:
        _stderr_lines.append(line)


threading.Thread(target=_drain_stderr, daemon=True).start()

_next_id = [0]
fails = []


def send(method, params=None, notify=False):
    if notify:
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
    else:
        _next_id[0] += 1
        msg = {"jsonrpc": "2.0", "id": _next_id[0],
               "method": method, "params": params or {}}
    line = json.dumps(msg, ensure_ascii=False)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()
    return msg.get("id")


def recv(want_id, timeout=90):
    """读一行 JSON，按 id 匹配（跳过通知/其它 id 就继续读）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server 关闭了 stdout（进程可能崩了）")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print("  [非JSON行]", line[:120])
            continue
        if msg.get("id") == want_id:
            return msg
        # 服务器主动推的通知（notifications/*）直接忽略
    raise TimeoutError(f"等 id={want_id} 超时")


def call(name, args=None, timeout=90):
    rid = send("tools/call", {"name": name, "arguments": args or {}})
    resp = recv(rid, timeout)
    if "error" in resp:
        fails.append(f"{name}: JSON-RPC error {resp['error']}")
        print(f"✗ {name} → JSON-RPC error: {resp['error']}")
        return None
    res = resp.get("result") or {}
    if res.get("isError"):
        txt = ""
        for c in res.get("content") or []:
            txt += c.get("text", "")
        fails.append(f"{name}: tool error {txt[:200]}")
        print(f"✗ {name} → tool error: {txt[:300]}")
        return None
    txt = "".join(c.get("text", "") for c in (res.get("content") or []))
    try:
        data = json.loads(txt)
    except Exception:
        fails.append(f"{name}: 返回不是 JSON: {txt[:200]}")
        print(f"✗ {name} → 非 JSON 返回: {txt[:200]}")
        return None
    return data


def check(cond, label):
    mark = "✓" if cond else "✗"
    if not cond:
        fails.append(label)
    print(f"  {mark} {label}")
    return cond


try:
    # ---- 握手 ----
    rid = send("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "frameseek-e2e", "version": "1.0"}})
    resp = recv(rid)
    srv = (resp.get("result") or {}).get("serverInfo") or {}
    check(bool(srv.get("name")), f"initialize 握手成功（serverInfo={srv}）")
    send("notifications/initialized", notify=True)

    # ---- tools/list ----
    rid = send("tools/list")
    resp = recv(rid)
    tools = (resp.get("result") or {}).get("tools") or []
    names = [t["name"] for t in tools]
    print(f"  tools/list → {len(tools)} 个工具:")
    for t in tools:
        req = ", ".join((t.get("inputSchema") or {}).get("required") or [])
        print(f"    - {t['name']}" + (f"（必填: {req}）" if req else ""))
    check(len(tools) >= 15, f"工具数 {len(tools)} >= 15")

    # ---- 只读工具 ----
    d = call("frameseek_state")
    check(bool(d and d.get("ok")), "frameseek_state 返回 ok")
    if d:
        print(f"    库 {len(d['libraries'])} 个 / 台词 {d['total']['lines']} 条 / "
              f"ocr_step={d['ocr_step']} / asr.device={d['asr'].get('device') or '?'}")

    d = call("frameseek_list_libraries")
    check(bool(d and d.get("libraries")), "frameseek_list_libraries 有库")

    d = call("frameseek_search", {"query": "功夫", "limit": 5})
    n = len((d or {}).get("results") or [])
    check(n > 0, f"frameseek_search('功夫') 命中 {n} 条")
    hit = ((d or {}).get("results") or [{}])[0]
    print(f"    首条: [{hit.get('video')} {hit.get('start_str')}] {hit.get('text')}")

    d = call("frameseek_list_videos", {"root": TESTLIB, "limit": 20})
    vids = (d or {}).get("videos") or []
    check(len(vids) > 0, f"frameseek_list_videos(_testlib) 有 {len(vids)} 个素材")
    # 库里有 AI 生成的风景视频（无语音无字幕，0 条台词）——挑台词最多的那个测，
    # 不然 get_lines/boxaudit 会拿到空结果误判成协议 bug
    vids.sort(key=lambda x: -(x.get("lines") or 0))
    vid = vids[0]["id"] if vids else 1
    print(f"    首素材: {vids[0]['name']} / {vids[0]['lines']} 条台词 / "
          f"boxed={vids[0]['boxed']} no_box={vids[0]['no_box']}")

    d = call("frameseek_video_info", {"root": TESTLIB, "video_id": vid})
    check(bool(d and d.get("video")), "frameseek_video_info 返回详情")

    d = call("frameseek_get_lines", {"root": TESTLIB, "video_id": vid, "limit": 5})
    lines = (d or {}).get("lines") or []
    check(len(lines) > 0, f"frameseek_get_lines 拿到 {len(lines)} 条")
    if lines:
        b = lines[0].get("box") or {}
        print(f"    首条: [{lines[0]['start_str']}] {lines[0]['text']} "
              f"font_dev={b.get('font_dev')}")

    d = call("frameseek_verify", {"root": TESTLIB, "video_id": vid})
    check(bool(d and "rate" in d),
          f"frameseek_verify rate={d.get('rate') if d else '?'} "
          f"({d.get('passed') if d else '?'}/{d.get('checked') if d else '?'} 通过, "
          f"skipped={d.get('skipped') if d else '?'})")

    d = call("frameseek_boxaudit", {"root": TESTLIB, "video_id": vid, "limit": 3})
    items = (d or {}).get("items") or []
    check(len(items) > 0, f"frameseek_boxaudit 明细 {len(items)} 条 "
                          f"(总 {d.get('total')}, 通过率 {d.get('rate')})")

    d = call("frameseek_stats")
    check(bool(d and d.get("lines", 0) > 0), "frameseek_stats 有台词数")

    # ---- 生成相关工具（不真生成：info 实调 + 校验路径） ----
    d = call("frameseek_gen_info")
    check(bool(d and d.get("ready") is not None and not d.get("err")),
          "frameseek_gen_info 返回引擎现状")
    if d:
        print(f"    引擎: {d.get('engine')} ready={d.get('ready')} "
              f"宽高比 {len(d.get('aspects') or [])} 种 / 落点 {d.get('gen_subdir')}")

    d = call("frameseek_generate_video",
             {"prompt": "x", "aspect": "99:1", "root": TESTLIB})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"非法宽高比被挡回: {d.get('err') if d else '?'}")
    d = call("frameseek_generate_video",
             {"prompt": "y" * 2001, "root": TESTLIB})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"超长 prompt 被挡回: {d.get('err') if d else '?'}")
    d = call("frameseek_generate_video",
             {"prompt": "z", "index": "bogus", "root": TESTLIB})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"非法 index 被挡回: {d.get('err') if d else '?'}")

    # ---- 白板手绘工具（info 实调 + 参数校验，不真渲染） ----
    d = call("frameseek_handdraw_info")
    check(bool(d and d.get("ok") and d.get("engine")), "frameseek_handdraw_info 返回 ok")
    if d:
        print(f"    手绘引擎: {d.get('engine')} ready={d.get('ready')} "
              f"缺={d.get('missing') or '无'} 音色={d.get('default_voice')}")

    d = call("frameseek_handdraw_init", {"script": "no_such_file.txt", "root": TESTLIB})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"手绘 init 缺文件被挡回: {d.get('err') if d else '?'}")
    d = call("frameseek_handdraw_init", {"script": "", "srt": "", "root": TESTLIB})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"手绘 init 空输入被挡回: {d.get('err') if d else '?'}")
    d = call("frameseek_handdraw_scenes", {"project": "不存在的工程", "root": TESTLIB})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"手绘 scenes 工程不存在被挡回: {d.get('err') if d else '?'}")
    d = call("frameseek_handdraw_render", {"project": "不存在的工程",
                                           "scene_id": "scene-01", "root": TESTLIB})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"手绘 render 工程不存在被挡回: {d.get('err') if d else '?'}")
    d = call("frameseek_handdraw_voice", {"text": "试听"})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"手绘 voice 无联网许可被挡回: {d.get('err') if d else '?'}")
    d = call("frameseek_handdraw_annotate",
             {"project": "不存在的工程", "scene_id": "s1", "image": "x.png",
              "annotation": "{坏JSON", "root": TESTLIB})
    check(bool(d and not d.get("ok") and d.get("err")),
          f"手绘 annotate 坏 JSON 被挡回: {d.get('err') if d else '?'}")

    # ---- 异步工具 + 进度轮询 ----
    d = call("frameseek_scan", {"path": TESTLIB})
    started = bool(d and d.get("started"))
    check(started, f"frameseek_scan(_testlib) 已启动: {d}")
    ok_task = False
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(3)
        d = call("frameseek_task", {"path": TESTLIB}, timeout=30)
        lt = (d or {}).get("latest") or {}
        st = lt.get("status")
        print(f"    task 轮询: status={st} done={lt.get('done')}/{lt.get('total')} "
              f"msg={lt.get('message', '')[:60]}")
        if st in ("done", "error", ""):
            ok_task = True
            break
    check(ok_task, "frameseek_task 轮询到任务结束")

    # ---- ping ----
    rid = send("ping")
    resp = recv(rid, timeout=15)
    check("result" in resp, "ping 通")

finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    try:
        err = "".join(_stderr_lines)
    except Exception:
        err = ""
    if err.strip():
        print("---- server stderr（前 1500 字）----")
        print(err[:1500])

print()
if fails:
    print(f"结果：{len(fails)} 项失败")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("结果：全部通过 ✓")
