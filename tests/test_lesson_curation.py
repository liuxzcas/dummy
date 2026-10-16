"""Phase 3 Step 5:教训的适用条件 + 退役机制 + 计数判据修正。

============ 为什么需要这些(2026-10-16) ============
实测发现三条真问题:

**① 教训会过时,但没有退役机制**
  第 4 条教训写的是"在受 **30 秒**总超时限制的终端中,for 循环顺序 curl 会超时";
  而 terminal 的默认超时早已改成 **120 秒** → 该教训已不适用,
  却仍被**每轮注入**(每轮都付 token,还可能误导模型)。

**② hits 被污染**
  `increment_lesson_hits` 原来接收"**所有被注入的**" ——
  而注入含"保底填充"(检索没命中、纯粹为了不空转而补的条目),
  那些与当前输入无关也照样 +1 → hits 高只代表"碰巧被注入得多"。

**③ 无法判断"这条还适不适用"**
  代码不该解析语义(那是护栏的老错误),所以改成:
  让模型生成时写清 `applies_when`,注入时**一并展示** ——
  代码只摆事实,判断归模型。

本文件锁住上面三条的实现契约。
"""
import os
import sys
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def store(tmp_path):
    """独立的临时库(不碰开发用的 session.db)。"""
    from session_store import SessionStore
    return SessionStore(str(tmp_path / "t.db"))


