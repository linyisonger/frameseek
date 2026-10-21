# -*- coding: utf-8 -*-
"""构建 core/hanzi.py 里的简繁单字表。**构建期**脚本，运行时不需要。

依赖（临时装进 .workbuddy/tmpdicts，不进项目依赖）：
  pip install --target .workbuddy/tmpdicts zhconv opencc-python-reimplemented \
      --index-url https://pypi.org/simple

为什么两张字典要合：
  - opencc 的 STCharacters.txt 给出「简体 -> 繁体候选（按频率排）」，但它只给
    首选形体的**一部分**：TSCharacters.txt 里「為」也映射到「为」，STCharacters
    却只写了「爲」。只按 ST 单向建表，库里写「為所欲為」、用户搜「为所欲为」
    就搜不到。所以繁体候选要 = ST 候选 ∪ TS 反查（t2s首选 == 该简体）。
  - zhconv 的 zh-hant 与 opencc 大体一致，但有 7 条 opencc 没有的（喂餵、踊踴
    等），合并进来补缺口。
  只用「简体 -> 繁体」方向建表、繁->简由它反向推导：反向字典（繁->简）是多对
  一，里面混了大量异体字（「緐/枱/檯/颱」都归到「繁/台」），直接拿它反过来用
  会生成「緐體字」这种噪音变体。这里对反查做了约束：只有当某个繁体的 t2s 首选
  就是该简体时才收，异体错配进不来。
"""
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMPD = os.path.join(BASE, ".workbuddy", "tmpdicts")
HANZI = os.path.join(BASE, "core", "hanzi.py")

# 常见形体优先：opencc 的首选取的是「正字」，跟影视字幕的实际用字不完全一致
# （最典型：opencc 给「爲」，字幕几乎都写「為」）。变体太多、查询被裁剪时，
# 保留的是排在前面的形体，所以这批高频字要手动把常见写法顶到第一位。
# 每 2 字一组：简体 + 更常见的繁体。
_FIRST = (
    "为為面麵线線里裡说說阅閱颜顏鸡雞绝絕绿綠湿濕苏蘇证證岁歲画畫药藥馆館"
    "赞贊术術录錄历歷参參宁寧伪偽众眾产產尽盡坛壇墙牆壳殼别別净淨叹嘆启啟"
    "团團奖獎尝嘗获獲复復干幹当當恶惡毁毀签簽竖豎闲閒须須迹跡采採鉴鑑针針"
    "钻鑽铲鏟锈鏽锐銳锤錘镇鎮镰鐮钟鐘"
)


TEMPLATE = '''# -*- coding: utf-8 -*-
"""简繁单字对应表 —— 专供台词检索做「简繁互通」。

为什么自己带表而不装第三方包：检索是 FrameSeek 的主路径，不该为一次字符
映射引入运行时依赖（换机器部署、离线环境都要能用）。这张表是**构建期**由
tools/scratch/_build_hanzi.py 合并 opencc（STCharacters/TSCharacters）与
zhconv（zh-hant）导出的静态数据，运行时零依赖、纯查表。

两个方向：
  S2T：简体 -> [繁体, ...]，按常见程度排，查询裁剪时优先留前面的形体。
  T2S：繁体 -> 简体，由 S2T 反向推导（一个繁体只归一个简体，取首选）。

为什么繁体候选 = opencc 首选 ∪ 繁->简反查：opencc 的 STCharacters 只写了
「为 -> 爲」，但 TSCharacters 里「為」同样映射到「为」。只按单向建表的话，库
里写「為所欲為」、用户打简体「为所欲为」就搜不到（真被咬过一次）。反查加了
约束——只有某个繁体的「繁->简」首选正好是该简体才收，所以「緐->繁」这类异体
错配进不来，不会生成「緐體字」这种噪音变体。

只做**字符级**映射，不做词汇级转换（「软件/軟體」这类用词差异不处理）：检索
要的是「同一个字的两种写法能互相搜到」，字符级足够，而且不会因为词汇替换把
用户输入的原文改得认不出来。

存储：`_S2T` 用 `|` 分组，组内第一字是简体、其余是它的繁体候选；`_FIRST` 每
2 字一组，把影视字幕里更常见的写法顶到候选第一位。
"""
import itertools

_S2T = "@@TABLE@@"

# 常见形体优先（每 2 字一组：简体 + 更常见的繁体）。opencc 的首选取的是「正
# 字」，跟字幕实际用字不完全一致（最典型：opencc 给「爲」，字幕几乎都写
# 「為」）。长查询的变体会被裁剪，排在前面的形体才留得下来。
_FIRST = "@@FIRST@@"

S2T = {}   # 简体字 -> [繁体字, ...]（按常见程度排）
T2S = {}   # 繁体字 -> 简体字（由 S2T 反向推导）


def _build():
    for grp in _S2T.split("|"):
        if len(grp) >= 2:
            S2T[grp[0]] = list(grp[1:])
    for i in range(0, len(_FIRST), 2):
        s, t = _FIRST[i], _FIRST[i + 1]
        if s in S2T and t in S2T[s]:
            S2T[s].remove(t)
            S2T[s].insert(0, t)
    # 先让「首选繁体」占位，保证 t2s 反查取到的是最常见的那个简体
    for s in S2T:
        if S2T[s]:
            T2S.setdefault(S2T[s][0], s)
    for s, ts in S2T.items():
        for t in ts:
            T2S.setdefault(t, s)


_build()
del _build


def to_simplified(q: str) -> str:
    """整串转简体（逐字查表，查不到原样保留）。"""
    return "".join(T2S.get(c, c) for c in (q or ""))


def to_traditional(q: str) -> str:
    """整串转繁体。一个简体字有多个繁体时取最常见的那个。"""
    return "".join((S2T.get(c) or [c])[0] for c in (q or ""))


def has_variant(q: str) -> bool:
    """这串有没有简繁异体——没有就不展开，省掉多余的查询分支。"""
    return any(c in S2T or c in T2S for c in (q or ""))


def _combos(opts):
    n = 1
    for o in opts:
        n *= len(o)
    return n


def expand(q: str, max_n: int = 96) -> list:
    """返回 q 的简繁变体（含 q 本身），给检索做 OR 展开。

    逐字笛卡尔积：这样连「简體字」这种繁简混写的库内文本也能命中——只生成
    「全简 + 全繁」两个方向是盖不住混写的。

    组合数随字数指数增长（每字 2~4 个形体），超过 max_n 就逐级裁剪：先把每字
    削到「原字 + 最常见的一个异体」，还超就继续把异体最少的字削回原字。这样
    长词不会退化成只有「全简/全繁」两个方向——那条老路漏过「為所欲為」，因为
    四字词展开 81 个组合时直接被丢到只留首选形体了。
    """
    q = q or ""
    if not q:
        return []
    opts = []
    for c in q:
        alt = []
        for t in S2T.get(c, []):
            if t != c and t not in alt:
                alt.append(t)
        s = T2S.get(c)
        if s and s != c and s not in alt:
            alt.append(s)
        opts.append([c] + alt)
    if _combos(opts) <= max_n:
        return sorted({q} | {"".join(p) for p in itertools.product(*opts)})
    cut = [o[:2] for o in opts]
    while _combos(cut) > max_n:
        multi = [i for i, o in enumerate(cut) if len(o) > 1]
        if not multi:
            break
        # 异体最少的字先削：它的形体差异对召回的贡献最小
        multi.sort(key=lambda i: len(opts[i]))
        cut[multi[0]] = cut[multi[0]][:1]
    out = {q, to_simplified(q), to_traditional(q)}
    out |= {"".join(p) for p in itertools.product(*cut)}
    return sorted(out)
'''


