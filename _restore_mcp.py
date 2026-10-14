# -*- coding: utf-8 -*-
"""恢复 ~/.workbuddy/mcp.json 里的 frameseek 条目（原始配置 + 图标），并验证信任指纹。"""
import hashlib
import json
import urllib.parse

MCP_JSON = r"C:\Users\Administrator\.workbuddy\mcp.json"
ICON_FILE = r"D:\project\AIProjects\frameseek\_out\logo_datauri.txt"
EXPECT_HASH = "4c4aaef426dc6354c9801e998657ad6e4bda642c7b420fd3d318287ff28b7f25"

entry = {
    "command": r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe",
    "args": [r"D:\project\AIProjects\frameseek\mcp_server.py"],
    "x-workbuddy": {
        "displayName": "FrameSeek",
        "description": "本地视频素材库：台词检索 / 方框校验 / 片段导出 / AI 视频生成",
        "icon": open(ICON_FILE, encoding="utf-8").read().strip(),
    },
}
cfg = {"mcpServers": {"frameseek": entry}}

with open(MCP_JSON, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("written:", MCP_JSON)

# ---- 验证 1：JSON 合法 + 指纹匹配 ----
back = json.load(open(MCP_JSON, encoding="utf-8"))
e = back["mcpServers"]["frameseek"]
material = "|".join([
    e["command"],
    ",".join(sorted(map(str, e.get("args") or []))),
    ",".join(sorted((e.get("env") or {}).keys())),
] + ([e["cwd"]] if e.get("cwd") else []))
h = hashlib.sha256(material.encode("utf-8")).hexdigest()
print("trustMaterial =", repr(material))
print("sha256        =", h)
print("期望指纹      =", EXPECT_HASH)
assert h == EXPECT_HASH, "指纹不匹配！信任记录不会命中"
print("✅ 指纹匹配 —— 05:24 的信任记录直接生效，无需重新点“信任”")

# ---- 验证 2：图标 data URI 可解码 ----
icon = e["x-workbuddy"]["icon"]
assert icon.startswith("data:image/svg+xml,")
svg = urllib.parse.unquote(icon.split(",", 1)[1])
assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
print("✅ 图标 data URI 可解码，SVG", len(svg), "字符")