# ============================================================
# ① 迁移:老库补 applies_when 列
# ============================================================
class TestSchemaMigration:
    def test_applies_when_column_exists(self, store):
        conn = sqlite3.connect(store.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(lessons)")}
        conn.close()
        assert "applies_when" in cols

    def test_migration_is_idempotent(self, store, tmp_path):
        """重复初始化不能报错(ALTER TABLE ADD COLUMN 本身不幂等)。"""
        from session_store import SessionStore
        for _ in range(3):
            SessionStore(str(tmp_path / "t.db"))
        # 能跑到这里就说明没抛异常

    def test_old_db_gets_column_without_data_loss(self, tmp_path):
        """模拟老库(无 applies_when 列)→ 迁移后数据不丢。"""
        db = str(tmp_path / "old.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, lesson TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0)""")
        conn.execute(
            "INSERT INTO lessons (session_id, lesson, created_at, updated_at) "
            "VALUES ('s', '老数据', '2026-01-01', '2026-01-01')")
        conn.commit()
        conn.close()

        from session_store import SessionStore
        s = SessionStore(db)          # 触发迁移
        rows = s.list_lessons()
        assert len(rows) == 1, "老数据不能丢"
        assert rows[0]["lesson"] == "老数据"
        assert rows[0]["applies_when"] == "", "新列应有默认空串"


# ============================================================
# ② 退役 / 恢复
# ============================================================
class TestRetireRestore:
    def test_retire_hides_from_default_list(self, store):
        """退役后默认列表看不到它。"""
        lid = store.add_lesson("s", "规则A", "cat")
        assert len(store.list_lessons()) == 1
        assert store.retire_lesson(lid) is True
        assert store.list_lessons() == [], "退役的不该出现在默认列表"
        assert len(store.list_lessons(include_retired=True)) == 1

    def test_restore_brings_back(self, store):
        """恢复后回到列表(pending)。"""
        lid = store.add_lesson("s", "规则A", "cat")
        store.retire_lesson(lid)
        assert store.restore_lesson(lid) is True
        rows = store.list_lessons()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending", "恢复应回到 pending 而非 verified"

    def test_retire_nonexistent_returns_false(self, store):
        assert store.retire_lesson(99999) is False

    def test_restore_nonexistent_returns_false(self, store):
        assert store.restore_lesson(99999) is False

    def test_retire_does_not_delete(self, store):
        """退役是**保留记录**(便于回看为什么当时学到这条),不是删除。"""
        lid = store.add_lesson("s", "规则A", "cat")
        store.retire_lesson(lid)
        rows = store.list_lessons(include_retired=True)
        assert len(rows) == 1
        assert rows[0]["lesson"] == "规则A"


# ============================================================
# ③ applies_when 的存取与生成
# ============================================================
class TestAppliesWhen:
    def test_stored_and_returned(self, store):
        lid = store.add_lesson("s", "大范围 find 会全盘扫描", "命令执行",
                               "目标目录含上万文件时")
        row = [l for l in store.list_lessons() if l["id"] == lid][0]
        assert row["applies_when"] == "目标目录含上万文件时"

    def test_defaults_to_empty(self, store):
        """不传就是空串(向后兼容老调用方)。"""
        lid = store.add_lesson("s", "规则A", "cat")
        row = [l for l in store.list_lessons() if l["id"] == lid][0]
        assert row["applies_when"] == ""

    def test_parser_extracts_applies_when(self):
        """generate_lesson 的解析器要能取出 applies_when。"""
        from lessons import _parse_lessons_response
        import json
        payload = json.dumps([{
            "lesson": "大范围 find 会全盘扫描",
            "category": "命令执行",
            "applies_when": "目标目录含上万文件时",
        }], ensure_ascii=False)
        out = _parse_lessons_response(payload)
        assert len(out) == 1
        assert out[0]["applies_when"] == "目标目录含上万文件时"

    def test_parser_tolerates_missing_field(self):
        """老格式(没 applies_when)→ 解析成空串,不报错。"""
        from lessons import _parse_lessons_response
        import json
        payload = json.dumps([{"lesson": "规则A", "category": "cat"}],
                             ensure_ascii=False)
        out = _parse_lessons_response(payload)
        assert out[0]["applies_when"] == ""

    def test_parser_regex_fallback_gets_applies_when(self):
        """第三级正则兜底也要能取到 applies_when。"""
        from lessons import _parse_lessons_response
        text = ('[{"lesson": "规则A", "category": "cat", '
                '"applies_when": "条件X"}]')
        # 破坏成非法 JSON,逼它走正则路径
        out = _parse_lessons_response(text.replace('"category": "cat",', "category: cat,"))
        assert out and out[0]["applies_when"] == "条件X"


# ============================================================
# ④ 生成 prompt 要求模型写 applies_when
# ============================================================
class TestGeneratePrompt:
    def test_prompt_asks_for_applies_when(self):
        """prompt 必须要求模型产出 applies_when,并说明该怎么写。"""
        import inspect
        import lessons
        src = inspect.getsource(lessons.generate_lesson)
        assert "applies_when" in src, "prompt 里应要求这个字段"
        assert "适用条件" in src or "什么条件下成立" in src, \
            "应对模型说明该字段的含义"
        assert "具体" in src, "应要求写具体的条件(否则会写成'某些情况下'这种空话)"

    def test_prompt_mentions_examples(self):
        """给出可模仿的例子(超时/系统/规模)—— 这类具体条件最容易验证。"""
        import inspect
        import lessons
        src = inspect.getsource(lessons.generate_lesson)
        assert "超时" in src or "Windows" in src, "应给出具体条件的示例"


# ============================================================
# ⑤ /lessons 命令
# ============================================================
class TestLessonsCommand:
    def _run(self, cmd, store):
        from main import handle_lessons_command
        return "\n".join(handle_lessons_command(cmd, store))

    def test_retire_command(self, store):
        lid = store.add_lesson("s", "规则A", "cat")
        out = self._run(f"/lessons retire {lid}", store)
        assert "已退役" in out
        assert store.list_lessons() == []

    def test_restore_command(self, store):
        lid = store.add_lesson("s", "规则A", "cat")
        store.retire_lesson(lid)
        out = self._run(f"/lessons restore {lid}", store)
        assert "已恢复" in out
        assert len(store.list_lessons()) == 1

    def test_retire_bad_id_shows_usage(self, store):
        out = self._run("/lessons retire", store)
        assert "用法" in out

    def test_retire_nonexistent_reports(self, store):
        out = self._run("/lessons retire 99999", store)
        assert "不存在" in out

    def test_list_shows_applies_when(self, store):
        store.add_lesson("s", "规则A", "cat", "条件X")
        out = self._run("/lessons", store)
        assert "适用条件" in out and "条件X" in out

    def test_list_all_shows_retired(self, store):
        lid = store.add_lesson("s", "规则A", "cat")
        store.retire_lesson(lid)
        assert "规则A" not in self._run("/lessons", store)
        assert "规则A" in self._run("/lessons -a", store)

    def test_retired_marked_in_all_list(self, store):
        lid = store.add_lesson("s", "规则A", "cat")
        store.retire_lesson(lid)
        assert "🚫" in self._run("/lessons -a", store)
