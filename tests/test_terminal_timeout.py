"""tools/tasks.py + terminal 超时修复的测试。

============ 被这些问题坑过（2026-09-18 实测） ============
用户报告：dummy 执行

    find /c/Users /d/ -iname "*.vmx" -not -path "*/Windows/*" | head -20

时卡死、未按预期超时、手动中止后无日志。诊断出四个独立缺陷，
每一个都有实测数据（见下方各测试的注释）：

  ① subprocess.run(timeout=5) 在 git-bash 上实际 20.4 秒才返回
     —— 它只杀直接子进程，之后仍要等 communicate 收尾
  ② TimeoutExpired.stdout 是 None —— "保留部分输出"落空
  ③ 超时后再次 communicate() 不会重新读管道，而是继续阻塞
     —— 实测多等 5 秒（设 3 秒实际 8.8 秒）
  ④ p.stdout.read(4096) 会阻塞等填满缓冲区
     —— 必须用 read1（实测 read(4096) 读不到、read1(4096) 能读到）
"""
import os
import sys
import time
import subprocess
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.terminal import terminal_handler, DEFAULT_TIMEOUT, MAX_TIMEOUT   # noqa: E402
from tools.tasks import (                                                   # noqa: E402
    TaskRegistry, TaskRecord, kill_process_tree, list_descendants, is_alive,
)


# ============================================================
# 常量
# ============================================================
def test_timeout_constants():
    """默认 120 秒、上限 600 秒 —— 对齐 Claude Code(120/600) 与 Hermes(180/600)。"""
    assert DEFAULT_TIMEOUT == 120
    assert MAX_TIMEOUT == 600
    assert MAX_TIMEOUT > DEFAULT_TIMEOUT


# ============================================================
# 超时的核心行为（缺陷 ①②③）
# ============================================================
@pytest.mark.skipif(sys.platform != "win32", reason="本组结论在 Windows 上实测得出")
def test_timeout_aborts_precisely():
    """超时必须**精确**中止（缺陷 ① 的回归）。

    旧实现 subprocess.run(timeout=N) 实测：设 5 秒、实际 20.4 秒才返回，
    因为被杀的 bash 的 sleep 子进程还活着、攥着 stdout 管道，
    communicate() 要等管道 EOF 就只能干等。
    """
    t0 = time.time()
    out = terminal_handler("sleep 20", timeout=3)
    elapsed = time.time() - t0
    assert elapsed < 8, f"超时不精确：设 3 秒实际 {elapsed:.1f} 秒"
    assert "[超时]" in out


@pytest.mark.skipif(sys.platform != "win32", reason="同上")
def test_timeout_keeps_partial_output():
    """超时必须保留超时前已经产生的输出（缺陷 ② 的回归）。

    旧实现用固定文案 return，把 TimeoutExpired.stdout 里的部分输出丢了。
    新实现自开线程边跑边读，超时时已经在缓冲里。
    """
    marker = f"partial{uuid.uuid4().hex[:6]}"
    out = terminal_handler(f"echo {marker}; sleep 20", timeout=3)
    assert marker in out, f"部分输出丢失；实际返回：{out[:300]}"
    assert "[超时]" in out


@pytest.mark.skipif(sys.platform != "win32", reason="同上")
def test_timeout_no_extra_wait():
    """超时后不能有额外的 5 秒等待（缺陷 ③ 的回归）。

    实测过：超时后再调 communicate() 不会重新读管道、而是继续阻塞，
    导致设 3 秒实际 8.8 秒（多出的正好是那个 5 秒超时）。
    """
    t0 = time.time()
    terminal_handler("sleep 20", timeout=3)
    elapsed = time.time() - t0
    assert elapsed < 7, f"超时后多等了 {elapsed - 3:.1f} 秒"


# ============================================================
# 超时参数
# ============================================================
def test_timeout_param_accepts_valid_value():
    """正常值被接受（用极短的值验证，避免测试变慢）。"""
    out = terminal_handler("echo ok", timeout=5)
    assert "ok" in out
    assert "[超时]" not in out


