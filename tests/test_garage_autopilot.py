"""garage 工作区 + autopilot + 危险命令黑名单（2026-11-02）。

============ 用户的需求链 ============
1. "我配置了一个沙箱，如果我想要让 dummy 全托管自动运行 tool calling loop，
    不需要人去打 y、敲回车等等确认东西，仅保持 /p 打断的能力，
    需要修改哪些地方？"

2. "所有为了干活而生成的临时程序、临时脚本、临时文件（除非是用户指定要求的）
    都放在一个固定的路径下运行或修改，我想暂定在项目主目录 dummy 下的
    base_ws 里，这样方便管理和清理。同时也将黑名单范围中的①的范围缩小。"

3. 定案：目录名 garage / 相对路径默认落 garage / 清理手动（/workspace clean）/
   危险操作黑名单 / 开关只用命令行参数

============ 本文件锁住什么 ============
  · garage.resolve:相对路径 → garage,绝对路径原样
  · garage.clean:清空但保留目录和 .gitkeep
  · terminal 子进程 cwd = garage
  · write_file 相对路径落 garage
  · terminal 危险命令黑名单:命中即拒绝(不等人)
  · autopilot:确认自动批准 / /p 后不等提示词 / 会话锁冲突放弃
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workspace                                  # noqa: E402


# ============================================================
# ① garage 路径解析
# ============================================================
class TestGarageResolve:
    def test_relative_goes_to_garage(self):
        """相对路径 → garage 下（用户："相对路径默认落到 garage"）。"""
        out = workspace.resolve("foo.py")
        assert workspace.is_inside_garage(out), out
        assert out.endswith("foo.py")

    def test_nested_relative_also_inside(self):
        assert workspace.is_inside_garage(workspace.resolve("a/b/c.txt"))

    def test_absolute_untouched(self, tmp_path):
        """绝对路径原样保留（模型显式指定的位置要尊重）。"""
        target = str(tmp_path / "x.txt")
        assert workspace.resolve(target) == os.path.abspath(target)

    def test_empty_path_safe(self):
        assert workspace.resolve("") == ""

    def test_ensure_creates_dir(self):
        assert os.path.isdir(workspace.ensure())

    def test_is_inside_garage_rejects_others(self, tmp_path):
        assert not workspace.is_inside_garage(str(tmp_path / "x.txt"))


# ============================================================
# ② garage 清理
# ============================================================
class TestGarageClean:
    @pytest.fixture
    def clean_garage(self, monkeypatch, tmp_path):
        """把 garage 指到临时目录，避免动真实工作区。"""
        d = tmp_path / "garage"
        d.mkdir()
        (d / ".gitkeep").write_text("")
        monkeypatch.setattr(workspace, "GARAGE_DIR", str(d))
        return d

    def test_clean_removes_files(self, clean_garage):
        (clean_garage / "a.txt").write_text("hello")
        (clean_garage / "b.log").write_text("world")
        n, size = workspace.clean()
        assert n == 2 and size > 0
        assert not (clean_garage / "a.txt").exists()

    def test_clean_keeps_gitkeep(self, clean_garage):
        """`.gitkeep` 要保留 —— 否则 clone 后目录消失。"""
        (clean_garage / "a.txt").write_text("x")
        workspace.clean()
        assert (clean_garage / ".gitkeep").exists()

    def test_clean_removes_dirs(self, clean_garage):
        sub = clean_garage / "sub"
        sub.mkdir()
        (sub / "x.txt").write_text("y")
        n, _ = workspace.clean()
        assert n == 1
        assert not sub.exists()

    def test_clean_empty_is_noop(self, clean_garage):
        assert workspace.clean() == (0, 0)

    def test_listing(self, clean_garage):
        (clean_garage / "a.txt").write_text("12345")
        items = dict(workspace.listing())
        assert "a.txt" in items and items["a.txt"] == 5
        assert ".gitkeep" not in items, "不该列 .gitkeep"


# ============================================================
# ③ terminal 危险命令黑名单
# ============================================================
class TestDangerousBlocklist:
    """用户："危险操作定个黑名单吧"（autopilot 下没人拦，这是最后保险）。"""

    @pytest.fixture(autouse=True)
    def _import(self):
        import tools.terminal as t
        self.blocked = t._blocked_reason

    def test_destructive_commands_blocked(self):
        """用户明确点出的危险命令必须拦得住。"""
        assert self.blocked("rm -rf /") is not None
        assert self.blocked("rm -rf ~") is not None
        assert self.blocked("git push --force origin main") is not None
        assert self.blocked("shutdown -h now") is not None
        assert self.blocked("reboot") is not None

    def test_safe_commands_allowed(self):
        """日常命令不能被误拦 —— 沙箱就是为了让它干活。"""
        for cmd in [
            "ls -la", "python main.py", "git status", "git commit -m 'x'",
            "git push origin main",            # 非强推,允许
            "rm -rf ./build",                  # 删项目内子目录,允许
            "pip install requests",
            "cat file.txt | grep foo",
        ]:
            assert self.blocked(cmd) is None, f"误拦了: {cmd}"

    def test_case_insensitive(self):
        assert self.blocked("SHUTDOWN now") is not None

    def test_blocked_returns_reason(self):
        r = self.blocked("git push --force")
        assert r and "强制推送" in r

    def test_handler_refuses_blocked(self):
        """命中黑名单时 handler 直接返回拒绝，不执行、不确认。"""
        from tools.terminal import terminal_handler
        called = []

        def fake_confirm(p):
            called.append(p)
            return "y"

        out = terminal_handler("rm -rf /", _confirm=fake_confirm)
        assert "拒绝执行" in out
        assert "黑名单" in out
        assert not called, "被拒绝的命令不该走确认流程"


# ============================================================
# ④ terminal 子进程 cwd = garage
# ============================================================
def test_subprocess_cwd_is_garage():
    """子进程 cwd 固定到 garage（用户：临时产物放固定路径运行）。"""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "tools" / "terminal.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Dict) and node.value.keys):
            continue
        keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
        if "stdout" in keys and "stderr" in keys:
            found = True
            assert "cwd" in keys, "Popen kwargs 必须给 cwd=garage"
            idx = keys.index("cwd")
            assert "workspace" in ast.unparse(node.value.values[idx])
            break
    assert found


# ============================================================
# ⑤ write_file 相对路径落 garage
# ============================================================
def test_write_file_relative_goes_to_garage(tmp_path, monkeypatch):
    """write_file("foo.py") 应落到 garage，而不是 cwd。"""
    from tools.write_file import write_file_handler

    monkeypatch.setattr(workspace, "GARAGE_DIR", str(tmp_path / "garage"))
    monkeypatch.setattr(workspace, "ensure", lambda: str(tmp_path / "garage"))

    result = write_file_handler(path="foo.py", content="x=1\n", verify=False)
    assert "写入成功" in result
    assert (tmp_path / "garage" / "foo.py").exists(), result


def test_write_file_garage_needs_no_confirm(tmp_path, monkeypatch):
    """写 garage 内不该弹确认（用户要缩小黑名单范围）。"""
    from tools.write_file import write_file_handler

    monkeypatch.setattr(workspace, "GARAGE_DIR", str(tmp_path / "garage"))
    monkeypatch.setattr(workspace, "ensure", lambda: str(tmp_path / "garage"))

    asked = []

    def confirm(p):
        asked.append(p)
        return "y"

    result = write_file_handler(path="a.py", content="x\n", _confirm=confirm)
    assert "写入成功" in result
    assert not asked, f"写 garage 内不该确认: {asked}"


# ============================================================
# ⑥ autopilot
# ============================================================
class TestAutopilot:
    """用户："全托管自动运行 tool calling loop，不需要人去打 y、敲回车，
    仅保持 /p 打断的能力。"
    """

    @pytest.fixture
    def agent(self):
        import core
        from conftest import ScriptedLLM
        from tools import create_default_registry

        a = core.DummyAgent(ScriptedLLM([]), create_default_registry())
        a.autopilot = True
        yield a
        a.session_store.close() if hasattr(a.session_store, "close") else None

    def test_autopilot_defaults_off(self):
        import core
        from conftest import ScriptedLLM
        from tools import create_default_registry
        a = core.DummyAgent(ScriptedLLM([]), create_default_registry())
        assert a.autopilot is False

    def test_confirm_auto_approves(self, agent):
        """autopilot 下确认函数直接返回 "y"，不读输入。"""
        confirm = agent._make_confirm()
        # 若它试图读输入，pytest 里会因为没有 stdin 而报错/阻塞
        assert confirm("要确认吗? ") == "y"

    def test_confirm_auto_approve_does_not_raise(self, agent):
        """自动批准不能抛 InterruptSignal（那会被当成打断）。"""
        from tools.registry import InterruptSignal
        confirm = agent._make_confirm()
        try:
            r = confirm("x")
        except InterruptSignal:
            pytest.fail("autopilot 下不该抛 InterruptSignal")
        assert r == "y"

    def test_autopilot_skips_prompt_on_interrupt(self):
        """autopilot 下 /p 打断后不阻塞等提示词 —— 静态检查。"""
        import ast
        import pathlib

        src = (pathlib.Path(__file__).resolve().parent.parent
               / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 找 if self.autopilot: prompt = ""
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = ast.unparse(node.test)
            if "autopilot" not in test:
                continue
            body = ast.unparse(node)
            # ast.unparse 用单引号,别写成 'prompt = ""'
            if "prompt = ''" in body:
                found = True
                break
        assert found, "autopilot 下打断处理必须跳过 read_plain(否则死锁)"


# ============================================================
# ⑦ autopilot 下的 garage 新增报告（用户选 2.a：只在 autopilot 报）
# ============================================================
class TestGarageReport:
    """用户："2.a" —— 只在 autopilot 模式报告本轮 garage 新增。

    理由：普通模式下文件是边生成边显示的，用户当场看得见；
    autopilot 一轮可能跑几十次工具调用、屏幕滚动飞快，
    事后需要一句话告诉他这轮产出了哪些文件。

    只报不删 —— 清理仍手动（/workspace clean）。
    """

    def _agent(self, tmp_path, monkeypatch, autopilot):
        import core
        from conftest import ScriptedLLM
        from tools import create_default_registry

        d = tmp_path / "garage"
        d.mkdir()
        monkeypatch.setattr(workspace, "GARAGE_DIR", str(d))
        monkeypatch.setattr(workspace, "ensure", lambda: str(d))

        a = core.DummyAgent(ScriptedLLM(["收到"]), create_default_registry())
        a.autopilot = autopilot
        return a, d

    def test_reports_new_files(self, tmp_path, monkeypatch, capsys):
        a, d = self._agent(tmp_path, monkeypatch, autopilot=True)
        a._garage_snapshot = {}
        (d / "probe.py").write_text("x")
        capsys.readouterr()
        a._report_garage_changes()
        out = capsys.readouterr().out
        assert "garage 新增" in out and "probe.py" in out, out

    def test_no_report_when_not_autopilot(self, tmp_path, monkeypatch, capsys):
        a, d = self._agent(tmp_path, monkeypatch, autopilot=False)
        a._garage_snapshot = {}
        (d / "probe.py").write_text("x")
        capsys.readouterr()
        a._report_garage_changes()
        assert capsys.readouterr().out == "", "普通模式不该报"

    def test_no_report_when_nothing_new(self, tmp_path, monkeypatch, capsys):
        a, d = self._agent(tmp_path, monkeypatch, autopilot=True)
        (d / "old.py").write_text("x")
        a._garage_snapshot = dict(workspace.listing())
        capsys.readouterr()
        a._report_garage_changes()
        assert capsys.readouterr().out == "", "没有新增就不该报"

    def test_mentions_clean_command(self, tmp_path, monkeypatch, capsys):
        """报告要告诉用户怎么清。"""
        a, d = self._agent(tmp_path, monkeypatch, autopilot=True)
        a._garage_snapshot = {}
        (d / "a.py").write_text("x")
        capsys.readouterr()
        a._report_garage_changes()
        out = capsys.readouterr().out
        assert "/workspace clean" in out, out

    def test_snapshot_cleared_after_report(self, tmp_path, monkeypatch):
        """报告后清掉快照，避免下一轮误用。"""
        a, d = self._agent(tmp_path, monkeypatch, autopilot=True)
        a._garage_snapshot = {}
        a._report_garage_changes()
        assert a._garage_snapshot is None

    def test_no_snapshot_no_crash(self, tmp_path, monkeypatch):
        """没有快照(比如普通模式残留)也不能崩。"""
        a, d = self._agent(tmp_path, monkeypatch, autopilot=True)
        a._garage_snapshot = None
        a._report_garage_changes()      # 不该抛


# ============================================================
# ⑧ read_file 的相对路径基准也要统一到 garage
# ============================================================
def test_read_file_relative_uses_garage(tmp_path, monkeypatch):
    """三个工具必须同一基准,否则会出现这种返工:

        write_file("probe.py") → garage/probe.py
        read_file("probe.py")  → 找不到(去 cwd 找了)
        → 模型误以为写失败了

    用户 2026-11-03 实测 bug 的直接后果。
    """
    from tools.read_file import read_file_handler

    d = tmp_path / "garage"
    d.mkdir()
    (d / "probe.py").write_text("hello\nworld\n", encoding="utf-8")
    monkeypatch.setattr(workspace, "GARAGE_DIR", str(d))
    monkeypatch.setattr(workspace, "ensure", lambda: str(d))

    out = read_file_handler("probe.py")     # 相对路径
    assert "hello" in out and "world" in out, out


# ============================================================
# ⑨ _inject_workspace:路径约定 + 清理引导写进 system
# ============================================================
class TestInjectWorkspace:
    """用户 2026-11-03:"路径规则 + 清理引导合并成 _inject_workspace 一个
    注入段,放 system 的可变段?"

    ============ 为什么必须放 system 而不是工具描述 ============
    autopilot 下没有"轮末"—— 模型给出最终回答就结束了,
    不会再看任何东西。所以"轮末提醒它清理"不可行
    (会多一轮 LLM 调用,autopilot 下没人喊停 → 死循环风险)。

    只能在规划期就告诉它,而规划期看得到的只有 system prompt。
    """

    def _agent(self):
        import core
        from tools import create_default_registry
        a = core.DummyAgent(None, create_default_registry(), system_prompt="P")
        a.history = [{"role": "system", "content": "P"}]
        return a

    def test_injects_path_rules(self):
        a = self._agent()
        a._inject_workspace()
        c = a.history[0]["content"]
        assert "## 工作区约定" in c
        assert "garage" in c
        assert "绝对路径" in c and "相对路径" in c

    def test_injects_cleanup_guidance(self):
        a = self._agent()
        a._inject_workspace()
        c = a.history[0]["content"]
        assert "任务完成后" in c, "必须含清理引导"
        assert "一次性中间产物" in c

    def test_cleanup_wording_is_soft(self):
        """用户选的措辞:"可以"而不是"必须"(先试这个,怕过度删除)。"""
        a = self._agent()
        a._inject_workspace()
        c = a.history[0]["content"]
        assert "可以用 terminal 删掉" in c
        assert "必须清理" not in c, "用户选的是宽松措辞,不是硬性要求"

    def test_idempotent(self):
        """重复注入只有一个段(和 _inject_cwd 一致)。"""
        a = self._agent()
        a._inject_workspace()
        a._inject_workspace()
        assert a.history[0]["content"].count("## 工作区约定") == 1

    def test_preserves_original_prompt(self):
        a = self._agent()
        a._inject_workspace()
        assert a.history[0]["content"].startswith("P")

    def test_called_in_chat_impl(self):
        """必须在 _chat_impl 里真的被调用(不是定义了没人用)。"""
        import ast
        import pathlib

        src = (pathlib.Path(__file__).resolve().parent.parent
               / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_inject_workspace"]
        assert calls, "_inject_workspace 定义了但没被调用 —— 注入不会生效"
