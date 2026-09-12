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


# 缺失 tool 回复时补入的占位内容(与"用户打断"占位区分,便于事后排查)
MISSING_TOOL_REPLY = "[工具结果缺失:该次调用未执行或被中断]"


def repair_tool_pairing(history: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """修复 history 中 assistant.tool_calls 与 tool 消息的配对关系。

    ============ 为什么需要这个函数 ============
    OpenAI/DeepSeek 的 Chat Completions API 有一条硬性约束:

        assistant 消息里声明了 N 个 tool_calls,
        其**紧随其后**必须有 N 条 role="tool" 且 tool_call_id 一一对应的消息。

    违反任一条都会被拒:
      - 少回复  → 400 "An assistant message with 'tool_calls' must be
                       followed by tool messages responding to each
                       'tool_call_id'. (insufficient tool messages ...)"
      - 多回复/位置错 → 400 "Messages with role 'tool' must be a response
                             to a preceding message with 'tool_calls'"

    而工具循环存在若干"提前跳出"的出口(Ctrl+C 中断、/p 打断后提示词为空、
    handler 抛非 InterruptSignal 异常等),这些出口未必来得及给同一批里
    其余 tool_call 补上占位回复。一旦残缺的历史被落库,后续每次 resume
    都会稳定复现 400,属于"一次损坏,永久失效"。

    所以这里做**双向**修复:
      1. 补:assistant.tool_calls 里缺 tool 回复的 → 补占位消息;
      2. 删:没有归属 assistant.tool_calls 的孤儿 tool 消息 → 丢弃
            (孤儿同样会触发 400);
      3. 顺带修正重复回复(同一 tool_call_id 出现多次时只保留第一条)。

    ============ 设计取舍 ============
    - 纯函数、不碰数据库、不改输入(返回新列表),便于单测与复用;
    - 幂等:对已合法的历史调用返回原顺序、零改动;
    - 只在"最小必要处"改动:补/删都发生在违规点,不动其他消息。

    返回 (修复后的历史, 统计字典)。统计字典字段:
      backfilled / dropped_orphans / dropped_duplicates / total
    """
    stats = {"backfilled": 0, "dropped_orphans": 0, "dropped_duplicates": 0, "total": 0}
    if not history:
        return history, stats

    # ---------- 第一遍:按顺序重建,处理"补"与"去重" ----------
    out: list[dict[str, Any]] = []
    i = 0
    n = len(history)
    while i < n:
        msg = history[i]
        role = msg.get("role")

        # 非 assistant 的 tool 消息:先原样带过,第二遍再判孤儿
        if role != "assistant":
            out.append(msg)
            i += 1
            continue

        out.append(msg)
        declared = [tc.get("id") for tc in (msg.get("tool_calls") or [])]
        if not declared:
            i += 1
            continue

        # 紧随其后的连续 tool 消息块(收集同时去重:每个 id 只留第一条)
        i += 1
        seen: set[str] = set()
        while i < n and history[i].get("role") == "tool":
            tid = history[i].get("tool_call_id")
            if tid in seen:
                stats["dropped_duplicates"] += 1   # 同一 id 重复回复
            else:
                seen.add(tid)
                out.append(history[i])
            i += 1

        # 声明了但没回复的 → 补占位(保持声明顺序,插在该批次末尾)
        for tid in declared:
            if tid not in seen:
                out.append({
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": MISSING_TOOL_REPLY,
                })
                stats["backfilled"] += 1

    # ---------- 第二遍:清理孤儿 tool 消息 ----------
    owned: set[str] = set()
    for msg in out:
        if msg.get("role") == "assistant":
            for tc in (msg.get("tool_calls") or []):
                if tc.get("id"):
                    owned.add(tc["id"])

    cleaned: list[dict[str, Any]] = []
    for msg in out:
        if msg.get("role") == "tool" and msg.get("tool_call_id") not in owned:
            stats["dropped_orphans"] += 1
            continue
        cleaned.append(msg)

    stats["total"] = (
        stats["backfilled"] + stats["dropped_orphans"] + stats["dropped_duplicates"]
    )
    return cleaned, stats


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
                updated_at TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 旧库迁移:早期 sessions 表无 usage 列,缺失时补列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        for col, ddl in [
            ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
            # 会话占用锁(2026-08-21 多进程防双开)
            ("locked_by", "TEXT"),
            ("locked_at", "TEXT"),
        ]:
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {col} {ddl}"
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
        # 教训表(Phase 3 Step 3):错误学习——规则(do/don't),与记忆(事实)分库
        # status: pending(未验证)/ verified(用户确认或 hits>=2 自动转正)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                lesson TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_lessons_status
            ON lessons(status)
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
            """INSERT INTO sessions (id, created_at, updated_at)
               VALUES (?, ?, ?)""",
            (session_id, now, now),
        )
        conn.commit()
        conn.close()
        return session_id

    def add_usage(self, session_id: str, prompt: int, completion: int, cached: int) -> None:
        """累计 token 用量到会话(输入/输出/缓存命中,增量累加)。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """UPDATE sessions SET
                   prompt_tokens = prompt_tokens + ?,
                   completion_tokens = completion_tokens + ?,
                   cached_tokens = cached_tokens + ?,
                   updated_at = ?
                   WHERE id = ?""",
                (prompt, completion, cached, self._now_iso(), session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_session_usage(self, session_id: str) -> dict[str, int]:
        """读取会话累计用量;会话不存在返回全 0。"""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """SELECT prompt_tokens, completion_tokens, cached_tokens
                   FROM sessions WHERE id = ?""",
                (session_id,),
            ).fetchone()
            if row is None:
                return {"prompt": 0, "completion": 0, "cached": 0}
            return {"prompt": row[0], "completion": row[1], "cached": row[2]}
        finally:
            conn.close()

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
        """从 SQLite 读取历史消息并恢复为 OpenAI 风格消息列表。

        注意:本方法是**忠实读取**——原样返回落库的内容,不做任何改写。
        持久层只负责存取,不负责校验业务约束;tool 配对的自愈由 Agent
        在 resume 时显式调用 repair_tool_pairing 完成(见 core.resume_session)。
        这样职责清晰,也保证 `save_history → load_history` 可无损往返。
        """
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

            # lessons(Phase 3 Step 3):教训规则作为第四索引源,
            # 注入时用 source='lesson' 过滤检索。
            lrows = conn.execute(
                "SELECT session_id, id, lesson FROM lessons"
            ).fetchall()
            for session_id, lid, lesson in lrows:
                conn.execute(
                    "INSERT INTO fts_en (source, session_id, seq, content) VALUES ('lesson', ?, ?, ?)",
                    (session_id, lid, lesson),
                )
                conn.execute(
                    "INSERT INTO fts_zh (source, session_id, seq, content) VALUES ('lesson', ?, ?, ?)",
                    (session_id, lid, lesson),
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
                UNION ALL
                SELECT 'lesson' AS source, session_id, id AS seq, lesson AS content
                FROM lessons
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

    def delete_session(self, session_id: str) -> bool:
        """删除一个会话(连带消息、归档、该会话产生的记忆)。

        事务内删除四张表,并重建全文索引(数据量小,全量重建简单可靠)。
        返回 False 表示会话不存在。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute(
                "DELETE FROM tool_result_archive WHERE session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM memories WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM lessons WHERE session_id = ?", (session_id,))
            conn.commit()
            if cur.rowcount:
                # 同步全文索引(被删内容不再可搜索)
                self.rebuild_search_index()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise
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

    # ---------------------------------------------------------------
    # Phase 3 Step 3:教训(lessons)——错误学习
    # ---------------------------------------------------------------
    def add_lesson(self, session_id: str, lesson: str,
                   category: str = "general") -> int:
        """新增教训条目(状态 pending);返回 id。"""
        now = self._now_iso()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            cur = conn.execute(
                """INSERT INTO lessons (session_id, lesson, category, status,
                   created_at, updated_at, hits)
                   VALUES (?, ?, ?, 'pending', ?, ?, 0)""",
                (session_id, lesson, category, now, now),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def list_lessons(self, status: str | None = None) -> list[dict[str, Any]]:
        """列出教训(status 过滤可选);按 id 倒序(新教训在前)。"""
        conn = sqlite3.connect(self.db_path)
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM lessons WHERE status = ? ORDER BY id DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM lessons ORDER BY id DESC"
                ).fetchall()
            cols = [d[0] for d in conn.execute("SELECT * FROM lessons").description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def delete_lesson(self, lesson_id: int) -> bool:
        """删除教训条目;不存在返回 False。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            cur = conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def confirm_lesson(self, lesson_id: int) -> bool:
        """用户确认教训(pending → verified);不存在返回 False。"""
        now = self._now_iso()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            cur = conn.execute(
                "UPDATE lessons SET status = 'verified', updated_at = ? WHERE id = ?",
                (now, lesson_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def increment_lesson_hits(self, lesson_ids: list[int]) -> None:
        """注入命中计数;命中 >=2 的 pending 教训自动转 verified。"""
        if not lesson_ids:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.executemany(
                "UPDATE lessons SET hits = hits + 1 WHERE id = ?",
                [(i,) for i in lesson_ids],
            )
            conn.execute(
                "UPDATE lessons SET status = 'verified' "
                "WHERE hits >= 2 AND status = 'pending'"
            )
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------
    # 会话占用锁(2026-08-21):多进程防"同 session 双开"导致历史交错
    # ---------------------------------------------------------------
    # 锁 TTL:超过视为残留(进程崩溃/强关),自动接管
    LOCK_TTL_SECONDS = 24 * 3600

    @staticmethod
    def _now_iso_utc() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def acquire_session_lock(self, session_id: str, owner: str,
                             force: bool = False) -> tuple[bool, str]:
        """获取会话占用锁。

        返回 (成功, 消息):成功 = 拿到锁(或已是自己持有);
        失败 = 被其他进程持有(TTL 内),消息含持有者信息。
        force=True:无视他人持有直接覆盖(用户强制接管,resume 用)。
        锁语义:同 session 双开防呆——不是强制排他,警告由调用方处理。
        """
        now = self._now_iso_utc()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            row = conn.execute(
                "SELECT locked_by, locked_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False, "会话不存在"
            locked_by, locked_at = row
            if locked_by == owner:
                return True, "已是本进程持有"
            if locked_by and locked_at and not force:
                from datetime import datetime, timezone
                try:
                    ts = datetime.fromisoformat(locked_at)
                    if (datetime.now(timezone.utc) - ts).total_seconds() < self.LOCK_TTL_SECONDS:
                        return False, f"被 {locked_by} 持有(自 {locked_at})"
                except ValueError:
                    pass  # 时间戳解析失败视为残留,接管
            conn.execute(
                "UPDATE sessions SET locked_by = ?, locked_at = ? WHERE id = ?",
                (owner, now, session_id),
            )
            conn.commit()
            return True, "已锁定"
        finally:
            conn.close()

    def release_session_lock(self, session_id: str, owner: str) -> None:
        """释放会话锁(仅 owner 匹配时,防误释放他人锁)。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute(
                "UPDATE sessions SET locked_by = NULL, locked_at = NULL "
                "WHERE id = ? AND locked_by = ?",
                (session_id, owner),
            )
            conn.commit()
        finally:
            conn.close()

    def get_session_lock(self, session_id: str) -> dict | None:
        """查询会话锁信息(供展示/调试)。"""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT locked_by, locked_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return {"locked_by": row[0], "locked_at": row[1]}
        finally:
            conn.close()
