"""SQLite 持久化：会话、任务、事件、主机档案与事实。

为什么需要它？
- 内存里的 Run 一重启就没了，用户想回看昨天的排查怎么办？
- Agent 第二次排查同一台机器时，应该记得「上次也是这个日志没轮转」——这叫长期记忆。

表结构（刻意保持简单，够用就好）：
    sessions(id, title, created_at)
    runs(id, session_id, input, status, started_at, finished_at, steps, tool_calls, tokens,
         text, report_json, error)
    events(run_id, seq, type, data_json)          -- 主键 (run_id, seq)
    host_profiles(host, data_json, updated_at)
    facts(id, host, key, value, run_id, created_at)

所有写操作都用短事务；读操作返回普通 dict，方便直接进 JSON 响应。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, title TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, session_id TEXT, input TEXT NOT NULL, status TEXT NOT NULL,
    started_at REAL NOT NULL, finished_at REAL, steps INTEGER, tool_calls INTEGER,
    tokens INTEGER, text TEXT, report_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS host_profiles (
    host TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    run_id TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_host ON facts(host);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""


class Store:
    def __init__(self, path: str | Path = "data/server_agent.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- 会话 / 任务 ----------
    def ensure_session(self, title: str = "默认会话") -> str:
        row = self._conn.execute("SELECT id FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            return row["id"]
        sid = f"session_{uuid.uuid4().hex[:8]}"
        self._conn.execute("INSERT INTO sessions(id, title, created_at) VALUES (?, ?, ?)",
                           (sid, title, time.time()))
        self._conn.commit()
        return sid

    def save_run_start(self, run_id: str, user_input: str, *, session_id: str | None = None) -> None:
        sid = session_id or self.ensure_session()
        self._conn.execute(
            "INSERT OR REPLACE INTO runs(id, session_id, input, status, started_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, sid, user_input, "running", time.time()))
        self._conn.commit()

    def append_event(self, run_id: str, seq: int, type_: str, data: dict) -> None:
        self._conn.execute("INSERT OR REPLACE INTO events(run_id, seq, type, data_json) VALUES (?, ?, ?, ?)",
                           (run_id, seq, type_, json.dumps(data, ensure_ascii=False)))
        self._conn.commit()

    def save_run_end(self, run_id: str, *, status: str, steps: int, tool_calls: int,
                     tokens: int, text: str = "", report: dict | None = None,
                     error: str | None = None) -> None:
        self._conn.execute(
            """UPDATE runs SET status = ?, finished_at = ?, steps = ?, tool_calls = ?, tokens = ?,
               text = ?, report_json = ?, error = ? WHERE id = ?""",
            (status, time.time(), steps, tool_calls, tokens, text,
             json.dumps(report, ensure_ascii=False) if report else None, error, run_id))
        self._conn.commit()

    def list_runs(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            """SELECT r.*, (SELECT COUNT(*) FROM events e WHERE e.run_id = r.id) AS event_count
               FROM runs r ORDER BY started_at DESC LIMIT ?""", (limit,)).fetchall()
        return [self._run_row(r) for r in rows]

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def get_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        rows = self._conn.execute(
            "SELECT seq, type, data_json FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after_seq)).fetchall()
        return [{"seq": r["seq"], "type": r["type"], "data": json.loads(r["data_json"])} for r in rows]

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["report"] = json.loads(d.pop("report_json")) if d.get("report_json") else None
        return d

    # ---------- 主机档案与事实 ----------
    def update_host_profile(self, host: str, data: dict) -> None:
        existing = self.get_host_profile(host) or {}
        existing.update({k: v for k, v in data.items() if v is not None})
        self._conn.execute("INSERT OR REPLACE INTO host_profiles(host, data_json, updated_at) VALUES (?, ?, ?)",
                           (host, json.dumps(existing, ensure_ascii=False), time.time()))
        self._conn.commit()

    def get_host_profile(self, host: str) -> dict | None:
        row = self._conn.execute("SELECT data_json FROM host_profiles WHERE host = ?", (host,)).fetchone()
        return json.loads(row["data_json"]) if row else None

    def remember_fact(self, host: str, key: str, value: str, run_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO facts(host, key, value, run_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (host, key, value, run_id, time.time()))
        self._conn.commit()

    def recall_facts(self, host: str, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT key, value, run_id, created_at FROM facts WHERE host = ? ORDER BY created_at DESC LIMIT ?",
            (host, limit)).fetchall()
        return [dict(r) for r in rows]

    def recent_incidents(self, host: str | None = None, limit: int = 5) -> list[dict]:
        """最近几次排查的结论摘要，用于给下一次排查提供上下文。"""
        sql = """SELECT r.id, r.input, r.started_at, r.report_json, r.status FROM runs r
                 WHERE r.report_json IS NOT NULL"""
        params: list[Any] = []
        if host:
            sql += " AND (r.input LIKE ? OR r.report_json LIKE ?)"
            params += [f"%{host}%", f"%{host}%"]
        sql += " ORDER BY r.started_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for row in self._conn.execute(sql, params).fetchall():
            report = json.loads(row["report_json"])
            out.append({"run_id": row["id"], "input": row["input"], "started_at": row["started_at"],
                        "status": row["status"], "summary": report.get("summary"),
                        "root_cause": report.get("root_cause"),
                        "confidence": report.get("confidence")})
        return out


_default: Store | None = None


def get_store() -> Store:
    """进程内默认 Store（路径来自 SA_DB_PATH）。"""
    global _default
    if _default is None:
        from server_agent.config import get_settings

        _default = Store(get_settings().db_path)
    return _default


def reset_store(store: Store | None = None) -> None:
    """测试用：替换/清空默认 Store。"""
    global _default
    _default = store
