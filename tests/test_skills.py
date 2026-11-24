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


pytestmark = pytest.mark.fast   # 测试分组见 pytest.ini


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


def test_frontmatter_multiline_description(skills_dir):
    """多行(folded >)description 必须拼成完整一行,不能只剩 '>'(anatomy 就是这种写法)。"""
    d = skills_dir / "folded-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "name: folded-skill\n"
        "description: >\n"
        "  Build crafted, highly detailed figures\n"
        "  that explain an idea as a physical object, with many small precise solid\n"
        "  parts, four-tone shading and correct painter order, so the reader\n"
        "  understands the idea because they recognise the machine.\n"
        "license: MIT\n"
        "  continued-value\n"
        "\n"
        "  block-body-line\n"
        "metadata:\n"
        "  opencode/slash: \"true\"\n"
        "---\n"
        "# body\n",
        encoding="utf-8",
    )
    meta = sm._parse_frontmatter(str(d / "SKILL.md"))
    desc = " ".join(meta["description"].split())
    assert desc == (
        "Build crafted, highly detailed figures that explain an idea as a physical object, "
        "with many small precise solid parts, four-tone shading and correct painter order, "
        "so the reader understands the idea because they recognise the machine."
    ), desc
    assert meta["name"] == "folded-skill"  # 续行没把 name 吞掉
    assert meta["license"].startswith("MIT continued-value")  # 缩进续行拼到上一个 key
    assert "block-body-line" not in meta["license"].split("\n")[0]  # 空行后另起一行,没和首行粘连
    assert meta["opencode/slash"] == '"true"'  # 嵌套 key 没被当成续行吞掉
    # 索引里描述是完整一句,而不是孤零零一个 '>' 或半截
    line = [ln for ln in sm.build_skills_index().splitlines() if ln.startswith("- folded-skill")][0]
    assert "Build crafted" in line and line.split(": ", 1)[1].endswith("…")
    assert not line.split(": ", 1)[1].startswith(">")
    # validate_skill 能识别这个技能
    assert sm.validate_skill("folded-skill") == (True, "OK")


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
    # 技能根目录注入(运行时计算,部署自适应)
    assert f"技能根目录: {sm.SKILLS_DIR}" in index


def test_build_skills_index_has_per_skill_dir(skills_dir):
    """每个技能都要带自己的目录 —— 正文里的相对路径以它为基准。

    之前索引只给总根目录,模型得自己拼 skills/<name>/…,那是推断不是保证。
    (anatomy 的 `node examples/build.mjs` 就依赖这个位置。)
    """
    index = sm.build_skills_index()
    for name in ("search-literature", "literature-review"):
        assert f"目录: {os.path.join(sm.SKILLS_DIR, name)}" in index
    # 明确告诉模型"相对路径以技能目录为基准"
    assert "以该技能的" in index


def test_prerequisites_missing_detected(tmp_path, monkeypatch):
    """声明了本机没有的命令 → 必须报出来,不能等跑到一半才炸。"""
    d = tmp_path / "needs-tool"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: needs-tool\ndescription: x\n"
        "prerequisites:\n  commands: [not-a-real-cmd-xyz]\n---\n", encoding="utf-8")
    monkeypatch.setattr(sm, "SKILLS_DIR", str(tmp_path))
    assert sm.missing_prerequisites("needs-tool") == ["not-a-real-cmd-xyz"]
    assert "缺少依赖命令" in sm.build_skills_index()
    assert "not-a-real-cmd-xyz" in sm.build_skills_index()


def test_prerequisites_satisfied_silent(tmp_path, monkeypatch):
    """依赖齐全时不许出现告警(否则每条技能都挂个红字,等于噪音)。"""
    import shutil as _sh
    have = "python" if _sh.which("python") else "git"
    d = tmp_path / "ok-tool"
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: ok-tool\ndescription: x\nprerequisites:\n  commands: [{have}]\n---\n",
        encoding="utf-8")
    monkeypatch.setattr(sm, "SKILLS_DIR", str(tmp_path))
    assert sm.missing_prerequisites("ok-tool") == []
    assert "缺少依赖命令" not in sm.build_skills_index()


def test_no_prerequisites_block_ok(skills_dir):
    """没写 prerequisites 的技能(大多数)不能报错 —— 之前缺 import re 会 NameError。"""
    assert sm.missing_prerequisites("search-literature") == []


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


# ---------------------------------------------------------------
# _inject_datetime(每轮刷新,省去 date 工具调用)
# ---------------------------------------------------------------
def test_inject_datetime(tmp_path):
    agent = DummyAgent(None, create_default_registry(), system_prompt="基础提示")
    agent.history = [{"role": "system", "content": "基础提示"}]
    agent._inject_datetime()
    content = agent.history[0]["content"]
    assert "今天是:" in content
    assert "基础提示" in content
    # 幂等:重复注入只一个日期段
    agent._inject_datetime()
    assert agent.history[0]["content"].count("今天是:") == 1


