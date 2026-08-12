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
import re
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
        # 全文搜索索引(Phase 2.3b 2026-08-10,见 docs/fts-search.md):
        # 双表方案——FTS5 的 tokenizer 是表级的,无法单表双分词。
        # fts_en: porter 词干化,服务英文/数字(词级 + 前缀查询)
        # fts_zh: trigram,服务中文(任意 >=3 字符连续子串)
        # UNINDEXED 列只存储不参与分词;原文冗余进索引表,
        # 不依赖 messages 行 id 对齐(压缩重写 sequence 后依然安全)。
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_en USING fts5(
                source UNINDEXED, session_id UNINDEXED, seq UNINDEXED, content,
                tokenize = 'porter'
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_zh USING fts5(
                source UNINDEXED, session_id UNINDEXED, seq UNINDEXED, content,
                tokenize = 'trigram'
            )
            """
        )
        # 跨 Session 记忆表(Phase 2.4 2026-08-11,见 docs/cross-session-memory.md):
        # 对话结束由 LLM 抽取事实存入;后续会话按需检索注入 system prompt。
        # replace_id 覆盖:保留原 id 与 hits(使用统计),更新事实与置信度。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                fact TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                confidence REAL NOT NULL DEFAULT 0.8,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_category
            ON memories(category)
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
        """返回最近更新的会话 ID。

        排序加 rowid 次级键:created_at/updated_at 是秒级精度,
        同秒创建的会话排序不稳定(测试抓到),rowid 保证取后插入的。
        """
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT id FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT 1"
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

    # ---------------------------------------------------------------
    # Phase 2.3b: 全文搜索(FTS5 双表)
    # ---------------------------------------------------------------
    def rebuild_search_index(self) -> None:
        """全量重建 FTS 索引。

        索引范围(见 docs/fts-search.md §2):
        - messages.content(折叠后的 tool 结果;排除 system 模板)
        - tool_result_archive.content(L1 折叠掉的完整原文,决策 C 兑现)
        数据量小(几千行)毫秒级;搜索前惰性调用,保证索引与库一致。
        增量同步留作数据量大的演进方向。
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM fts_en")
            conn.execute("DELETE FROM fts_zh")

            rows = conn.execute(
                """
                SELECT session_id, sequence, content FROM messages
                WHERE role != 'system' AND content IS NOT NULL AND content != ''
                """
            ).fetchall()
            for session_id, seq, content in rows:
                conn.execute(
                    "INSERT INTO fts_en (source, session_id, seq, content) VALUES ('message', ?, ?, ?)",
                    (session_id, seq, content),
                )
                conn.execute(
                    "INSERT INTO fts_zh (source, session_id, seq, content) VALUES ('message', ?, ?, ?)",
                    (session_id, seq, content),
                )

            arows = conn.execute(
                "SELECT session_id, id, content FROM tool_result_archive"
            ).fetchall()
            for session_id, aid, content in arows:
                conn.execute(
                    "INSERT INTO fts_en (source, session_id, seq, content) VALUES ('archive', ?, ?, ?)",
                    (session_id, aid, content),
                )
                conn.execute(
                    "INSERT INTO fts_zh (source, session_id, seq, content) VALUES ('archive', ?, ?, ?)",
                    (session_id, aid, content),
                )

            # memories(Phase 2.4):事实条目作为第三索引源,
            # 注入时用 source='memory' 过滤检索。
            mrows = conn.execute(
                "SELECT session_id, id, fact FROM memories"
            ).fetchall()
            for session_id, mid, fact in mrows:
                conn.execute(
                    "INSERT INTO fts_en (source, session_id, seq, content) VALUES ('memory', ?, ?, ?)",
                    (session_id, mid, fact),
                )
                conn.execute(
                    "INSERT INTO fts_zh (source, session_id, seq, content) VALUES ('memory', ?, ?, ?)",
                    (session_id, mid, fact),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def search(
        self, query: str, source: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """全文搜索(Phase 2.3b)。

        路由规则(实测修正 2026-08-10,见 docs/fts-search.md §3):
        - 按**连续同语言片段**拆分查询(混合查询如 "L1 压缩" 拆成
          "L1" + "压缩" 分别查):
          * 英文/数字片段 → fts_en(porter,词级 + 前缀 'compre*')
          * 中文 >=3 字符片段 → fts_zh(trigram)
          * 中文 2 字符片段 → LIKE 退化(trigram 无法匹配 2 字 token)
        - 为什么不能整体查:trigram 对 "L1 压缩" 切成 "L1 " / "1 压" /
          " 压缩"(含空格的窗口),与文本 token 集不一致,必然不命中;
          porter 表又要求中英片段连续出现。按片段拆分是正确解。
        查询转义:默认包成短语;用户显式以 * 结尾的片段保留前缀语法。
        """
        if not query or not query.strip():
            return []
        self.rebuild_search_index()

        zh_parts = [p for p in re.findall(r"[\u4e00-\u9fff]{2,}", query)]
        en_parts = [p for p in re.findall(r"[A-Za-z0-9_]{2,}", query)]
        if not zh_parts and not en_parts:
            en_parts = [query]  # 兜底:无常规片段时整体当英文查
        # 前缀符号 * 不属于 [A-Za-z0-9_],会被正则吃掉;
        # 原始查询以 * 结尾时把它还给最后一个英文片段(前缀查询语义)
        if query.rstrip().endswith("*") and en_parts:
            en_parts[-1] = en_parts[-1] + "*"

        conn = sqlite3.connect(self.db_path)
        try:
            hits: list[dict[str, Any]] = []
            for part in en_parts:
                hits += self._fts_query(conn, "fts_en", part, source, limit)
            for part in zh_parts:
                if len(part) >= 3:
                    hits += self._fts_query(conn, "fts_zh", part, source, limit)
                else:
                    hits += self._like_query(conn, part, source, limit)

            # 并集去重(同源同序只留一条),按 bm25 升序(越小越相关)
            seen: set[tuple[str, str, int]] = set()
            uniq: list[dict[str, Any]] = []
            for h in sorted(hits, key=lambda h: h["score"]):
                key = (h["source"], h["session_id"], h["seq"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(h)
            return uniq[:limit]
        finally:
            conn.close()

    def _fts_query(
        self, conn: sqlite3.Connection, table: str,
        part: str, source: str | None, limit: int,
    ) -> list[dict[str, Any]]:
        """单片段 FTS5 查询(短语化;显式 * 结尾保留前缀语法)。"""
        if part.rstrip().endswith("*"):
            match_query = part.rstrip()
        else:
            match_query = '"' + part.replace('"', '""') + '"'
        source_filter = " AND source = ?" if source else ""
        args: list = [match_query]
        if source:
            args.append(source)
        args.append(limit)
        rows = conn.execute(
            f"""
            SELECT source, session_id, seq,
                   snippet({table}, 3, '[', ']', '…', 20) AS snip,
                   bm25({table}) AS score
            FROM {table} WHERE {table} MATCH ?{source_filter}
            ORDER BY score LIMIT ?
            """,
            args,
        ).fetchall()
        return [
            {"source": s_, "session_id": sid, "seq": seq,
             "snippet": snip or "", "score": score}
            for s_, sid, seq, snip, score in rows
        ]

    def _like_query(
        self, conn: sqlite3.Connection, part: str,
        source: str | None, limit: int,
    ) -> list[dict[str, Any]]:
        """中文 2 字片段:LIKE 退化(转义 % _ \)。"""
        esc = part.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{esc}%"
        rows = conn.execute(
            """
            SELECT source, session_id, seq, content, 0 AS score FROM (
                SELECT 'message' AS source, session_id, sequence AS seq, content
                FROM messages WHERE role != 'system' AND content IS NOT NULL
                UNION ALL
                SELECT 'archive' AS source, session_id, id AS seq, content
                FROM tool_result_archive
                UNION ALL
                SELECT 'memory' AS source, session_id, id AS seq, fact AS content
                FROM memories
            ) WHERE content LIKE ? ESCAPE '\\'
              AND (? IS NULL OR source = ?)
            ORDER BY length(content) LIMIT ?
            """,
            (pattern, source, source, limit),
        ).fetchall()
        return [
            self._make_hit(s_, sid, seq, content, part, 0.0)
            for s_, sid, seq, content, score in rows
        ]

    @staticmethod
    def _make_hit(
        source_: str, session_id: str, seq: int,
        content: str, query: str, score: float,
    ) -> dict[str, Any]:
        """LIKE 退化的结果构造:手动截取命中词附近的上下文作 snippet。"""
        idx = content.find(query)
        start = max(0, idx - 40)
        end = min(len(content), idx + len(query) + 40)
        snippet = content[start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(content):
            snippet = snippet + "…"
        return {"source": source_, "session_id": session_id, "seq": seq,
                "snippet": snippet, "score": score}

    # ---------------------------------------------------------------
    # Phase 2.4: 跨 Session 记忆(memories 表 CRUD)
    # ---------------------------------------------------------------
    def add_memory(
        self, session_id: str, fact: str, category: str = "general",
        confidence: float = 0.8, replace_id: int | None = None,
    ) -> int:
        """写入一条记忆。

        replace_id 非空时覆盖旧条目(保留 id 与 hits,更新事实与置信度),
        返回记忆 id;否则新增并返回新 id。
        """
        now = self._now_iso()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            if replace_id is not None:
                conn.execute(
                    """
                    UPDATE memories SET fact = ?, category = ?, confidence = ?,
                        session_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (fact, category, confidence, session_id, now, replace_id),
                )
                conn.commit()  # UPDATE 分支也要提交(否则连接关闭回滚)
                return replace_id
            cur = conn.execute(
                """
                INSERT INTO memories (session_id, fact, category, confidence,
                                      created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, fact, category, confidence, now, now),
            )
            conn.commit()
            return cur.lastrowid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_memories(self) -> list[dict[str, Any]]:
        """列出全部记忆(按创建时间倒序)。"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """
            SELECT id, session_id, fact, category, confidence,
                   created_at, updated_at, hits
            FROM memories ORDER BY id DESC
            """
        ).fetchall()
        conn.close()
        return [
            {"id": r[0], "session_id": r[1], "fact": r[2], "category": r[3],
             "confidence": r[4], "created_at": r[5], "updated_at": r[6],
             "hits": r[7]}
            for r in rows
        ]

    def delete_memory(self, memory_id: int) -> bool:
        """删除一条记忆;不存在返回 False。"""
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def increment_memory_hits(self, memory_ids: list[int]) -> None:
        """注入命中计数(使用统计,供 /memories 展示)。"""
        if not memory_ids:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.executemany(
                "UPDATE memories SET hits = hits + 1 WHERE id = ?",
                [(i,) for i in memory_ids],
            )
            conn.commit()
        finally:
            conn.close()
