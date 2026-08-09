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
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_sequence
            ON messages(session_id, sequence)
            """
        )
        # 折叠原文归档表(决策 C 2026-08-07):
        # L1 折叠掉的超长 tool 结果原文,供未来的 agent 工具按需取回。
        # 键 = (session_id, tool_call_id):tool_call_id 在压缩重写后保持稳定。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_result_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                folded_at TEXT NOT NULL,
                original_len INTEGER NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(session_id, tool_call_id)
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

        逐条 upsert（以 `(session_id, sequence)` 为稳定键）：
        - 已有的行只更新 role/content/tool_call_id/payload，
          不覆盖 created_at —— 保留每条消息首次写入的时间；
        - 超出新历史长度的旧行删除 —— 这样既能处理历史增长，
          也能处理未来 context compression 导致的历史缩水。

        整个写入在单个 `BEGIN IMMEDIATE` 事务内完成，配合
        `busy_timeout`，避免多进程并行写入时抛 "database is locked"
        或出现交叉覆盖。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("BEGIN IMMEDIATE")
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
                    ON CONFLICT(session_id, sequence) DO UPDATE SET
                        role = excluded.role,
                        content = excluded.content,
                        tool_call_id = excluded.tool_call_id,
                        payload = excluded.payload
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
                "DELETE FROM messages WHERE session_id = ? AND sequence > ?",
                (session_id, len(history)),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
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

    def archive_tool_results(self, session_id: str, items: list[dict[str, Any]]) -> None:
        """归档被 L1 折叠的 tool 结果原文(决策 C)。

        键 = (session_id, tool_call_id):同一键重复写入覆盖旧值(幂等,
        多次压缩不会产生重复归档)。单事务 + busy_timeout,与 save_history 一致。
        """
        if not items:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("BEGIN IMMEDIATE")
            now = self._now_iso()
            for item in items:
                tool_call_id = item.get("tool_call_id")
                content = item.get("content") or ""
                conn.execute(
                    """
                    INSERT INTO tool_result_archive (
                        session_id, tool_call_id, folded_at, original_len, content
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, tool_call_id) DO UPDATE SET
                        folded_at = excluded.folded_at,
                        original_len = excluded.original_len,
                        content = excluded.content
                    """,
                    (session_id, tool_call_id, now, len(content), content),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_archived_tool_result(self, session_id: str, tool_call_id: str) -> str | None:
        """取回被折叠前的完整原文;不存在返回 None。"""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT content FROM tool_result_archive
            WHERE session_id = ? AND tool_call_id = ?
            """,
            (session_id, tool_call_id),
        ).fetchone()
        conn.close()
        return row[0] if row else None
