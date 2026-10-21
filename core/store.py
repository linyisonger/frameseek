# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - SQLite 数据层

库文件放在每个素材文件夹下的 <root>/.frameseek/index.db，随素材走，拷走文件夹索引也跟着走。
台词表用 FTS5(trigram) 建全文索引，中文子串查询也能走索引。
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

from core import config as C
from core import hanzi

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS videos(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rel TEXT NOT NULL,              -- 相对 root 的路径
    name TEXT NOT NULL,
    md5 TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    duration REAL,
    width INTEGER,
    height INTEGER,
    fps REAL,                       -- 帧率（探测，素材页筛选用）
    vcodec TEXT,                    -- 视频编码
    acodec TEXT,                    -- 音频编码
    bitrate INTEGER,                -- 整体码率 bps
    cover TEXT,                     -- 相对 .frameseek 的路径
    gif TEXT,                       -- 相对 .frameseek 的路径（动图预览）
    line_count INTEGER DEFAULT 0,
    line_source TEXT,               -- embedded / asr / none
    status TEXT DEFAULT 'ok',       -- ok / pending / error
    err TEXT,
    created_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_md5 ON videos(md5);
CREATE INDEX IF NOT EXISTS idx_videos_rel ON videos(rel);

CREATE TABLE IF NOT EXISTS lines(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    seq INTEGER,
    start REAL NOT NULL,
    end REAL,
    text TEXT NOT NULL,
    shot TEXT,                      -- 相对 .frameseek 的路径（该台词对应截图）
    box TEXT,                       -- 方框凭证（JSON）：这条字幕在画面上的框几何，
                                    -- 见 core/verify.py。外挂SRT/内嵌字幕没有，存 NULL
    src TEXT                        -- 来源：external/embedded/ocr/asr
);
CREATE INDEX IF NOT EXISTS idx_lines_video ON lines(video_id);
CREATE INDEX IF NOT EXISTS idx_lines_start ON lines(start);

CREATE VIRTUAL TABLE IF NOT EXISTS lines_fts USING fts5(
    text, content='lines', content_rowid='id', tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS ignored(
    md5 TEXT PRIMARY KEY,       -- 从库中移除、但磁盘文件还留着的素材
    name TEXT,
    rel TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    root TEXT,
    status TEXT,                    -- running / done / error / stopped / interrupted
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    message TEXT,
    created_at REAL,
    updated_at REAL
);
"""


def fmt_ts(sec: float) -> str:
    sec = max(0.0, float(sec or 0))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:06.3f}"
    return f"{m:02d}:{s:06.3f}"


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self):
        """短连接：用完就关。

        以前 `with self._conn() as conn` 只 commit、**不 close**，连接要等 GC 才回收；
        boxaudit 这种一次请求开上百条连接的场景会把读锁堆起来，之后每条新连接执行
        `PRAGMA journal_mode=WAL` 都要等锁，实测单条 box_stats 从毫秒涨到 2 秒。
        这里显式 close，并把 journal_mode 只在 _init 里设一次。
        """
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self):
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('schema','1')")
            conn.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('app',?)", (C.APP_NAME,))
            self._migrate(conn)

    def _migrate(self, conn):
        """给既有库补上后加的列。

        CREATE TABLE IF NOT EXISTS 对已存在的表是空操作，所以老库的 lines
        表不会自动多出 box / src 两列。这里读 PRAGMA table_info 比对，
        缺哪列补哪列——用户已有的 65 部片的索引库不用重建。
        """
        want = {
            "lines": [("box", "TEXT"), ("src", "TEXT")],
            # 2026-10-15：素材页要按媒体属性筛选，补齐探测字段
            "videos": [("fps", "REAL"), ("vcodec", "TEXT"), ("acodec", "TEXT"),
                       ("bitrate", "INTEGER")],
        }
        for table, cols in want.items():
            have = {r["name"] for r in conn.execute(
                f"PRAGMA table_info({table})")}
            if not have:
                continue
            for name, decl in cols:
                if name not in have:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ---------------- videos ----------------
    def find_by_md5(self, md5: str):
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM videos WHERE md5=?", (md5,)).fetchone()
            return dict(r) if r else None

    def find_by_rel(self, rel: str):
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM videos WHERE rel=?", (rel,)).fetchone()
            return dict(r) if r else None

    def add_video(self, v: Dict[str, Any]) -> int:
        cols = ["rel", "name", "md5", "size", "mtime", "duration", "width",
                "height", "fps", "vcodec", "acodec", "bitrate",
                "cover", "gif", "line_count", "line_source",
                "status", "err", "created_at"]
        vals = [v.get(c) for c in cols]
        ph = ",".join("?" * len(cols))
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT OR REPLACE INTO videos({','.join(cols)}) VALUES({ph})", vals)
            return cur.lastrowid

    def update_video(self, vid: int, **kw):
        if not kw:
            return
        sets = ",".join(f"{k}=?" for k in kw)
        with self._conn() as conn:
            conn.execute(f"UPDATE videos SET {sets} WHERE id=?",
                         (*kw.values(), vid))

    def get_video(self, vid: int):
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
            return dict(r) if r else None

    def list_videos(self, q: str = "", limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM videos"
        args: List[Any] = []
        if q:
            sql += " WHERE name LIKE ?"
            args.append(f"%{q}%")
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def delete_video(self, vid: int) -> Dict[str, Any]:
        """删掉一个素材：台词 + FTS5 + 记录。返回被删掉的缓存相对路径（封面/动图/截图）"""
        v = self.get_video(vid)
        if not v:
            return {"ok": False, "assets": [], "video": None}
        assets: List[str] = []
        if v.get("cover"):
            assets.append(v["cover"])
        if v.get("gif"):
            assets.append(v["gif"])
        with self._conn() as conn:
            for r in conn.execute(
                    "SELECT shot FROM lines WHERE video_id=?", (vid,)):
                if r["shot"]:
                    assets.append(r["shot"])
            # lines_fts 是外部内容表，必须用 'delete' 命令同步，否则残留可搜到幽灵行
            try:
                conn.execute(
                    "INSERT INTO lines_fts(lines_fts, rowid, text) "
                    "SELECT 'delete', id, text FROM lines WHERE video_id=?", (vid,))
            except Exception:
                conn.execute(
                    "DELETE FROM lines_fts WHERE rowid IN "
                    "(SELECT id FROM lines WHERE video_id=?)", (vid,))
            conn.execute("DELETE FROM lines WHERE video_id=?", (vid,))
            conn.execute("DELETE FROM videos WHERE id=?", (vid,))
        return {"ok": True, "assets": assets, "video": v}

    # ---------------- 忽略名单（移除但不删文件） ----------------
    def is_ignored(self, md5: str) -> bool:
        if not md5:
            return False
        with self._conn() as conn:
            return conn.execute(
                "SELECT 1 FROM ignored WHERE md5=?", (md5,)).fetchone() is not None

    def add_ignore(self, md5: str, name: str = "", rel: str = ""):
        if not md5:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ignored(md5,name,rel,created_at) VALUES(?,?,?,?)",
                (md5, name, rel, time.time()))

    def remove_ignore(self, md5: str = "", all: bool = False) -> int:
        with self._conn() as conn:
            if all:
                cur = conn.execute("DELETE FROM ignored")
            else:
                cur = conn.execute("DELETE FROM ignored WHERE md5=?", (md5,))
            return cur.rowcount or 0

    def list_ignored(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM ignored ORDER BY created_at DESC")]

    def stats(self) -> Dict[str, Any]:
        with self._conn() as conn:
            v = conn.execute("SELECT COUNT(*) c FROM videos").fetchone()["c"]
            l = conn.execute("SELECT COUNT(*) c FROM lines").fetchone()["c"]
            d = conn.execute("SELECT SUM(duration) s FROM videos").fetchone()["s"]
            ig = conn.execute("SELECT COUNT(*) c FROM ignored").fetchone()["c"]
        return {"videos": v, "lines": l, "duration": d or 0, "ignored": ig}

    # ---------------- lines ----------------
    def replace_lines(self, vid: int, rows: List[Dict[str, Any]],
                      src: str = ""):
        """整部片的台词全量替换。

        `src` 是这批台词的来源（external/embedded/ocr/asr），存进 lines.src
        让 verify.check_line 能区分"本来就没有画面框"（外挂/内嵌）和
        "应该有框却没有"（OCR 却没给凭证）——这两种情况性质完全不同。

        `rows` 里每条可以带 `box`（方框凭证 dict），这里序列化成 JSON 存库。
        """
        with self._conn() as conn:
            # 顺序不能反：lines_fts 是外部内容表，必须先按旧行发 'delete'
            # 命令同步，再把 lines 删掉。以前是先删 lines 再删 fts，
            # 那时子查询已经查不到任何 id，fts 里的旧行全留了下来——
            # 重识别后旧台词仍然能被搜到（幽灵行）。
            self._fts_drop(conn, vid)
            conn.execute("DELETE FROM lines WHERE video_id=?", (vid,))
            for i, r in enumerate(rows):
                box = r.get("box")
                if isinstance(box, dict):
                    box = json.dumps(box, ensure_ascii=False,
                                     separators=(",", ":"))
                cur = conn.execute(
                    "INSERT INTO lines(video_id,seq,start,end,text,shot,box,src)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (vid, i, r["start"], r.get("end"), r["text"],
                     r.get("shot"), box, r.get("src") or src or None))
                conn.execute(
                    "INSERT INTO lines_fts(rowid,text) VALUES(?,?)", (cur.lastrowid, r["text"]))
            conn.execute(
                "UPDATE videos SET line_count=? WHERE id=?", (len(rows), vid))

    @staticmethod
    def _fts_drop(conn, vid: int):
        """把一个素材的台词从全文索引里摘掉。

        lines_fts 是 content='lines' 的外部内容表，直接 DELETE 会不同步，
        必须用 'delete' 命令；万一这条命令不可用再退回 DELETE。
        """
        try:
            conn.execute(
                "INSERT INTO lines_fts(lines_fts, rowid, text) "
                "SELECT 'delete', id, text FROM lines WHERE video_id=?", (vid,))
        except Exception:
            conn.execute(
                "DELETE FROM lines_fts WHERE rowid IN "
                "(SELECT id FROM lines WHERE video_id=?)", (vid,))

    def clear_lines(self, vid: int) -> Dict[str, Any]:
        """清空一个素材的台词，连同全文索引和截图文件清单一起返回。

        「重新识别台词」第一件事就是调它：旧台词先清干净，再按当前逻辑重跑，
        否则旧结果（含旧截图）会混在新结果里——比如上次识别了 100 条、
        这次逻辑改完只出 80 条，剩下 20 条旧台词会原样躺在库里。
        """
        with self._conn() as conn:
            shots = [r["shot"] for r in conn.execute(
                "SELECT shot FROM lines WHERE video_id=?", (vid,)) if r["shot"]]
            self._fts_drop(conn, vid)
            n = conn.execute(
                "DELETE FROM lines WHERE video_id=?", (vid,)).rowcount
            conn.execute(
                "UPDATE videos SET line_count=0, line_source='pending' WHERE id=?",
                (vid,))
        return {"removed": int(n or 0), "shots": shots}

    def set_shot(self, line_id: int, shot: str):
        with self._conn() as conn:
            conn.execute("UPDATE lines SET shot=? WHERE id=?", (shot, line_id))

    def update_line(self, line_id: int, text: str = None,
                    start: float = None, end: float = None,
                    src: str = None) -> Dict[str, Any]:
        """人工改一条台词：文字 / 起止时间 / 来源标记。传 None 的不动。

        方框凭证 `box` 刻意不开放在这里——那是"框在哪"，由 set_box 管；
        本方法是"说了什么、什么时候说的"，两件事分开，改一处不会
        顺带把另一处的凭证搅乱。

        全文索引要同步：lines_fts 是 content='lines' 的外部内容表，
        直接 UPDATE lines 不会反映到 fts，必须先按**旧文本**发 'delete'
        命令再插新文本。漏了这一步的后果是"改了字但搜不到 / 还搜得到旧字"。
        """
        sets: List[str] = []
        vals: List[Any] = []
        if text is not None:
            sets.append("text=?")
            vals.append(text)
        if start is not None:
            sets.append("start=?")
            vals.append(float(start))
        if end is not None:
            sets.append("end=?")
            vals.append(float(end))
        if src is not None:
            sets.append("src=?")
            vals.append(src or None)
        if not sets:
            return {"ok": False, "err": "没有要改的内容"}

        with self._conn() as conn:
            old = conn.execute("SELECT text FROM lines WHERE id=?",
                               (line_id,)).fetchone()
            if not old:
                return {"ok": False, "err": "找不到这条台词"}
            if text is not None:
                try:
                    conn.execute("INSERT INTO lines_fts(lines_fts, rowid, text) "
                                 "VALUES('delete',?,?)", (line_id, old["text"]))
                except Exception:
                    conn.execute("DELETE FROM lines_fts WHERE rowid=?", (line_id,))
            cur = conn.execute(
                f"UPDATE lines SET {','.join(sets)} WHERE id=?", vals + [line_id])
            if text is not None:
                conn.execute("INSERT INTO lines_fts(rowid, text) VALUES(?,?)",
                             (line_id, text))
            row = conn.execute("SELECT * FROM lines WHERE id=?",
                               (line_id,)).fetchone()
        return {"ok": cur.rowcount > 0,
                "line": dict(row) if row else None}

    def set_box(self, line_id: int, box: Optional[Dict[str, Any]]):
        """回填/改写某条台词的方框凭证。

        两条用得上的路径：
          · 方框复查页里人工确认后回填（老库那批语音转写台词本来没框）
          · 复核发现框画歪了，直接在页面上修正，不用重跑整部片的 OCR

        传 None 表示清空凭证。文本不同步改——凭证只描述"框在哪"，
        不负责改台词内容，那是 SRT 那条路的事。
        """
        s = None
        if isinstance(box, dict) and box:
            s = json.dumps(box, ensure_ascii=False, separators=(",", ":"))
        with self._conn() as conn:
            cur = conn.execute("UPDATE lines SET box=? WHERE id=?", (s, line_id))
        return cur.rowcount > 0

    def box_stats(self, vid: int) -> Dict[str, int]:
        """这个素材里有多少条带凭证 / 不带凭证（一次 SQL，别把全表拉出来）。"""
        with self._conn() as conn:
            r = conn.execute(
                "SELECT COUNT(*) t,"
                " SUM(CASE WHEN box IS NOT NULL AND box<>'' THEN 1 ELSE 0 END) b "
                "FROM lines WHERE video_id=?", (vid,)).fetchone()
        return {"total": r["t"] or 0, "boxed": r["b"] or 0}

    def line_box_counts(self) -> Dict[int, tuple]:
        """整库一次 GROUP BY：video_id → (台词数, 带框数)。

        方框复查首屏要列出所有片子的框统计。逐片调 box_stats 是 N 条 SQL，
        TV 库 80 部片实测 4~8 秒、还会随片子增加线性变慢；整库聚合一次
        扫 6 万行只要几十毫秒。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT video_id, COUNT(*) total,"
                " SUM(CASE WHEN box IS NOT NULL AND box<>'' THEN 1 ELSE 0 END) boxed "
                "FROM lines GROUP BY video_id").fetchall()
        return {int(r["video_id"]): (int(r["total"] or 0), int(r["boxed"] or 0))
                for r in rows}

    def lines_of(self, vid: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM lines WHERE video_id=? ORDER BY seq", (vid,))]
        for r in rows:
            b = r.get("box")
            if isinstance(b, str):
                try:
                    r["box"] = json.loads(b)
                except Exception:
                    r["box"] = None
        return rows

    def get_line(self, line_id: int):
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM lines WHERE id=?", (line_id,)).fetchone()
            return dict(r) if r else None

    # ---------------- search ----------------
    def search(self, q: str, limit: int = 200) -> List[Dict[str, Any]]:
        """中文优先：>=3 字走 FTS5 trigram，<3 字或 FTS 无结果时退回 LIKE。

        关键词会按简繁展开成多个变体再 OR 查询（core.hanzi）：库里存的是繁体
        字幕、用户打简体（或反过来、或繁简混写）都要能互相搜到。没有简繁异体
        的词不展开，避免白白多出查询分支。
        """
        q = (q or "").strip()
        if not q:
            return []
        vs = hanzi.expand(q) if hanzi.has_variant(q) else [q]
        rows: List[Dict[str, Any]] = []
        if len(q) >= 3:
            # 变体是字符级 1:1 替换，长度跟 q 一致，所以按 q 判长度即可
            expr = " OR ".join('"%s"' % v.replace('"', '""') for v in vs)
            try:
                with self._conn() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT l.*, v.name, v.rel, v.duration, v.cover, v.gif "
                        "FROM lines_fts f JOIN lines l ON l.id=f.rowid "
                        "JOIN videos v ON v.id=l.video_id "
                        "WHERE lines_fts MATCH ? ORDER BY l.video_id, l.start LIMIT ?",
                        (expr, limit))]
            except Exception:
                rows = []
        if not rows:
            where = " OR ".join(["l.text LIKE ?"] * len(vs))
            with self._conn() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT l.*, v.name, v.rel, v.duration, v.cover, v.gif "
                    "FROM lines l JOIN videos v ON v.id=l.video_id "
                    f"WHERE {where} ORDER BY l.video_id, l.start LIMIT ?",
                    (*[f"%{v}%" for v in vs], limit))]
        out = []
        for r in rows:
            r["start_str"] = fmt_ts(r["start"])
            r["end_str"] = fmt_ts(r.get("end") or r["start"])
            # 这行命中的是哪个简繁变体：前端拿它做高亮，否则搜简体时繁体结果
            # 会因为和关键词字面不同而高亮不上，看起来像没命中。
            if len(vs) > 1:
                txt = r.get("text") or ""
                for v in vs:
                    if v in txt:
                        r["mv"] = v
                        break
            # box 存的是 JSON 字符串，这里解成 dict 方便前端直接读几何字段
            b = r.get("box")
            if isinstance(b, str):
                try:
                    r["box"] = json.loads(b)
                except Exception:
                    r["box"] = None
            out.append(r)
        return out

    # ---------------- tasks ----------------
    def new_task(self, kind: str, root: str, total: int = 0) -> int:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO tasks(kind,root,status,total,done,message,created_at,updated_at)"
                " VALUES(?,?,'running',?,0,'',?,?)", (kind, root, total, now, now))
            return cur.lastrowid

    def tick_task(self, tid: int, done: int, message: str = "", total: int = None):
        with self._conn() as conn:
            if total is not None:
                conn.execute("UPDATE tasks SET done=?,message=?,total=?,updated_at=? WHERE id=?",
                             (done, message, total, time.time(), tid))
            else:
                conn.execute("UPDATE tasks SET done=?,message=?,updated_at=? WHERE id=?",
                             (done, message, time.time(), tid))

    def finish_task(self, tid: int, status: str = "done", message: str = ""):
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET status=?,message=?,updated_at=? WHERE id=?",
                         (status, message, time.time(), tid))

    def reap_stale_tasks(self) -> int:
        """把遗留的 running 任务标记成中断。

        服务进程被kill/断电时，tasks 表里的 running 是**假的**——线程早没了。
        不清的话页面会永远显示一个 0% 的进度卡片，看起来像卡住了。
        每次启动扫一遍即可。
        """
        with self._conn() as conn:
            n = conn.execute(
                "UPDATE tasks SET status='interrupted',"
                " message=COALESCE(NULLIF(message,''),'服务重启，任务被中断'),"
                " updated_at=? WHERE status='running'", (time.time(),)).rowcount
        return n or 0

    def get_task(self, tid: int):
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            return dict(r) if r else None

    def latest_task(self, root: str = None):
        with self._conn() as conn:
            if root:
                r = conn.execute(
                    "SELECT * FROM tasks WHERE root=? ORDER BY id DESC LIMIT 1", (root,)).fetchone()
            else:
                r = conn.execute(
                    "SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
            return dict(r) if r else None

    # ---------------- meta ----------------
    def set_meta(self, k: str, v: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, v))

    def get_meta(self, k: str, default: str = None):
        with self._conn() as conn:
            r = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
            return r["value"] if r else default
