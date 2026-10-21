# -*- coding: utf-8 -*-
"""检索页「素材库下拉宽度 + 简繁互通」冒烟。

跑：先 python app.py，再 python tools/scratch/_libpick_check.py
量三件事：
  1. #libpick 的选择框宽度（不能把整行撑满，也不能窄到看不出内容）
  2. 展开下拉后面板的宽度（长目录名不能把面板顶出屏幕）
  3. 打简体「为所欲为」能搜到库里的繁体「為所欲為」
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("FS_BASE", "http://127.0.0.1:8771")
OUT = os.path.abspath("_out/libpick")
os.makedirs(OUT, exist_ok=True)


def main():
    with sync_playwright() as p:
        # 本机 playwright 自带的 chromium 版本号对不上，直接借系统 Edge
        b = p.chromium.launch(channel="msedge")
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(BASE + "/#search", wait_until="networkidle")
        pg.wait_for_timeout(800)

        # 服务可能因为没输入而没渲染，等一下搜索区
        # 注意：el-select 把 id 透传给了内部那个窄窄的隐藏 input，id 选择器
        # 量到的是 11px 的 input，不是可见的选择框——量根元素（class 落在根上）
        box = pg.locator(".fs-libpick").bounding_box()
        print("选择框宽度: %.0fpx" % box["width"])

        pg.locator(".fs-libpick").click()
        pg.wait_for_timeout(400)
        pop = pg.locator(".fs-libpick-pop").first.bounding_box()
        print("下拉面板宽度: %.0fpx" % (pop["width"] if pop else -1))
        pg.screenshot(path=os.path.join(OUT, "libpick_open.png"))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)

        # 简繁互通：库里有繁体「為所欲為」，这里打简体
        pg.fill("#q", "为所欲为")
        pg.click("#btnSearch")
        pg.wait_for_timeout(1500)
        n = pg.locator("#results .fs-rrow").count()
        txt = pg.locator("#resultSum").inner_text() if pg.locator("#resultSum").count() else ""
        print("搜「为所欲为」结果: %d 条 | %s" % (n, txt.replace("\n", " ")[:60]))
        pg.screenshot(path=os.path.join(OUT, "search_hant.png"), full_page=False)
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