def test_inject_datetime_refresh(tmp_path, monkeypatch):
    """跨轮刷新:时间变化后重新注入,值更新。"""
    import datetime as dt
    # 注意:monkeypatch 替换的是全局 datetime 模块的 datetime 属性
    # (core.datetime 与这里 dt 是同一模块对象),先捕获真实类备用
    _real = dt.datetime
    agent = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent.history = [{"role": "system", "content": "P"}]
    fake = _real(2026, 8, 21, 10, 0)
    monkeypatch.setattr("core.datetime.datetime", _FakeDatetime(fake))
    agent._inject_datetime()
    # 精度到"天"(2026-09-16 改:分钟级会每轮打碎缓存前缀)
    assert "2026-08-21" in agent.history[0]["content"]
    # 时间前进,重新注入刷新
    fake2 = _real(2026, 8, 22, 9, 30)
    monkeypatch.setattr("core.datetime.datetime", _FakeDatetime(fake2))
    agent._inject_datetime()
    content = agent.history[0]["content"]
    assert "2026-08-22" in content
    assert content.count("今天是:") == 1
    # 同一天内重复注入，system 内容必须**逐字节不变**(缓存前缀稳定的前提)
    before = agent.history[0]["content"]
    agent._inject_datetime()
    assert agent.history[0]["content"] == before


def test_system_prompt_is_stable_across_turns(run_script, tmp_path):
    """system 消息在多轮 chat 之间**逐字节不变** —— 缓存前缀稳定的前提。

    为什么必须锁死:LLM 的 prompt cache 按**前缀匹配**,system 是消息序列的
    第 0 条 —— 它变一个字节,后面所有内容(含技能索引、本轮全部工具结果)
    都按未命中计费(¥3.0/M vs 缓存价 ¥0.1/M,差 30 倍)。

    2026-09-16 实测的问题:旧实现每轮改写 system 里的"当前时间"(精度到分钟),
    变更点在第 776 字符,后面 641 字符每轮白算。

    这条测试防的就是回归 —— 任何"每轮往 system 里写变化内容"的改动都会
    在这里失败。
    """
    agent, _ = run_script([
        ("read_file", {"path": str(tmp_path / "x.txt")}),
    ])
    first = agent.history[0]["content"]
    agent.chat("再问一句")
    second = agent.history[0]["content"]
    assert first == second, (
        "system 在多轮 chat 之间必须逐字节不变,否则缓存前缀被打碎。\n"
        f"  第 1 轮: {first[-120:]!r}\n"
        f"  第 2 轮: {second[-120:]!r}"
    )


def test_volatile_field_is_last_in_system(run_script, tmp_path):
    """system 里**唯一会变的内容**(日期)必须排在所有稳定内容之后。

    为什么重要:prompt cache 是**前缀匹配** —— 变更点越靠前,后面被废掉的
    内容越多。现在 system 里唯一会变的是"今天是: YYYY-MM-DD"(跨天变一次),
    它必须排在技能索引等稳定内容**之后**,这样跨天时受影响的只有它自己。

    2026-09-16 实测过反例:旧实现把"当前时间"(每轮变)放在第 776 字符,
    后面 641 字符(技能索引)每轮都白算。

    这条测试防的是"以后有人往 system 里加新内容时,把它插到了日期前面"。
    """
    agent, _ = run_script([
        ("read_file", {"path": str(tmp_path / "x.txt")}),
    ])
    content = agent.history[0]["content"]
    date_pos = content.find("今天是:")
    assert date_pos >= 0, "应有会变的日期字段"

    # 日期之后不应再出现"稳定但可能将来被误插入"的大块内容
    tail = content[date_pos:]
    for stable_marker in ("## 可用技能", "## 使用规则", "## 可用工具"):
        assert stable_marker not in tail, (
            f"稳定内容 {stable_marker!r} 排在了会变的日期字段之后 —— "
            "跨天时它会被缓存失效连累,应把它移到日期之前"
        )


# ---------------------------------------------------------------
# _inject_cwd(当前工作目录注入,§7 设计)
# ---------------------------------------------------------------
def test_inject_cwd(tmp_path):
    agent = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent.history = [{"role": "system", "content": "P"}]
    agent._inject_cwd()
    content = agent.history[0]["content"]
    cwd = os.getcwd()
    assert f"当前工作目录: {cwd}" in content
    # 2026-11-03:相对路径基准从 cwd 改成 garage（三个工具统一）
    assert "都以 garage 为基准" in content
    assert "绝对路径原样使用" in content
    assert content.startswith("P")  # 原内容保留
    # 幂等:重复注入只一个段
    agent._inject_cwd()
    assert agent.history[0]["content"].count("当前工作目录:") == 1


class _FakeDatetime:
    """替换 core.datetime.datetime 的替身(仅提供 now())。"""

    def __init__(self, fixed):
        self._fixed = fixed

    def now(self):
        return self._fixed
