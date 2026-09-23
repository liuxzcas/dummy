"""tools/tasks.py — dummy 启动过的进程登记表(内存版)。

============ 为什么需要这个 ============
2026-09-18 用户实测:dummy 执行

    find /c/Users /d/ -iname "*.vmx" -not -path "*/Windows/*" | head -20

时卡死,手动中止后**没有任何日志**。诊断发现三个独立缺陷:

  ① 超时不能真正中止 —— subprocess.run(timeout=N) 内部只杀直接子进程,
     实测设 5 秒实际 20.4 秒才返回(因为 bash 启动的 sleep 还活着,
     攥着 stdout 管道,communicate() 要等管道 EOF)。
  ② 已产生的部分输出被丢弃(TimeoutExpired.stdout 里有,旧代码用固定文案 return)。
  ③ 卡死期间磁盘上没有任何"我在跑什么"的记录。

本模块解决 ②③ 所需的"事实来源",并给上层的"报告给 LLM"提供数据。

============ 分层原则(用户 2026-09-18 定) ============
用户把这件事拆成了两种性质不同的动作:

  情形一【机制】:超时后的清理
      超时 → 杀整棵树 → 这是 script 里的固定下一步,不需要问 LLM。
      理由:不杀就泄漏,是机制性问题(类比 TCP 连接超时关闭)。
      → 由 terminal.py 自己做,本模块只负责登记与注销。

  情形二【信息】:"我留下了什么进程" 这个事实
      登记表 → 报告给 LLM → LLM 判断要不要处理。
      理由:"要不要杀掉残留进程"是语义判断(用户可能故意启动了服务)。
      → 本模块负责记录,terminal.py 负责报告。

============ 为什么是"表"不是"树" ============
dummy 的进程关系是**扁平**的 —— 一次工具调用产生一棵进程树,
各次调用之间没有父子关系。所以:

    需要记录的是: "这次调用启动了哪些进程"
    不需要表达的是:"进程之间的父子关系"

真正需要"树"的场景是【子 Agent 委派】(Agent A 启动 Agent B),
那是 roadmap 里 Phase 4 的事。届时把 TaskRecord 扩展成带 parent 字段即可。

============ 内存版(用户 2026-09-18 定) ============
表只存内存,不落盘。理由:
  · 覆盖"Ctrl+C 退出时清理"这个实际会遇到的场景 ✓
  · 覆盖"dummy 被强杀后的残留"需要落盘 + 处理 pid 复用,风险收益比不高
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field


# ============================================================
# 进程树操作(跨平台)
# ============================================================

def kill_process_tree(pid: int) -> tuple[bool, str]:
    """杀掉指定进程及其整个子进程树。返回 (是否成功, 说明)。

    ============ 为什么不能只调 proc.kill()(2026-09-18 实测) ============
    实测:subprocess.run([bash, "-lc", "sleep 20"], timeout=5)
          设 5 秒,实际 20.4 秒才返回。原因:
            1. kill() 只杀 bash(TerminateProcess)
            2. bash 启动的 sleep 还活着,持有 stdout 管道写端
            3. communicate() 要读到管道 EOF 才返回 → 只能等 sleep 自然结束

    另一个实测发现:**git-bash 会多 fork 一层**
          Popen 的 bash(36508) → 又一层 bash(24100) → 真正的命令
      所以必须用 /T(树)才能杀干净。实测:
          taskkill /F /T /PID 36508
          → SUCCESS: 24100 (child) terminated
          → SUCCESS: 36508 terminated
          → communicate 在 0.0 秒内返回 ✓

    ============ 平台差异(实测) ============
    Windows:
        有 taskkill /T,但**依赖父子关系链完整** —— 树根已死就失效
        os.killpg 不存在(AttributeError) —— Windows 没有 POSIX 的进程组语义
        CREATE_NEW_PROCESS_GROUP 只是一个标志,不提供 killpg 语义
    POSIX:
        有 os.killpg(配合 start_new_session=True 使用)
        但同样有失效场景:
          · 命令用 setsid / nohup 换了进程组 → killpg 找不到
          · 进程忽略 SIGTERM → 要升级到 SIGKILL
          · 进程处于不可中断睡眠(D 状态) → 连 SIGKILL 都杀不掉
        (containerd issue #4594 就是"只杀根 PID 导致子进程变孤儿"的实证)
    """
    if pid <= 0:
        return False, "pid 非法"

    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=15,
            )
            ok = r.returncode == 0
            return ok, (r.stdout or r.stderr or "").strip()[:200]
        except Exception as e:
            return False, f"taskkill 调用失败: {type(e).__name__}: {e}"

    # POSIX
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True, "已发送 SIGKILL 给进程组"
    except ProcessLookupError:
        return False, "进程已不存在"
    except (PermissionError, OSError) as e:
        # 兜底:进程组杀不了时,直接杀进程本身
        try:
            os.kill(pid, signal.SIGKILL)
            return True, "已发送 SIGKILL 给进程本身(进程组不可用)"
        except Exception as e2:
            return False, f"{type(e).__name__}: {e}; 兜底也失败: {e2}"


def list_descendants(root_pid: int) -> list[int]:
    """列出某进程的全部后代 pid(递归)。返回不含 root_pid 本身。

    用途:在树根被强杀后,仍然能按这份名单逐个清理。
    实测:Windows 上用 Get-CimInstance 查 ParentProcessId 递归即可。
    """
    if os.name == "nt":
        # 一次拿到全部进程,在 Python 里建索引 —— 比递归调 PowerShell 快得多
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)\" }"],
                capture_output=True, text=True, timeout=20,
            )
            pairs = []
            for line in r.stdout.strip().splitlines():
                p = line.strip().split("|")
                if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
                    pairs.append((int(p[0]), int(p[1])))
        except Exception:
            return []

        children: dict[int, list[int]] = {}
        for child, parent in pairs:
            children.setdefault(parent, []).append(child)

        out: list[int] = []
        stack = [root_pid]
        seen = {root_pid}
        while stack:
            cur = stack.pop()
            for kid in children.get(cur, []):
                if kid not in seen:
                    seen.add(kid)
                    out.append(kid)
                    stack.append(kid)
        return out

    # POSIX:用 ps 列出 ppid
    try:
        r = subprocess.run(["ps", "-eo", "pid,ppid"], capture_output=True, text=True, timeout=10)
        pairs = []
        for line in r.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                pairs.append((int(parts[0]), int(parts[1])))
    except Exception:
        return []

    children: dict[int, list[int]] = {}
    for child, parent in pairs:
        children.setdefault(parent, []).append(child)
    out, stack, seen = [], [root_pid], {root_pid}
    while stack:
        cur = stack.pop()
        for kid in children.get(cur, []):
            if kid not in seen:
                seen.add(kid)
                out.append(kid)
                stack.append(kid)
    return out


def is_alive(pid: int) -> bool:
    """进程是否还在运行。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


# ============================================================
# 登记表
# ============================================================

@dataclass
class TaskRecord:
    """一次工具调用启动的进程记录。

    字段设计说明:
      pid          根进程 pid(通常是 bash)。杀树时用 /T 以它为根。
      command      命令原文。报告给 LLM 时要让它知道"这是哪条命令留下的"。
      started_at   启动时间(用于算运行了多久)。
      status       运行状态 —— 报告时要说清"还在跑"还是"已结束"。
      descendants  启动后探查到的后代 pid。**这是兜底名单**:
                   如果根进程被强杀(超时/异常),树链断了,
                   /T 就找不到它们了 —— 这时按这份名单逐个杀。
    """
    pid: int
    command: str
    started_at: float
    status: str = "running"          # running | done | killed
    descendants: list[int] = field(default_factory=list)

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def still_running(self) -> list[int]:
        """返回**当前还活着**的进程(含根 + 后代)。"""
        alive = []
        if self.status == "running" and is_alive(self.pid):
            alive.append(self.pid)
        for d in self.descendants:
            if is_alive(d):
                alive.append(d)
        return alive


class TaskRegistry:
    """进程登记表(内存版)。

    只做三件事:登记 / 注销 / 报告事实。**不做判决** ——
    "要不要杀残留进程"是模型的事(见模块头的分层原则)。
    """

    def __init__(self) -> None:
        self._tasks: list[TaskRecord] = []

    # ---------- 登记 ----------
    def register(self, pid: int, command: str) -> TaskRecord:
        """记录一个刚启动的进程。"""
        rec = TaskRecord(pid=pid, command=command, started_at=time.time())
        self._tasks.append(rec)
        return rec

    def note_descendants(self, rec: TaskRecord) -> None:
        """探查并记下后代 pid(在命令执行完/超时后调用)。

        为什么要在"之后"探查:进程刚启动时子进程可能还没 fork 出来。
        """
        try:
            rec.descendants = list_descendants(rec.pid)
        except Exception:
            rec.descendants = []

    def finish(self, rec: TaskRecord, timed_out: bool) -> None:
        """标记一条记录的状态。"""
        rec.status = "killed" if timed_out else "done"

    # ---------- 查询 ----------
    def lingering(self) -> list[TaskRecord]:
        """返回**还有活着的进程**的记录(用于报告给 LLM)。

        注意:一条记录可能"命令已结束"但"进程还活着"
              —— 例如命令启动了后台服务后正常退出。
              这种情况正是要报告给模型让它决定的。
        """
        out = []
        for rec in self._tasks:
            if rec.still_running():
                out.append(rec)
        return out

    def all_tasks(self) -> list[TaskRecord]:
        return list(self._tasks)

    # ---------- 清理 ----------
    def kill_lingering(self) -> int:
        """杀掉所有还活着的进程。返回杀掉的进程数。

        用途:退出时清理(用户 Ctrl+C / 正常退出)。
        —— 这是"机制"层面的事,不需要问 LLM。
        """
        killed = 0
        for rec in self._tasks:
            for pid in rec.still_running():
                ok, _ = kill_process_tree(pid)
                if ok:
                    killed += 1
        return killed

    def clear(self) -> None:
        self._tasks.clear()


# ============================================================
# 全局单例(与 ui 一致的用法)
# ============================================================
tasks = TaskRegistry()