@pytest.mark.parametrize("bad", [99999, -5, 0, "abc", None, 3.7])
def test_timeout_param_never_crashes(bad):
    """非法/越界的 timeout 值不能导致崩溃（夹到 [1, MAX_TIMEOUT] 或回落默认）。"""
    out = terminal_handler("echo safe", timeout=bad)
    assert "safe" in out or "[错误]" not in out


# ============================================================
# 正常命令回归（改执行路径不能破坏原有行为）
# ============================================================
def test_normal_command_unchanged():
    """正常命令：输出、环境声明、无超时标记。"""
    out = terminal_handler("echo hello_world")
    assert "hello_world" in out
    assert "[超时]" not in out
    assert "[SHELL:" in out          # 环境声明还在


def test_exit_code_reported():
    """非零退出码仍然被报告（下游 is_tool_error 依赖它）。"""
    out = terminal_handler("exit 3")
    assert "[EXIT CODE: 3]" in out


def test_stderr_captured():
    out = terminal_handler("echo to_err >&2")
    assert "[STDERR]" in out
    assert "to_err" in out


# ============================================================
# 进程表
# ============================================================
def test_registry_records_and_finishes():
    """登记表能记录任务并标记状态。"""
    reg = TaskRegistry()
    rec = reg.register(pid=12345, command="echo test")
    assert rec.command == "echo test"
    assert rec.status == "running"
    reg.finish(rec, timed_out=False)
    assert rec.status == "done"


def test_registry_marks_killed_on_timeout():
    reg = TaskRegistry()
    rec = reg.register(pid=12345, command="sleep 100")
    reg.finish(rec, timed_out=True)
    assert rec.status == "killed"


def test_registry_lingering_detects_alive_process():
    """lingering() 只返回"还有活着的进程"的记录。

    这正是要报告给模型的信息 —— 模型据此判断要不要处理。
    """
    reg = TaskRegistry()
    # 造一个真实存活的长进程
    p = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        rec = reg.register(pid=p.pid, command="python sleep")
        time.sleep(0.5)
        assert rec.pid in [r.pid for r in reg.lingering()], "存活进程应被 lingering 检出"
    finally:
        p.kill()
        p.wait()


def test_registry_lingering_ignores_dead():
    """进程已死 → 不在 lingering 里。"""
    reg = TaskRegistry()
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    reg.register(pid=p.pid, command="python pass")
    assert reg.lingering() == []


def test_task_record_elapsed():
    rec = TaskRecord(pid=1, command="x", started_at=time.time() - 2)
    assert rec.elapsed() >= 2


# ============================================================
# 进程树操作
# ============================================================
def test_list_descendants_of_self():
    """列后代：自己启动的子进程应能被找到。"""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        time.sleep(0.6)
        kids = list_descendants(os.getpid())
        assert p.pid in kids, f"未找到子进程 {p.pid}；找到 {kids}"
    finally:
        p.kill()
        p.wait()


def test_is_alive_true_for_running():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        assert is_alive(p.pid) is True
    finally:
        p.kill()
        p.wait()


def test_is_alive_false_for_dead():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    time.sleep(0.3)
    assert is_alive(p.pid) is False


def test_kill_process_tree_invalid_pid():
    """非法 pid 不崩，返回 (False, 说明)。"""
    ok, msg = kill_process_tree(0)
    assert ok is False
    assert msg


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专用路径")
def test_kill_process_tree_really_kills():
    """杀整棵树：根与子进程都要死（这是"不留野进程"的基础）。"""
    p = subprocess.Popen(
        [sys.executable, "-c", "import subprocess,sys,time; "
                               "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
                               "time.sleep(60)"],
    )
    try:
        time.sleep(1.2)
        kids = list_descendants(p.pid)
        ok, msg = kill_process_tree(p.pid)
        time.sleep(1.0)
        assert ok, f"杀树失败: {msg}"
        assert not is_alive(p.pid), "根进程未死"
        for k in kids:
            assert not is_alive(k), f"子进程 {k} 未死"
    finally:
        for k in list_descendants(p.pid):
            kill_process_tree(k)
        kill_process_tree(p.pid)
