"""SQLite session/message persistence helpers.

本模块为 Agent 提供轻量级的会话持久化能力：
- sessions 表：保存会话元信息
- messages 表：保存具体的历史消息（按顺序存储）

这样不仅可以在进程退出后恢复上下文，还能为后续的 Context Compression
与跨 Session 记忆做基础。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


class SessionStore:
    """持久化会话消息到 SQLite。"""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(base_dir, "session.db")
        self.db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """创建必要的数据表。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
            """
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _now_iso() -> str:
        """返回 UTC 时间戳字符串。"""
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def create_session(self) -> str:
        """创建一个新会话并返回 session_id。"""
        session_id = uuid.uuid4().hex
        now = self._now_iso()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
            (session_id, now, now),
        )
        conn.commit()
        conn.close()
        return session_id

    def list_sessions(self) -> list[dict[str, str | int]]:
        """返回最近的会话列表，并附带消息数量统计。

        返回结构示例：
        {
            "id": "...",
            "created_at": "...",
            "updated_at": "...",
            "message_count": 12,
        }
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.created_at,
                s.updated_at,
                COUNT(m.id) AS message_count
            FROM sessions AS s
            LEFT JOIN messages AS m
                ON m.session_id = s.id
            GROUP BY s.id, s.created_at, s.updated_at
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_latest_session_id(self) -> str | None:
        """返回最近更新的会话 ID。"""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def save_history(self, session_id: str, history: list[dict[str, Any]]) -> None:
        """保存完整 history 到指定 session。

        这里会把整个历史重新写一遍，确保数据库与当前内存状态一致。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            now = self._now_iso()
            for idx, msg in enumerate(history, start=1):
                payload = json.dumps(msg, ensure_ascii=False, default=str)
                role = str(msg.get("role", "unknown"))
                content = msg.get("content")
                tool_call_id = msg.get("tool_call_id")
                conn.execute(
                    """
                    INSERT INTO messages (
                        session_id,
                        sequence,
                        role,
                        content,
                        tool_call_id,
                        payload,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        idx,
                        role,
                        content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str),
                        tool_call_id,
                        payload,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def load_history(self, session_id: str) -> list[dict[str, Any]]:
        """从 SQLite 读取历史消息并恢复为 OpenAI 风格消息列表。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT payload
            FROM messages
            WHERE session_id = ?
            ORDER BY sequence ASC, id ASC
            """,
            (session_id,),
        ).fetchall()
        conn.close()

        history: list[dict[str, Any]] = []
        for row in rows:
            history.append(json.loads(row["payload"]))
        return history
