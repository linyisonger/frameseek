# -*- coding: utf-8 -*-
"""Playwright 启动辅助（回归脚本共用）。

坑：本机 ms-playwright 里装的 chromium 版本号经常跟 site-packages 里的
playwright 期望的对不上（playwright 1.63 要 chromium-1243，本机只有 1223），
直接 launch() 会报 "Executable doesn't exist"。
这里先试默认，失败就退回本机已装的最新 chromium。
"""
import glob
import os


def _installed():
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    hits = glob.glob(os.path.join(base, "chromium-*", "chrome-win*", "chrome.exe"))
    # 版本号大的优先
    def key(p):
        seg = os.path.basename(os.path.dirname(os.path.dirname(p)))
        n = seg.replace("chromium-", "")
        return int(n) if n.isdigit() else -1
    return sorted(hits, key=key, reverse=True)


def launch(p, **kw):
    try:
        return p.chromium.launch(**kw)
    except Exception:
        for exe in _installed():
            try:
                return p.chromium.launch(executable_path=exe, **kw)
            except Exception:
                continue
        raise
