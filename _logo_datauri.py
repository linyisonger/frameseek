# -*- coding: utf-8 -*-
"""从 web/logo.svg 生成 URL 编码的 data URI，供 favicon / mcp.json 使用。"""
import urllib.parse

src = open("web/logo.svg", encoding="utf-8").read().strip()
uri = "data:image/svg+xml," + urllib.parse.quote(src, safe="/:<>=\"' ")
print(uri)
print("---- 长度:", len(uri))
