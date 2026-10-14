# -*- coding: utf-8 -*-
"""用 app.asar 实锤的 trustMaterial 格式反推 frameseek 原始配置。

trustMaterial = [command, args.map(String).sort().join(","),
                 Object.keys(env).sort().join(","), ...(cwd?[cwd]:[])].join("|")
目标：sha256 -> 4c4aaef426dc6354c9801e998657ad6e4bda642c7b420fd3d318287ff28b7f25
"""
import hashlib
import itertools

TARGET = "4c4aaef426dc6354c9801e998657ad6e4bda642c7b420fd3d318287ff28b7f25"
CC_TARGET = "af4a39906e0b7a427b589392b3c1835f93892a3835c90294f891df9b927602f3"

CMDS_FS = [
    r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe",
    "C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe",
]
ARGS_FS = [
    [r"D:\project\AIProjects\frameseek\mcp_server.py"],
    ["D:/project/AIProjects/frameseek/mcp_server.py"],
]
CWDS = [None, r"D:\project\AIProjects\frameseek", "D:/project/AIProjects/frameseek"]
ENVS = [{}, {"PYTHONIOENCODING": "utf-8"}, {"PYTHONUTF8": "1"},
        {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}]

CC_CMDS = ["node", r"C:\Program Files\nodejs\node.exe",
           "C:/Program Files/nodejs/node.exe"]
CC_ARGS = [[r"D:\project\AIProjects\ChatCut\bridge\server.mjs"],
           ["D:/project/AIProjects/ChatCut/bridge/server.mjs"]]


def trust_material(cmd, args, env, cwd):
    return "|".join([cmd, ",".join(sorted(map(str, args))),
                     ",".join(sorted(env.keys()))] + ([cwd] if cwd else []))


def crack(target, cmds, args_list, cwds, envs, label):
    n = 0
    for cmd, args, cwd, env in itertools.product(cmds, args_list, cwds, envs):
        n += 1
        m = trust_material(cmd, args, env, cwd)
        if hashlib.sha256(m.encode("utf-8")).hexdigest() == target:
            print(f"[HIT] {label}")
            print(f"  command = {cmd}")
            print(f"  args    = {args}")
            print(f"  env     = {env}")
            print(f"  cwd     = {cwd}")
            print(f"  material= {m!r}")
            return (cmd, args, env, cwd)
    print(f"[MISS] {label}（试了 {n} 种组合）")
    return None


hit = crack(TARGET, CMDS_FS, ARGS_FS, CWDS, ENVS, "frameseek")
if not hit:
    crack(CC_TARGET, CC_CMDS, CC_ARGS, [None, r"D:\project\AIProjects\ChatCut"],
          [{}], "chatcut-local")
