# -*- coding: utf-8 -*-
"""拾帧 FrameSeek - 项目级素材库登记处

每个素材文件夹下有自己的 .frameseek/index.db（随素材走），
而"我登记了哪些素材库"这件事，统一记在项目目录下的 hub.db。
"""
import os
import sqlite3
import time
from typing import List, Dict, Any

from core import config as C
from core import indexer as IX

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB_DB = os.path.join(BASE, "hub.db")
CFG_FILE = os.path.join(BASE, "frameseek.json")

# 内置只读库：AI 生成产物目录（2026-10-15 起生成物不再落素材库）。
# 特殊之处——不登记进 hub.db，而是在 list_all() 里凭空合成一条 builtin 记录：
# 素材页筛选里能勾选、能预览，但不进「素材库」卡片列表、不能编辑/删除/改名。
BUILTIN_AI = {"name": "AI 生成", "note": "内置 · 生成产物，只读"}

# 测试/自测用的库不进用户可见列表（回归脚本登记的 _testlib、名字带「测试」的库）。
# 只影响"列出来"：按 id / 路径直接访问（扫描、激活、删除）照样能用，
# 这样回归脚本不受影响，用户界面上也不会看到一堆测试库。
TEST_DIR_HINTS = ("_testlib",)
TEST_NAME_PREFIX = ("测试", "test")

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    enabled INTEGER DEFAULT 1,
    note TEXT DEFAULT '',
    created_at REAL,
    last_scan_at REAL
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Hub:
    def __init__(self, db_path: str = HUB_DB):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ---------------- libraries ----------------
    def add(self, path: str, name: str = "", note: str = "") -> Dict[str, Any]:
        path = os.path.abspath(path.strip().strip('"'))
        if not os.path.isdir(path):
            return {"ok": False, "err": f"目录不存在：{path}"}
        raw = (name or "").strip()
        disp = raw or os.path.basename(path.rstrip("\\/")) or path
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM libraries WHERE path=?", (path,)).fetchone()
            if row:
                if raw:
                    conn.execute(
                        "UPDATE libraries SET enabled=1,note=?,name=? WHERE id=?",
                        (note, raw, row["id"]))
                else:
                    conn.execute(
                        "UPDATE libraries SET enabled=1,note=? WHERE id=?",
                        (note, row["id"]))
                lid = row["id"]
                disp = raw or row["name"]
            else:
                cur = conn.execute(
                    "INSERT INTO libraries(name,path,enabled,note,created_at)"
                    " VALUES(?,?,1,?,?)", (disp, path, note, time.time()))
                lid = cur.lastrowid
        # 写事务外面再动 settings，避免自己和自己抢写锁
        if not self.get_setting("active_id"):
            self.set_active(lid)
        return {"ok": True, "id": lid, "path": path, "name": disp}

    def remove(self, lid: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM libraries WHERE id=?", (lid,))
        if self.get_setting("active_id") == str(lid):
            self.set_setting("active_id", "")

    def rename(self, lid: int, name: str):
        with self._conn() as conn:
            conn.execute("UPDATE libraries SET name=? WHERE id=?", (name, lid))

    def get(self, lid: int):
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM libraries WHERE id=?", (lid,)).fetchone()
            return dict(r) if r else None

    def is_test_lib(self, row: Dict[str, Any]) -> bool:
        """是不是测试/自测库（不进用户可见列表的那种）。"""
        name = (row.get("name") or "").strip().lower()
        if name.startswith(TEST_NAME_PREFIX):
            return True
        p = os.path.normcase(os.path.abspath(row.get("path") or ""))
        return any(os.sep + h + os.sep in p + os.sep for h in TEST_DIR_HINTS)

    def list(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM libraries ORDER BY id")]
        out = []
        for r in rows:
            if self.is_test_lib(r):
                continue
            r["exists"] = os.path.isdir(r["path"])
            r["builtin"] = False
            st = self.stats_of(r["path"])
            r.update(st)
            out.append(r)
        return out

    def list_all(self, with_builtin: bool = True
                 ) -> List[Dict[str, Any]]:
        """所有库 = 用户登记的 + 内置 AI 库（凭空合成，不落 hub.db）。"""
        rows = self.list()
        if with_builtin:
            rows = rows + self._builtin_ai_rows()
        return rows

    def _builtin_ai_rows(self) -> List[Dict[str, Any]]:
        from core import gen as gen_mod
        path = gen_mod.output_dir()
        st = self.stats_of(path)
        return [{
            "id": 0, "name": BUILTIN_AI["name"], "path": path,
            "enabled": 1, "note": BUILTIN_AI["note"],
            "created_at": 0, "last_scan_at": 0,
            "exists": os.path.isdir(path), "builtin": True, **st,
        }]

    def builtin_by_path(self, path: str) -> Dict[str, Any]:
        """给定的 path 是不是内置 AI 库；是则返回记录，否则 None。"""
        p = os.path.abspath(path or "")
        for r in self._builtin_ai_rows():
            if os.path.normcase(r["path"]) == os.path.normcase(p):
                return r
        return None

    def stats_of(self, path: str) -> Dict[str, Any]:
        """读该素材库自己的 index.db；没索引过就返回 0"""
        empty = {"videos": 0, "lines": 0, "duration": 0, "ignored": 0, "indexed": False}
        db = os.path.join(path, C.CACHE_DIR, C.DB_NAME)
        if not os.path.exists(db):
            return empty
        try:
            s = IX.open_store(path).stats()
            s["indexed"] = True
            return s
        except Exception:
            return empty

    def touch_scan(self, path: str):
        with self._conn() as conn:
            conn.execute("UPDATE libraries SET last_scan_at=? WHERE path=?",
                         (time.time(), os.path.abspath(path)))

    # ---------------- settings ----------------
    def set_setting(self, k: str, v: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))

    def get_setting(self, k: str, default: str = "") -> str:
        with self._conn() as conn:
            r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            return r["value"] if r else default

    def set_active(self, lid: int):
        row = self.get(lid)
        if row and self.is_test_lib(row):
            return          # 测试库不当激活库（用户不该一开机就落在测试库上）
        self.set_setting("active_id", str(lid))

    def get_active(self):
        v = self.get_setting("active_id", "")
        if not v:
            return None
        return self.get(int(v))

    def active_path(self) -> str:
        a = self.get_active()
        if a:
            return a["path"]
        rows = self.list()
        return rows[0]["path"] if rows else ""

    # ---------------- 跨库搜索 ----------------
    def search_all(self, q: str, limit: int = 200, only: List[str] = None
                   ) -> List[Dict[str, Any]]:
        out = []
        per = max(20, limit // max(1, len(self.list())))
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM libraries WHERE enabled=1 ORDER BY id")]
        for r in rows:
            if only and r["path"] not in only:
                continue
            if self.is_test_lib(r):
                continue        # 测试库的台词不出现在用户的跨库搜索里
            if not os.path.isdir(r["path"]):
                continue
            try:
                st = IX.open_store(r["path"])
                for x in st.search(q, per):
                    x["root"] = r["path"]
                    x["lib"] = r["name"]
                    out.append(x)
            except Exception:
                continue
        out.sort(key=lambda x: (x.get("lib", ""), x["video_id"], x["start"]))
        return out[:limit]

    # ---------------- 旧配置迁移 ----------------
    def migrate_json(self):
        """把早期 frameseek.json 里的 roots 搬进 hub.db（一次性）"""
        if not os.path.exists(CFG_FILE):
            return
        try:
            import json
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            for p in d.get("roots", []):
                self.add(p)
            for k in ("use_asr", "with_gif"):
                if k in (d.get("settings") or {}):
                    self.set_setting(k, "1" if d["settings"][k] else "0")
            act = d.get("active")
            if act:
                with self._conn() as conn:
                    r = conn.execute(
                        "SELECT id FROM libraries WHERE path=?",
                        (os.path.abspath(act),)).fetchone()
                if r:
                    self.set_active(r["id"])
            os.rename(CFG_FILE, CFG_FILE + ".bak")
        except Exception:
            pass


HUB = Hub()
