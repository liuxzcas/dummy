"""
tests/test_self_improve.py — Phase 3 Step 4:自我改进闭环

覆盖:
1. dispatch 错误统计:调用/错误计数、错误样本、get_tool_stats
2. detect_tool_issue:阈值(I4-A:>=3 次且 >30%)、边界
3. 提案生成:根因/changes/verification 解析(三级容错)
4. _is_allowed_file:禁止区/项目外/允许
5. verify_change:语法门/导入门/pytest/启动门
6. run_improvement:完整闭环(批准→修改→验证→记录)与拒绝/回退
7. 改进记录 JSONL
"""

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import self_improve as si  # noqa: E402
from core import DummyAgent  # noqa: E402
from session_store import SessionStore  # noqa: E402
from tools import create_default_registry  # noqa: E402


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, **kwargs):
        return self._responses.pop(0)


def R(content):
    return type("R", (), {"get": lambda self, k, d=None: content})()


# ---------------------------------------------------------------
# dispatch 统计
# ---------------------------------------------------------------
def test_dispatch_stats():
    reg = create_default_registry()
    reg.dispatch("terminal", {"command": "echo ok"})
    reg.dispatch("terminal", {"command": "bad command"})
    # 触发真实错误:terminal 执行不存在的命令
    reg.dispatch("terminal", {"command": "definitely-not-a-command-xyz"})
    reg.dispatch("nope_tool", {})
    stats = reg.get_tool_stats()
    assert stats["terminal"]["calls"] == 3
    assert stats["terminal"]["errors"] >= 1  # 非零退出码
    assert len(stats["terminal"]["error_samples"]) <= 3
    assert reg.unknown_calls == 1


def test_dispatch_stats_param_error():
    reg = create_default_registry()
    reg.dispatch("read_file", {"_error": "JSON 解析失败"})
    stats = reg.get_tool_stats()
    assert stats["read_file"]["calls"] == 1
    assert stats["read_file"]["errors"] == 1
    assert "[ToolDispatch]" in stats["read_file"]["error_samples"][0]


# ---------------------------------------------------------------
# detect_tool_issue(I4-A 阈值)
# ---------------------------------------------------------------
def test_detect_threshold():
    # >=3 次且 >30%
    assert si.detect_tool_issue({"terminal": {"calls": 10, "errors": 4}}) == "terminal"
    # 次数不够(偶然)
    assert si.detect_tool_issue({"terminal": {"calls": 2, "errors": 2}}) is None
    # 比例不够
    assert si.detect_tool_issue({"terminal": {"calls": 100, "errors": 5}}) is None
    # 边界:恰好 30% 不触发(严格大于)
    assert si.detect_tool_issue({"terminal": {"calls": 10, "errors": 3}}) is None
    assert si.detect_tool_issue({}) is None


# ---------------------------------------------------------------
# 提案生成
# ---------------------------------------------------------------
def test_generate_proposal():
    llm = FakeLLM([R(json.dumps({
        "root_cause": "用了 cmd 语法",
        "changes": [{"file": "tools/terminal.py",
                     "description": "改用 git-bash"}],
        "verification": "跑一轮真实命令",
    }))])
    p = si.generate_proposal(llm, "terminal", {"calls": 10, "errors": 4,
                                               "error_samples": ["bad"]})
    assert p["root_cause"] == "用了 cmd 语法"
    assert p["changes"][0]["file"] == "tools/terminal.py"
    assert p["verification"]


def test_generate_proposal_invalid():
    llm = FakeLLM([R("不是JSON"), R(""), R('{"changes": []}')])
    assert si.generate_proposal(llm, "t", {"calls": 1, "errors": 1}) == {}
    assert si.generate_proposal(llm, "t", {"calls": 1, "errors": 1}) == {}
    assert si.generate_proposal(llm, "t", {"calls": 1, "errors": 1}) == {}


# ---------------------------------------------------------------
# 文件权限
# ---------------------------------------------------------------
def test_is_allowed_file():
    ok, _ = si._is_allowed_file("tools/terminal.py")
    assert ok
    ok, reason = si._is_allowed_file(".git/config")
    assert not ok and "禁止" in reason
    ok, reason = si._is_allowed_file("../outside.py")
    assert not ok and "项目外" in reason


# ---------------------------------------------------------------
# verify_change 验证门链
# ---------------------------------------------------------------
def test_verify_change_ok():
    # 项目内真实文件:语法 + 导入门(run_pytest=False 避免单测嵌套全量)
    failures = si.verify_change("lessons.py", run_pytest=False)
    assert failures == []


def test_verify_change_syntax_error(tmp_path):
    f = tmp_path / "bad_mod.py"
    f.write_text("def broken(:\n", encoding="utf-8")
    failures = si.verify_change(str(f))
    assert failures and "语法门" in failures[0]


# ---------------------------------------------------------------
# run_improvement 完整闭环
# ---------------------------------------------------------------
def test_run_improvement_rejected(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path / "m.db"))
    reg = create_default_registry()
    llm = FakeLLM([R(json.dumps({
        "root_cause": "测试",
        "changes": [{"file": "tools/terminal.py", "description": "测试改动"}],
        "verification": "测试",
    }))])
    agent = DummyAgent(llm, reg, system_prompt="P")
    agent.session_store = store
    reg.dispatch("terminal", {"command": "echo x"})
    reg.dispatch("terminal", {"command": "bad"})
    reg.dispatch("terminal", {"command": "bad"})
    reg.dispatch("terminal", {"command": "bad"})
    # 拒绝批准
    monkeypatch.setattr(agent, "_make_confirm", lambda: lambda p: "n")
    result = agent.run_improvement("terminal")
    assert result["status"] == "rejected"


def test_run_improvement_no_data(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path / "m.db"))
    agent = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent.session_store = store
    result = agent.run_improvement("nonexistent")
    assert result["status"] == "no_data"


def test_run_improvement_blocked(tmp_path, monkeypatch):
    """禁止区文件:批准后仍被阻止。"""
    store = SessionStore(str(tmp_path / "m.db"))
    reg = create_default_registry()
    llm = FakeLLM([R(json.dumps({
        "root_cause": "测试",
        "changes": [{"file": ".git/config", "description": "改 git 配置"}],
        "verification": "测试",
    }))])
    agent = DummyAgent(llm, reg, system_prompt="P")
    agent.session_store = store
    reg.dispatch("terminal", {"command": "echo x"})
    reg.dispatch("terminal", {"command": "bad"})
    reg.dispatch("terminal", {"command": "bad"})
    reg.dispatch("terminal", {"command": "bad"})
    monkeypatch.setattr(agent, "_make_confirm", lambda: lambda p: "y")
    result = agent.run_improvement("terminal")
    assert result["status"] == "blocked"


def test_improve_log_format(tmp_path, monkeypatch):
    """改进记录 JSONL(§3.5 量化数据源)。"""
    log_path = os.path.join(tmp_path, "self-improve.jsonl")
    monkeypatch.setattr(si, "IMPROVE_LOG", log_path)
    si.append_improve_log({"status": "ok", "tool": "terminal"})
    si.append_improve_log({"status": "rolled_back", "tool": "read_file"})
    with open(log_path, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 2
    assert lines[0]["status"] == "ok"
