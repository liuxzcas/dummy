"""
tests/test_skills.py — Phase 3 Step 1:技能机制基础

覆盖:
1. list_skills:扫描目录、frontmatter 解析(name/description/type)
2. load_skill:读全文 / 不存在返回 None
3. delete_skill:删除 / 不存在返回 False
4. build_skills_index:索引格式(名称/描述/工作流标记)、空库
5. handle_skills_command:/skills list/show/del/错误处理
6. core._inject_skills:system prompt 注入索引、幂等、无技能跳过
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import main as mm  # noqa: E402
import skills_manager as sm  # noqa: E402
from core import DummyAgent  # noqa: E402
from session_store import SessionStore  # noqa: E402
from tools import create_default_registry  # noqa: E402


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    """隔离的技能目录(不影响仓库真实 skills/)。"""
    for name, content in {
        "search-literature": (
            "---\n"
            "name: search-literature\n"
            "description: 检索文献(arXiv),用于调研\n"
            "type: atomic\n"
            "---\n"
            "# 检索\n步骤一\n"
        ),
        "literature-review": (
            "---\n"
            "name: literature-review\n"
            "description: 文献综述流程\n"
            "type: workflow\n"
            "steps:\n"
            "  - search-literature\n"
            "---\n"
            "# 综述\n步骤一\n"
        ),
        "bad-skill": "没有 frontmatter 的文件",
    }.items():
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr(sm, "SKILLS_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------
# skills_manager
# ---------------------------------------------------------------
def test_list_skills(skills_dir):
    skills = sm.list_skills()
    names = [s["name"] for s in skills]
    assert "search-literature" in names
    assert "literature-review" in names
    assert "bad-skill" not in names  # frontmatter 解析失败跳过
    by_name = {s["name"]: s for s in skills}
    assert by_name["search-literature"]["type"] == "atomic"
    assert by_name["literature-review"]["type"] == "workflow"


def test_load_skill(skills_dir):
    content = sm.load_skill("search-literature")
    assert content is not None and "步骤一" in content
    assert sm.load_skill("nope") is None


def test_delete_skill(skills_dir):
    assert sm.delete_skill("search-literature") is True
    assert sm.load_skill("search-literature") is None
    assert sm.delete_skill("nope") is False


# ---------------------------------------------------------------
# validate_skill(Step 2:创建后校验)
# ---------------------------------------------------------------
def test_validate_skill_ok(skills_dir):
    ok, msg = sm.validate_skill("search-literature")
    assert ok is True, msg
    ok, msg = sm.validate_skill("literature-review")  # workflow + 引用存在
    assert ok is True, msg


def test_validate_skill_errors(skills_dir):
    # 不存在
    assert sm.validate_skill("nope")[0] is False
    # 缺 description
    d = skills_dir / "no-desc"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: no-desc\ntype: atomic\n---\n正文", encoding="utf-8")
    ok, msg = sm.validate_skill("no-desc")
    assert ok is False and "description" in msg
    # name 与目录不一致
    d2 = skills_dir / "dir-name"
    d2.mkdir()
    (d2 / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: x\ntype: atomic\n---\n正文",
        encoding="utf-8")
    ok, msg = sm.validate_skill("dir-name")
    assert ok is False and "不一致" in msg
    # type 非法
    d3 = skills_dir / "bad-type"
    d3.mkdir()
    (d3 / "SKILL.md").write_text(
        "---\nname: bad-type\ndescription: x\ntype: magic\n---\n正文",
        encoding="utf-8")
    ok, msg = sm.validate_skill("bad-type")
    assert ok is False and "type" in msg
    # workflow 缺 steps
    d4 = skills_dir / "wf-nosteps"
    d4.mkdir()
    (d4 / "SKILL.md").write_text(
        "---\nname: wf-nosteps\ndescription: x\ntype: workflow\n---\n正文",
        encoding="utf-8")
    ok, msg = sm.validate_skill("wf-nosteps")
    assert ok is False and "steps" in msg
    # workflow 引用不存在
    d5 = skills_dir / "wf-badref"
    d5.mkdir()
    (d5 / "SKILL.md").write_text(
        "---\nname: wf-badref\ndescription: x\ntype: workflow\nsteps:\n  - ghost-skill\n---\n正文",
        encoding="utf-8")
    ok, msg = sm.validate_skill("wf-badref")
    assert ok is False and "ghost-skill" in msg


def test_build_skills_index(skills_dir):
    index = sm.build_skills_index()
    assert "search-literature" in index
    assert "检索文献(arXiv)" in index
    assert "[工作流]" in index and "literature-review" in index
    assert "## 可用技能" in index
    assert "read_file 读取 skills/" in index


def test_build_skills_index_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "SKILLS_DIR", str(tmp_path / "none"))
    assert sm.build_skills_index() == ""


# ---------------------------------------------------------------
# /skills 命令(纯函数)
# ---------------------------------------------------------------
def test_skills_command_list(skills_dir, monkeypatch):
    monkeypatch.setattr(sm, "SKILLS_DIR", str(skills_dir))
    lines = mm.handle_skills_command("/skills")
    assert "📦 技能" in lines[0]
    assert any("search-literature" in l for l in lines)
    assert any("[工作流] literature-review" in l for l in lines)


def test_skills_command_show(skills_dir, monkeypatch):
    monkeypatch.setattr(sm, "SKILLS_DIR", str(skills_dir))
    lines = mm.handle_skills_command("/skills show search-literature")
    assert "步骤一" in lines[0]
    lines2 = mm.handle_skills_command("/skills show nope")
    assert "不存在" in lines2[0]


def test_skills_command_del(skills_dir, monkeypatch):
    monkeypatch.setattr(sm, "SKILLS_DIR", str(skills_dir))
    lines = mm.handle_skills_command("/skills del search-literature")
    assert "已删除" in lines[0]
    assert sm.load_skill("search-literature") is None
    lines2 = mm.handle_skills_command("/skills del nope")
    assert "不存在" in lines2[0]


# ---------------------------------------------------------------
# core._inject_skills
# ---------------------------------------------------------------
def test_inject_skills(tmp_path, skills_dir, monkeypatch):
    monkeypatch.setattr(sm, "SKILLS_DIR", str(skills_dir))
    store = SessionStore(str(tmp_path / "m.db"))
    agent = DummyAgent(None, create_default_registry(), system_prompt="基础提示")
    agent.session_store = store
    agent.history = [{"role": "system", "content": "基础提示"}]
    agent._inject_skills()
    content = agent.history[0]["content"]
    assert "## 可用技能" in content
    assert "search-literature" in content
    assert "基础提示" in content  # 原内容保留
    # 幂等:重复注入不叠加
    agent._inject_skills()
    content2 = agent.history[0]["content"]
    assert content2.count("## 可用技能") == 1


def test_inject_skills_empty(tmp_path, monkeypatch):
    """无技能时 system prompt 不动。"""
    monkeypatch.setattr(sm, "SKILLS_DIR", str(tmp_path / "none"))
    agent = DummyAgent(None, create_default_registry(), system_prompt="基础提示")
    agent.history = [{"role": "system", "content": "基础提示"}]
    agent._inject_skills()
    assert agent.history[0]["content"] == "基础提示"
