"""autopilot 下 /p 必须能终止 tool calling loop（2026-11-11 用户报障）。

============ 用户报告 ============
"帮我修一个问题:autopilot 模式下 /p 不能终止 tool calling loop"

============ 根因链（三条,缺一不可导致失效）============

① **autopilot 下没有"输入环节"可依赖**
   非 autopilot 时,工具确认走 read_plain → prompt_toolkit 在活动,
   用户敲的 /p 被它读到并作为返回值交给 confirm → 检测到 /p →
   raise InterruptSignal。这条路能打断。
   autopilot 时 confirm 直接 return "y",**全程没有输入调用** ——
   打断只剩 msvcrt 轮询这一条路。

② **terminal 工具把主线程彻底阻塞**
   `proc.wait(timeout=timeout)` 一阻塞就是整个超时(默认 120 秒),
   期间没人检查监听器 buffer。用户敲的 /p 要等命令跑完才被发现。

③ **_run_shell 的 except Exception 把 InterruptSignal 吞了**
   实测得到:
       "[错误] 命令执行失败: InterruptSignal: 用户 /p 打断"
   于是打断变成"工具报错"——模型以为工具失败,换个命令接着干,
   loop 根本停不下来。这条最隐蔽。

============ 修法 ============
· core 通过 set_interrupt_probe() 把"是否收到 /p"交给 terminal 工具
  (不在 tools 层 import core,避免循环导入)
· _run_shell 把"一次性 wait"改成 50ms 小步轮询,每步回调探针
· 命中就杀进程树并抛 InterruptSignal —— 打断**立刻生效**
· peek_interrupt() **不消费** buffer:确认是 /p 才杀进程,
  之后检查点照常消费并走统一收尾(否则用户打错字的行会丢)
· _run_shell / terminal_handler 都显式放行 InterruptSignal
· _run_with_timeout(future.result) 也要放行 —— 它会把 handler 的
  异常重新抛出,不显式放行同样会被 dispatch 的兜底吞掉
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core                                          # noqa: E402
import tools.terminal as T                           # noqa: E402
from tools import create_default_registry            # noqa: E402


@pytest.fixture(autouse=True)
def clean_probe():
    """每条测试后把探针还原,避免相互污染。"""
    yield
    T.set_interrupt_probe(None)


# ============================================================
# ① peek_interrupt:只读不消费
# ============================================================
class TestPeekInterrupt:
    def test_peek_true_on_p(self):
        l = core._InterruptListener()
        with l._lock:
            l._buffer += "/p" + chr(10)
        assert l.peek_interrupt() is True

    def test_peek_does_not_consume(self):
        """★ 关键:peek 不能吃掉 buffer。

        如果用 take_line 做探针,用户打错字(不是 /p)的那一行会被
        提前消费掉 —— 后面检查点再也看不到它,那行输入就丢了。
        """
        l = core._InterruptListener()
        with l._lock:
            l._buffer += "/p" + chr(10)
        assert l.peek_interrupt() is True
        assert l.peek_interrupt() is True, "第二次 peek 仍应为 True(没被消费)"
        assert l.take_line() == "/p"
        assert l.peek_interrupt() is False, "消费后应为 False"

    def test_peek_false_for_other_input(self):
        l = core._InterruptListener()
        with l._lock:
            l._buffer += "hello" + chr(10)
        assert l.peek_interrupt() is False
        # 行不能被吞
        assert l.take_line() == "hello"

    def test_peek_false_without_newline(self):
        """还没按回车时不算完整行。"""
        l = core._InterruptListener()
        with l._lock:
            l._buffer += "/p"
        assert l.peek_interrupt() is False


# ============================================================
# ② 长命令执行期间能被 /p 打断
# ============================================================
class TestInterruptDuringCommand:
    def _reg(self):
        r = create_default_registry()
        r.set_confirm_provider(lambda p: "")
        return r

    def test_long_command_interrupted(self):
        """sleep 10 应在约 2 秒内被打断,而不是等满 10 秒。"""
        start = time.time()
        T.set_interrupt_probe(lambda: time.time() - start > 1.5)
        reg = self._reg()

        t0 = time.time()
        with pytest.raises(core.InterruptSignal):
            reg.dispatch("terminal", {"command": "sleep 10"})
        elapsed = time.time() - t0
        assert elapsed < 5, f"打断太慢: {elapsed:.2f}s(应约 1.5-2s)"
        assert elapsed > 1.0, f"没等到 /p 就退出了: {elapsed:.2f}s"

    def test_no_interrupt_runs_normally(self):
        """没有 /p 时行为不变。"""
        T.set_interrupt_probe(lambda: False)
        reg = self._reg()
        out = reg.dispatch("terminal", {"command": "echo hi"})
        assert "hi" in out

    def test_interrupt_signal_not_swallowed(self):
        """★ 回归防线:打断不能变成"[错误] 命令执行失败: InterruptSignal"。

        这是实测踩到的:terminal_handler 的 except Exception 把它兜成
        错误字符串,模型以为工具失败,换个命令接着干,loop 停不下来。
        """
        T.set_interrupt_probe(lambda: True)     # 立刻就有 /p
        reg = self._reg()
        with pytest.raises(core.InterruptSignal):
            reg.dispatch("terminal", {"command": "sleep 10"})


# ============================================================
# ③ 异常穿透链路
# ============================================================
class TestSignalPropagation:
    def test_run_with_timeout_propagates_signal(self):
        """future.result() 会重新抛出 handler 的异常,必须显式放行。"""
        from tools.registry import InterruptSignal, Tool, ToolRegistry

        def boom(**kw):
            raise InterruptSignal()

        reg = ToolRegistry()
        reg.register(name="boom", description="x", parameters={},
                     handler=boom, timeout=5)
        with pytest.raises(InterruptSignal):
            reg.dispatch("boom", {})

    def test_terminal_handler_reraises(self):
        """terminal_handler 不能把 InterruptSignal 吞成字符串。"""
        from tools.registry import InterruptSignal
        T.set_interrupt_probe(lambda: True)
        with pytest.raises(InterruptSignal):
            T.terminal_handler("sleep 10")


# ============================================================
# ④ 探针注入
# ============================================================
class TestProbeWiring:
    def test_agent_injects_probe(self):
        """DummyAgent 构造后,terminal 模块应已拿到 probe。"""
        T.set_interrupt_probe(None)
        core.DummyAgent(None, create_default_registry(), system_prompt="P")
        assert T._interrupt_probe is not None, "agent 没把 probe 装给 terminal"

    def test_probe_returns_bool(self):
        core.DummyAgent(None, create_default_registry(), system_prompt="P")
        assert T._default_on_poll() is False     # buffer 空时为 False

    def test_no_probe_means_never_interrupt(self):
        """单测/非交互环境下没有 probe,程序不能崩或被误打断。"""
        T.set_interrupt_probe(None)
        assert T._default_on_poll() is False


# ============================================================
# ⑤ "幽灵 /p":打断一次之后,不能每轮都自动触发
# ============================================================
class TestNoGhostInterrupt:
    """用户报障(2026-11-11):
    "我打 /p 不能立马停止,结果到了下一轮对话我啥也不打
     又开始一直触发 /p 停止。"

    ============ 根因 ============
    第一版探针用的是 peek_interrupt(**不消费**),于是:

        ① 命令执行中敲 /p → buffer = "/p\n"
        ② peek → True → 杀进程、抛 InterruptSignal
        ③ **buffer 里那行还在**
        ④ interrupt_triggered=True → 检查点 B 的
           `if not interrupt_triggered` 判断为 False → **跳过 drain**
        ⑤ 这一行永远没人清
        ⑥ 下一轮任何工具执行,peek 又看到它 → 立刻再打断
           → **每轮都触发一次,直到 LLM 返回文本**

    ============ 修法 ============
    · 探针改成 consume_interrupt():确认打断的同时把行取走
    · 检查点 A/B 改成**无条件** drain:即使已经在打断流程里,
      也要把 buffer 清干净(漏清 = 留下幽灵 /p)
    """

    def test_consume_interrupt_removes_line(self):
        l = core._InterruptListener()
        with l._lock:
            l._buffer += "/p" + chr(10)
        assert l.consume_interrupt() is True
        # 消费后 buffer 必须干净
        assert l.consume_interrupt() is False, "第二行不该还有 /p"
        assert l.take_line() is None, "buffer 应该空了"

    def test_consume_returns_false_for_other_input(self):
        """不是 /p 的行要被消费掉吗?——是的,但返回 False。

        (这一行会由 take_line 之外的方式丢掉?不 —— 见下一条:
         实际上 consume 也会把它取走,所以调用方只在返回 True 时
         才当打断处理。)
        """
        l = core._InterruptListener()
        with l._lock:
            l._buffer += "hello" + chr(10)
        assert l.consume_interrupt() is False

    def test_probe_is_consume_not_peek(self):
        """接线检查:探针必须是 consume 语义,不能是 peek。

        这是本轮 bug 的直接防线 —— 用 peek 会留幽灵 /p。
        """
        import ast
        import pathlib

        src = (pathlib.Path(__file__).resolve().parent.parent
               / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "__init__":
                continue
            body = ast.unparse(node)
            if "set_interrupt_probe" not in body:
                continue
            assert "consume_interrupt" in body, (
                "探针必须是 consume_interrupt —— 用 peek 会留下幽灵 /p,"
                "导致下一轮每轮都自动触发打断")
            assert "peek_interrupt)" not in body
            return
        raise AssertionError("没找到注入探针的地方")

    def test_checkpoints_drain_unconditionally(self):
        """检查点不能再写 `if not interrupt_triggered: drain()`。

        那样在"已经打断过"时会跳过清理,残留行留到下一轮。
        """
        import ast
        import pathlib

        src = (pathlib.Path(__file__).resolve().parent.parent
               / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_chat_impl":
                body = ast.unparse(node)
                assert "if not interrupt_triggered:\n                interrupt_triggered = self._drain_interrupt()" not in body, \
                    "检查点还在条件 drain —— 会留幽灵 /p"
                return
        raise AssertionError("没找到 _chat_impl")

    def test_no_ghost_after_interrupt(self):
        """端到端:打断一次后,后续工具执行不该再自动被打断。"""
        T.set_interrupt_probe(None)
        agent = core.DummyAgent(None, create_default_registry(),
                                system_prompt="P")
        lis = agent._interrupt_listener

        # 模拟:用户在命令执行中敲了 /p
        with lis._lock:
            lis._buffer += "/p" + chr(10)

        # 第一次:应该触发
        assert lis.consume_interrupt() is True
        # 第二次(下一轮工具执行):不该再触发
        assert lis.consume_interrupt() is False, \
            "幽灵 /p:第一次打断后 buffer 没清干净"
