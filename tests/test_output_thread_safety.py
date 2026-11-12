"""输出层线程安全（2026-11-11 输入架构改造 第 1 步）。

============ 为什么要做 ============
用户:"我想了想,hermes 的交互习惯是在 agent 处理 tool calling 时
     依旧等待用户输入...用户可以输入和编辑任意东西。"

这要求 agent 跑在**后台线程**、主线程被 prompt_toolkit Application
占着渲染输入区。此时后台线程直接 print() 会画花输入区 ——
这正是用户此前报的"工具轮间清屏/历史被擦掉"的同一类问题
(两个写入方 + 一个在管光标)。

============ 本步做什么 ============
① ui 层提供 safe_print:有活跃 Application 时走 run_in_terminal,
   否则直接 print(现在的行为,零变化)
② **所有输出收口到 ui**:core.py 42 处、write_file.py 23 处裸 print
   全部改成 ui.raw —— 否则"收口"是假的,那 65 处照样会画花屏幕
③ 现在还没 Application,所以 set_active_app 是留好的接线点,
   第 3-5 步只需要让它返回真对象
"""
import ast
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# ① safe_print 的两种分支
# ============================================================
class TestSafePrint:
    def test_no_app_falls_back_to_print(self, capsys):
        """没有活跃 app 时行为与 print 完全一致。"""
        import ui as ui_mod
        ui_mod.set_active_app(None)
        ui_mod.safe_print("hello")
        assert capsys.readouterr().out == "hello\n"

    def test_end_argument_respected(self, capsys):
        import ui as ui_mod
        ui_mod.set_active_app(None)
        ui_mod.safe_print("x", end="")
        assert capsys.readouterr().out == "x"

    def test_inactive_app_treated_as_none(self, capsys):
        """app 存在但没在跑(已退出)→ 必须退回直接打印。

        否则 run_in_terminal 会抛异常,输出就丢了。
        """
        import ui as ui_mod

        class DeadApp:
            is_running = False

        ui_mod.set_active_app(DeadApp())
        try:
            ui_mod.safe_print("after-exit")
            assert "after-exit" in capsys.readouterr().out
        finally:
            ui_mod.set_active_app(None)

    def test_broken_app_does_not_lose_output(self, capsys):
        """app.is_running 访问就抛异常时,输出也不能丢。"""
        import ui as ui_mod

        class BrokenApp:
            @property
            def is_running(self):
                raise RuntimeError("boom")

        ui_mod.set_active_app(BrokenApp())
        try:
            ui_mod.safe_print("survives")
            assert "survives" in capsys.readouterr().out
        finally:
            ui_mod.set_active_app(None)

    def test_ui_raw_goes_through_safe_print(self, capsys):
        """ui.raw 必须走 safe_print(而不是自己 print)。"""
        import ui as ui_mod
        ui_mod.set_active_app(None)
        ui_mod.ui.raw("via-safe")
        assert "via-safe" in capsys.readouterr().out


# ============================================================
# ② 收口检查:不许有裸 print 绕过 ui
# ============================================================
class TestNoBarePrint:
    """★ 这条是本次改造的核心防线。

    ui.safe_print 建好了但没人用 = 白做。实测:core.py 有 42 处、
    tools/write_file.py 有 23 处裸 print,它们跑在后台线程时
    照样会画花 prompt_toolkit 的输入区。
    """

    # 允许保留 print 的文件及理由
    ALLOWED = {
        "ui.py": "safe_print 自身必须用真 print",
        "colors.py": "",
        "main.py": "启动期(应用还没起来),走真 print 更简单可靠",
        "input_layer.py": (
            "粘贴提示在 prompt_toolkit 的按键回调里执行 —— "
            "那里已经在用 run_in_terminal 包着(safe_print 是给"
            "后台线程用的,这条路径在应用内部,层次不同)"),
    }

    # 不参与扫描的目录:独立脚本/模板,不跑在 dummy 的后台线程里
    SKIP_DIRS = ("tests/", ".git/", "garage/", "base_ws/", "docs/",
                 "scripts/", "skills/")

    def _py_files(self):
        for p in ROOT.rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith(self.SKIP_DIRS):
                continue
            yield rel, p

    def test_no_bare_print_outside_allowlist(self):
        offenders = []
        for rel, p in self._py_files():
            if rel in self.ALLOWED:
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "print"):
                    offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, (
            "这些地方直接调了 print(后台线程里会画花输入区),"
            "应改用 ui.raw:\n  " + "\n  ".join(offenders))


# ============================================================
# ③ 接线点
# ============================================================
def test_set_active_app_exists():
    """第 3 步要调这个函数 —— 名字变了要立刻发现。"""
    import ui as ui_mod
    assert callable(getattr(ui_mod, "set_active_app", None))


def test_safe_print_handles_empty_line(capsys):
    """write_file 的 diff 预览里有大量 raw("") 空行,不能丢。"""
    import ui as ui_mod
    ui_mod.set_active_app(None)
    ui_mod.ui.raw("")
    assert capsys.readouterr().out == "\n"


# ============================================================
# ④ 输出诊断（用户报"清屏"至今未定位，2026-11-11）
# ============================================================
class TestOutputTrace:
    """为什么要它:信息停在"多发于思考之前"，不足以定位，需要运行时事实。

    DUMMY_OUTPUT_TRACE=1 时每次输出记一条:
        时间 / 种类 / 光标位置 / 终端尺寸 / 长度 / 首行
    下次清屏,拿 logs/output_trace.log 就能看到"清屏前最后一次输出
    是什么、当时光标在哪"。
    """

    def test_disabled_by_default(self):
        import ui as ui_mod
        # 默认(env 没设)不该写日志
        ui_mod.trace_output("raw", "x")
        # 只要不抛异常即可 —— 真正是否写由 _TRACE 决定

    def test_enabled_writes_line(self, tmp_path, monkeypatch):
        import ui as ui_mod
        log = tmp_path / "trace.log"
        monkeypatch.setattr(ui_mod, "_TRACE", True)
        monkeypatch.setattr(ui_mod, "_TRACE_PATH", str(log))
        ui_mod.trace_output("thinking", "第一行\n第二行")
        content = log.read_text(encoding="utf-8")
        assert "kind=thinking" in content
        assert "第一行" in content
        assert "第二行" not in content, "只记首行,避免日志爆炸"

    def test_trace_never_raises(self, monkeypatch):
        """诊断代码绝不能影响主流程。"""
        import ui as ui_mod
        monkeypatch.setattr(ui_mod, "_TRACE", True)
        monkeypatch.setattr(ui_mod, "_TRACE_PATH", "/nonexistent/x/y.log")
        ui_mod.trace_output("raw", "x")      # 不该抛

    def test_emit_routes_through_trace(self, monkeypatch, capsys):
        """emit/raw 都要经过 trace(否则定位不到是哪次输出后清的)。

        ★ 用 PlainRenderer:测试环境非 TTY,默认的是 SilentRenderer,
          对话流(KIND_AGENT)会返回 None,压根走不到 trace。
        """
        import ui as ui_mod
        calls = []
        monkeypatch.setattr(ui_mod, "trace_output",
                            lambda k, t: calls.append((k, t)))
        monkeypatch.setattr(ui_mod, "ui", ui_mod.UI(ui_mod.PlainRenderer()))
        ui_mod.set_active_app(None)
        ui_mod.ui.raw("via-raw")
        ui_mod.ui.show(ui_mod.KIND_AGENT, "via-show")
        assert ("raw", "via-raw") in calls
        assert any(k == "agent" for k, _ in calls), calls