def load_opencc():
    st, ts = {}, {}
    with io.open(os.path.join(TMPD, "opencc", "dictionary", "STCharacters.txt"),
                 encoding="utf-8") as f:
        for ln in f:
            a = ln.rstrip("\n").split("\t")
            if len(a) == 2 and len(a[0]) == 1:
                st[a[0]] = a[1].split()
    with io.open(os.path.join(TMPD, "opencc", "dictionary", "TSCharacters.txt"),
                 encoding="utf-8") as f:
        for ln in f:
            a = ln.rstrip("\n").split("\t")
            if len(a) == 2 and len(a[0]) == 1:
                ts[a[0]] = a[1].split()
    return st, ts


def load_old():
    """读现有 hanzi.py 里的旧表（zhconv 视角），作为补缺来源。"""
    src = io.open(HANZI, encoding="utf-8").read()
    m = re.search(r'_S2T\s*=\s*"([^"]*)"', src)
    if not m:
        return {}
    s = m.group(1)
    old = {}
    for i in range(0, len(s), 2):
        old.setdefault(s[i], []).append(s[i + 1])
    return old


def main():
    st, ts = load_opencc()
    old = load_old()
    # 繁体 -> 简体（只认 t2s 首选，挡掉「緐->繁」这类异体错配）
    rev = {}
    for t, ss in ts.items():
        if ss and ss[0] != t:
            rev.setdefault(ss[0], []).append(t)

    keys = set(st) | set(old)
    out = {}
    for s in sorted(keys):
        cand = []
        for src in (st.get(s, []), rev.get(s, []), old.get(s, [])):
            for t in src:
                if t != s and t not in cand:
                    cand.append(t)
        if not cand:
            continue
        # _FIRST 里点名的字：把常见形体顶到第一
        out[s] = cand

    first = {}
    for i in range(0, len(_FIRST), 2):
        first[_FIRST[i]] = _FIRST[i + 1]
    for s, t in first.items():
        if s in out and t in out[s]:
            out[s].remove(t)
            out[s].insert(0, t)
        elif s in out:
            out[s].insert(0, t)

    groups = "|".join(s + "".join(v) for s, v in
                      sorted(out.items(), key=lambda kv: kv[0]))
    n_char = sum(len(v) for v in out.values())
    print("简字数 %d，简繁对应 %d 组，串长 %d" % (len(out), n_char, len(groups)))
    print("抽查 为:", out.get("为"), " 台:", out.get("台"), " 面:", out.get("面"))
    src = TEMPLATE.replace("@@TABLE@@", groups).replace("@@FIRST@@", _FIRST)
    io.open(HANZI, "w", encoding="utf-8").write(src)
    print("已写出 core/hanzi.py")


if __name__ == "__main__":
    sys.exit(main())
