"""输入源抽象（2026-11-11 输入架构改造 第 2 步）。

============ 为什么要这层 ============
用户要求:"agent 处理 tool calling 时依旧等待用户输入,按下回车才打断
并注入内容。" 完整方案(第 3-5 步)是 prompt_toolkit Application 常驻
+ agent 后台线程 + queue 传输入。

但 core 不该关心输入从哪来。所以先抽 InputSource:

    InputSource
      ├── ListenerSource  ← 现在:msvcrt 监听器(只有 /p 算打断)
      └── QueueSource     ← 第 3-4 步:prompt_toolkit 的 queue
                            (任何非空行都算打断,内容要保留注入)

不抽这层的话,第 3-4 步要同时改 core 检查点、terminal 探针、
main 主循环三处,出问题无法归因。

============ 本文件锁住什么 ============
1. 两个实现的语义差异(尤其"能不能消费"这个坑)
2. core 只依赖契约,不直接摸监听器
3. 轮末 drain:防止跨轮幽灵输入
"""
import os
import queue
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core                                          # noqa: E402
from input_source import (                           # noqa: E402
    InputSource, ListenerSource, QueueSource, strip_prefix,
)
from tools import create_default_registry            # noqa: E402


# ============================================================
# ① ListenerSource:保持旧语义(只有 /p 算打断)
# ============================================================
class TestListenerSource:
    def _src(self):
        l = core._InterruptListener()
        return l, ListenerSource(l)

    def test_take_returns_line(self):
        l, src = self._src()
        with l._lock:
            l._buffer += "hello" + chr(10)
        assert src.take() == "hello"

    def test_take_none_when_empty(self):
        _, src = self._src()
        assert src.take() is None

    def test_poll_true_on_p(self):
        l, src = self._src()
        with l._lock:
            l._buffer += "/p" + chr(10)
        assert src.poll() is True

    def test_poll_false_on_other_input(self):
        l, src = self._src()
        with l._lock:
            l._buffer += "hello" + chr(10)
        assert src.poll() is False

    def test_poll_consumes_p(self):
        """★ poll 必须消费 —— 不消费就是"幽灵 /p"那个 bug。"""
        l, src = self._src()
        with l._lock:
            l._buffer += "/p" + chr(10)
        assert src.poll() is True
        assert src.poll() is False, "第二次 poll 还是 True = buffer 没清"
        assert src.take() is None

    def test_poll_keeps_non_p_line(self):
        """非 /p 的行不能被 poll 吃掉(用户打错字也要能重来)。"""
        l, src = self._src()
        with l._lock:
            l._buffer += "hello" + chr(10)
        src.poll()
        assert src.take() == "hello"

    def test_drain_clears_everything(self):
        l, src = self._src()
        with l._lock:
            l._buffer += "a" + chr(10) + "/p" + chr(10) + "b" + chr(10)
        src.drain()
        assert src.take() is None


# ============================================================
# ② QueueSource:第 3-4 步要用的实现
# ============================================================
class TestQueueSource:
    def _src(self):
        q = queue.Queue()
        return q, QueueSource(q)

    def test_take_gets_item(self):
        q, src = self._src()
        q.put("打点字")
        assert src.take() == "打点字"

    def test_take_none_when_empty(self):
        _, src = self._src()
        assert src.take() is None

    def test_poll_true_when_pending(self):
        q, src = self._src()
        q.put("打断我")
        assert src.poll() is True

    def test_poll_does_not_consume(self):
        """★ 与 ListenerSource 相反:队列内容要注入给模型，不能取走就丢。"""
        q, src = self._src()
        q.put("这行要保留")
        assert src.poll() is True
        assert src.poll() is True, "poll 不该消费队列"
        assert src.take() == "这行要保留", "内容必须还在"

    def test_drain_clears(self):
        q, src = self._src()
        q.put("a")
        q.put("b")
        src.drain()
        assert src.take() is None


# ============================================================
# ③ strip_prefix:兼容用户继续敲 /p
# ============================================================
class TestStripPrefix:
    def test_strips_p_prefix(self):
        assert strip_prefix("/p 换个思路") == "换个思路"
        assert strip_prefix("/p") == ""

    def test_keeps_plain_text(self):
        assert strip_prefix("普通输入") == "普通输入"

    def test_handles_empty(self):
        assert strip_prefix("") == ""
        assert strip_prefix(None) == ""


# ============================================================
# ④ core 只依赖契约
# ============================================================
class TestCoreUsesAbstraction:
    def _agent(self):
        return core.DummyAgent(None, create_default_registry(),
                               system_prompt="P")

    def test_agent_has_input_source(self):
        a = self._agent()
        assert isinstance(a._input_source, InputSource)

    def test_drain_interrupt_delegates(self):
        """检查点必须走 input_source,不直接摸监听器。"""
        a = self._agent()
        with a._interrupt_listener._lock:
            a._interrupt_listener._buffer += "/p" + chr(10)
        assert a._drain_interrupt() is True
        assert a._drain_interrupt() is False, "第二次不该再 True"

    def test_terminal_probe_bound_to_input_source(self):
        import tools.terminal as T
        self._agent()
        assert T._interrupt_probe is not None
        # 探针等价于 input_source.poll
        assert T._default_on_poll() is False

    def test_round_end_drains(self):
        """轮末必须清空输入残留(防止跨轮幽灵)。"""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "chat":
                assert "_input_source.drain()" in ast.unparse(node), \
                    "轮末没清输入残留 —— 会跨轮触发幽灵打断"
                return
        raise AssertionError("没找到 chat()")


# ============================================================
# ⑤ 回归:幽灵 /p 不能复现
# ============================================================
def test_no_ghost_after_round_drain():
    """模拟完整一轮:打断 → 轮末 drain → 下一轮不该再触发。"""
    a = core.DummyAgent(None, create_default_registry(), system_prompt="P")
    with a._interrupt_listener._lock:
        a._interrupt_listener._buffer += "/p" + chr(10)

    assert a._drain_interrupt() is True      # 本轮打断
    a._input_source.drain()                  # 轮末清理
    assert a._drain_interrupt() is False     # 下一轮干净
