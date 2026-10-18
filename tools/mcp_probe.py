# -*- coding: utf-8 -*-
"""MCP 工具面探针：看 LLM 实际看到的 tools/list 到底长什么样。

用法： python tools/mcp_probe.py [--tools] [--call 名称] [--args JSON]
它起一个真实 stdio 子进程，走完 initialize → tools/list，
把每个工具的 name / description 长度 / 入参 schema 打出来。
重点查三件事：
  1. 描述里有没有写清「前置依赖」和「坑」（LLM 最容易漏的就是这两样）
  2. 参数名是否自解释（use_asr 这种历史名字是不是还在误导）
  3. 有没有工具覆盖 LLM 常见的诉求（找视频文件、看画面、列已导出片段）
"""
import json
import os
import subprocess
import sys
import threading

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

# stderr 必须持续抽干，否则管道写满会把服务端事件循环憋死
def _drain():
    for line in proc.stderr:
        pass


threading.Thread(target=_drain, daemon=True).start()

_id = [0]


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


def recv(want_id, timeout=60):
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
        if m.get("id") == want_id:
            return m


def boot():
    i = send("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "mcp_probe", "version": "1"}})
    r = recv(i)
    send("notifications/initialized", {}, notify=True)
    return r


def list_tools():
    i = send("tools/list")
    r = recv(i)
    return r.get("result", {}).get("tools", [])


def call(name, args):
    i = send("tools/call", {"name": name, "arguments": args or {}})
    r = recv(i, timeout=180)
    if "error" in r:
        return {"__rpc_error": r["error"]}
    res = r.get("result", {})
    txt = "".join(c.get("text", "") for c in res.get("content", [])
                  if c.get("type") == "text")
    return {"isError": res.get("isError"), "text": txt}


def main():
    boot()
    tools = list_tools()
    args = sys.argv[1:]

    if "--tools" in args or not args:
        print(f"共 {len(tools)} 个工具\n" + "=" * 72)
        for t in tools:
            d = (t.get("description") or "").strip()
            sch = t.get("inputSchema") or {}
            props = sch.get("properties") or {}
            req = set(sch.get("required") or [])
            print(f"\n■ {t['name']}  ({len(d)} 字描述)")
            first = d.split("\n")[0] if d else "(空)"
            print(f"  首行: {first[:110]}")
            sig = []
            for k, v in props.items():
                ty = v.get("type", "?")
                mark = "*" if k in req else ""
                de = (v.get("description") or "").strip().replace("\n", " ")
                sig.append(f"{k}{mark}:{ty}" + (f"({de[:40]})" if de else ""))
            print(f"  参数: {' '.join(sig) if sig else '(无)'}")
        return

    if "--schema" in args:
        name = args[args.index("--schema") + 1]
        for t in tools:
            if t["name"] == name:
                print(json.dumps(t, ensure_ascii=False, indent=2))
        return

    if "--call" in args:
        name = args[args.index("--call") + 1]
        a = {}
        if "--args" in args:
            a = json.loads(args[args.index("--args") + 1])
        print(json.dumps(call(name, a), ensure_ascii=False, indent=2))
        return

    if "--raw" in args:
        i = send("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "p", "version": "1"}})
        print(json.dumps(recv(i), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()