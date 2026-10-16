"""残留进程报告 —— 命令留下的后台进程要如实报告给模型。

============ 分层原则(2026-09-18 用户定,2026-10-16 实施) ============
两种情形必须分开:

  情形一【机制】: 超时后的清理
      超时 → 杀整棵树 → 写进 script 流程,不问 LLM
      理由:不杀就泄漏,是机制性问题(类比 TCP 超时关闭连接)

  情形二【信息】: "我留下了什么进程" 这个事实
      登记表 → 报告给 LLM → LLM 判断要不要处理
      理由:"要不要杀掉残留进程"是**语义判断** ——
            用户可能故意启动了一个常驻服务,杀了反而错

本文件锁住情形二的契约:
  · 命令执行完 → 如果还留着活进程,如实报告(含命令原文 + pid + 跑了多久)
  · 报告是**事实陈述**,不含"建议杀掉"这类指令
  · 没有残留 → 不报告(不制造噪音)
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tasks import TaskRegistry, tasks   # noqa: E402


# ============================================================
# 报告内容
# ============================================================
class TestLingeringReport:
    """报告必须包含判断所需的全部事实。"""

    def test_report_mentions_command_and_pid(self):
        """报告要说明是**哪条命令**留下的、**哪个 pid**,以及跑了多久。

        模型需要这些才能判断:"这是我刚才那条 `npm run dev` 吗?
        它已经跑了 3 分钟,是正常的常驻服务吗?"
        """
        from tools.tasks import format_lingering_report
        reg = TaskRegistry()
        rec = reg.register(pid=99999, command="npm run dev")
        rec.descendants = [99998]      # 假装有个后代
        rec.still_running = lambda: [99998]   # 伪造"还活着"

        text = format_lingering_report([rec])
        assert "npm run dev" in text, "要报出命令原文"
        assert "99998" in text, "要报出还活着的 pid"

    def test_report_is_factual_not_directive(self):
        """报告只能是**事实陈述** —— 不能出现"建议杀掉"这类指令。

        这遵循项目原则:代码提供信息,判断归模型。
        (反例:如果写"请杀掉这些进程",那就是代码替模型做了决定。)
        """
        from tools.tasks import format_lingering_report
        reg = TaskRegistry()
        rec = reg.register(pid=99999, command="python server.py")
        rec.still_running = lambda: [99999]

        text = format_lingering_report([rec])
        for directive in ("建议杀掉", "请杀掉", "必须杀", "建议终止"):
            assert directive not in text, (
                f"报告里出现了指令性措辞 {directive!r} —— 应只陈述事实"
            )

    def test_report_handles_multiple_records(self):
        """多条残留记录都要报出来。"""
        from tools.tasks import format_lingering_report
        reg = TaskRegistry()
        a = reg.register(pid=99991, command="cmd-a")
        b = reg.register(pid=99992, command="cmd-b")
        a.still_running = lambda: [99991]
        b.still_running = lambda: [99992]
        text = format_lingering_report([a, b])
        assert "cmd-a" in text and "cmd-b" in text


# ============================================================
# 不制造噪音
# ============================================================
class TestNoNoiseWhenClean:
    """没有残留 → 不报告(否则每轮都多一段废话)。"""

    def test_no_report_when_nothing_lingering(self):
        from tools.tasks import format_lingering_report
        assert format_lingering_report([]) == ""

    def test_lingering_empty_for_finished_commands(self):
        """命令结束且无后台进程 → lingering() 返回空。"""
        reg = TaskRegistry()
        rec = reg.register(pid=os.getpid(), command="echo hi")   # 用自己 pid 保证"活着"
        rec.descendants = []
        reg.finish(rec, timed_out=False)
        # status=done → 根进程不再算"残留";没有后代 → 空
        assert reg.lingering() == []


# ============================================================
# 与 globals 单例的衔接
# ============================================================
class TestGlobalRegistry:
    """core 用的是模块级单例 `tasks`,要确认它的行为一致。"""

    def test_module_level_registry_exists(self):
        assert isinstance(tasks, TaskRegistry)

    def test_register_and_lingering_roundtrip(self):
        n_before = len(tasks.all_tasks())
        rec = tasks.register(pid=99999, command="__test_cmd__")
        assert len(tasks.all_tasks()) == n_before + 1
        assert rec.command == "__test_cmd__"
        # 清理:别污染其他测试
        tasks._tasks.remove(rec)


# ============================================================
# note_descendants 的累加语义（2026-10-16 修的真 bug）
# ============================================================
class TestDescendantsAccumulate:
    """note_descendants 必须**累加**,不能覆盖 —— 否则轮询探查会丢好结果。

    ============ 被这个 bug 坑过(2026-10-16 实测) ============
    原实现:`rec.descendants = list_descendants(rec.pid)` —— 每次都覆盖。

    在"趁 shell 还活着时轮询探查"的用法下,这会导致:
        第 1 次探:shell 活着  → 探到 3 个,写入 ✅
        第 2 次探:shell 已退出 → 探到 0 个,**覆盖成空** ❌

    实测现象正是如此:第一次调用返回 3 个 pid,最终 descendants 却是 []。
    """

    def test_accumulates_instead_of_overwriting(self):
        """先探到结果、再探到空 → 不能把好结果抹掉。"""
        reg = TaskRegistry()
        rec = reg.register(pid=999999, command="fake")

        # 第 1 次:假装探到 3 个
        rec.descendants = []
        calls = {"n": 0}

        import tools.tasks as T
        real = T.list_descendants

        def fake_list(pid):
            calls["n"] += 1
            return [111, 222, 333] if calls["n"] == 1 else []

        T.list_descendants = fake_list
        try:
            reg.note_descendants(rec)
            first = list(rec.descendants)
            reg.note_descendants(rec)       # 第 2 次探到空
            second = list(rec.descendants)
        finally:
            T.list_descendants = real

        assert first == [111, 222, 333], f"第一次应探到 3 个: {first}"
        assert second == [111, 222, 333], (
            f"第二次探到空不能覆盖已有结果;实际: {second}"
        )

    def test_merges_new_pids(self):
        """两次探到不同 pid → 取并集。"""
        reg = TaskRegistry()
        rec = reg.register(pid=999999, command="fake")

        import tools.tasks as T
        real = T.list_descendants
        calls = {"n": 0}

        def fake_list(pid):
            calls["n"] += 1
            return [1, 2] if calls["n"] == 1 else [3, 4]

        T.list_descendants = fake_list
        try:
            reg.note_descendants(rec)
            reg.note_descendants(rec)
        finally:
            T.list_descendants = real

        assert rec.descendants == [1, 2, 3, 4], f"应取并集: {rec.descendants}"

    def test_empty_result_keeps_existing(self):
        """一开始就是空、后面还是空 → 保持空(不报错)。"""
        reg = TaskRegistry()
        rec = reg.register(pid=999999, command="fake")

        import tools.tasks as T
        real = T.list_descendants
        T.list_descendants = lambda pid: []
        try:
            reg.note_descendants(rec)
            reg.note_descendants(rec)
        finally:
            T.list_descendants = real

        assert rec.descendants == []

    def test_exception_does_not_wipe_existing(self):
        """探查抛异常 → 不能把已有结果清空(原实现会清成 [])。"""
        reg = TaskRegistry()
        rec = reg.register(pid=999999, command="fake")
        rec.descendants = [7, 8]

        import tools.tasks as T
        real = T.list_descendants

        def boom(pid):
            raise RuntimeError("powershell 挂了")

        T.list_descendants = boom
        try:
            reg.note_descendants(rec)
        finally:
            T.list_descendants = real

        assert rec.descendants == [7, 8], (
            f"探查失败不该清空已有结果;实际: {rec.descendants}"
        )


# ============================================================
# 覆盖局限(如实记录的边界)
# ============================================================
class TestKnownLimits:
    """这些测试记录**已知的覆盖边界** —— 不是保证,是如实声明。

    背景(2026-10-16 实测):Windows 上 `cmd &` 启动的后台进程会被
    **过继**给系统里别的 bash,ppid 不再指向我们启动的 shell。
    实测:
        我们的 bash pid = 27072(已死)
        从它递归探查 → 0 个后代
        而 sleep 的 ppid = 28996(**不是** 27072)
    → 所以"纯后台命令留下的进程"**探不到**,报告也就不会出现。

    另两条路也实测排除了(见 docs/05-cross-platform/)：
      · 管道信号(communicate 是否 EOF)→ **无判别力**,正反例全判成 eof
      · 快照差集 → 能捞出后台进程,但**误报**(全系统新进程都进差集)
                   且每条命令 +1.05 秒
    """

    def test_lingering_only_reports_what_was_found(self):
        """报告只基于**实际探查到的** pid —— 探不到就不报(不编造)。"""
        from tools.tasks import format_lingering_report
        reg = TaskRegistry()
        rec = reg.register(pid=999999, command="sleep 30 & echo x")
        rec.descendants = []          # 模拟"探不到"(实测就是这样)
        assert format_lingering_report([rec]) == "", \
            "探不到后代时不该报残留(那会变成编造)"

    def test_returns_empty_not_none(self):
        """空结果返回空串而非 None —— 调用方用 `if text:` 判断。"""
        from tools.tasks import format_lingering_report
        assert format_lingering_report([]) == ""
        assert isinstance(format_lingering_report([]), str)
