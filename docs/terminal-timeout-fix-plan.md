# terminal 工具超时失效：诊断与修复方案

> 编写时间：2026-09-18
> 触发问题：dummy 执行 `find /c/Users /d/ -iname "*.vmx" -not -path "*/Windows/*" 2>/dev/null | head -20` 时卡死，未按预期超时，手动中止后无日志
> 状态：**待评审**（未改代码）
> **本文所有机制结论均经本机实测验证**（见附录 A 的验证脚本与输出）

---

## 一、问题复述

用户报告三个现象：

| # | 现象 |
|---|---|
| 1 | 执行 `find /c/Users /d/ ...` 时**直接卡死，没有触发 timeout** |
| 2 | （隐含）等待时间远超设定的 30 秒 |
| 3 | **手动中止后没有留下任何 log 信息** |

---

## 二、诊断过程与结论

### 2.1 第一步：确认命令本身的行为

**实测**（`timeout 60 bash -lc 'find /c/Users /d/ -iname "*.vmx" ...'`）：

```
real    1m0.053s      ← 跑满 60 秒仍未结束,被 timeout 命令强制杀掉
user    0m0.015s      ← CPU 几乎无消耗
sys     0m0.136s
退出码: 124
```

**结论**：这个命令**本质上就很慢**（扫描 `/c/Users` + `/d/` 两个盘的完整目录树），而且 `user`/`sys` 时间几乎为 0——说明时间全花在**等待文件系统 I/O**，不是在计算。

**另一个实测**：同一命令在文件系统缓存已热的情况下**只要 0.3 秒**。

**所以这个命令的耗时极不稳定**（0.3 秒 ~ 1 分钟以上），取决于磁盘缓存状态。

### 2.2 第二步：为什么 `timeout=30` 没有生效

**dummy 的实现**（`tools/terminal.py:68-92`）：

```python
def _run_shell(command: str, timeout: int = 30):
    bash = _find_bash()
    if bash:
        result = subprocess.run(
            [bash, "-lc", command],
            capture_output=True,
            text=False,
            timeout=timeout,
        )
        return result, "git-bash"
    ...
```

**实测**（`subprocess.run([bash, "-lc", "sleep 20"], timeout=5)`）：

```
设定 timeout = 5 秒
实际 TimeoutExpired 在 **20.4 秒**才触发
                        ↑ 多等了 15.4 秒（等于子进程自然结束的时间）
```

**机制解释**（逐步）：

```
1. subprocess.run(timeout=5) 内部用 Popen + communicate(timeout=5)
2. 5 秒后抛 TimeoutExpired
3. 但它**先**调用 Popen.kill()
4. Windows 上的 kill() = TerminateProcess(bash.exe)
   → **只杀 bash.exe 这一个进程**
5. 然后 subprocess.run **继续**调用 communicate() 等待收尾
6. communicate() 要读到 stdout/stderr 管道的 EOF 才返回
7. 但 bash 启动的**子进程（sleep / find）还活着**，它们继承了管道写端
8. → **必须等子进程自己结束**（或管道被关闭）
```

**所以"没有触发 timeout"是错觉** —— timeout 确实触发了，但**触发之后还要等子进程自然结束**。

**这解释了现象 1 和 2**：用户看到的是"卡死"，实际是"timeout 已触发但仍在等子进程"。

### 2.3 第三步：为什么手动中止后没有日志

**代码**（`tools/terminal.py:149-152`，已核对行号）：

```python
    except subprocess.TimeoutExpired:
        return f"[错误] 命令执行超时（30 秒上限）：{command}"
    except Exception as e:
        return f"[错误] 命令执行失败: {type(e).__name__}: {e}"
```

**问题的三个层面**：

| # | 问题 | 后果 |
|---|---|---|
| **a** | **异常还没抛出时，进程就被手动中止了** | 因为卡在 `subprocess.run()` 里面等 `communicate()`，`TimeoutExpired` 尚未抛出 → 历史里没有这条 tool 结果 |
| **b** | 即使抛出了，`TimeoutExpired.stdout` / `.stderr` 里的**部分输出被丢弃** | `return` 只用了固定文案，没取 `e.stdout` |
| **c** | **执行前不留痕** | 卡住期间磁盘上没有"我正在跑什么命令"的记录 |

**实际发生的过程**（用户场景）：

```
_run_shell 卡在 communicate() 等待中
    ↓ 用户按 Ctrl+C / 手动中止
KeyboardInterrupt 在这里抛出
    ↓
main.py 的 except KeyboardInterrupt → _settle_history → 释放锁 → 退出
    ↓
历史里没有这条 tool 结果(异常在生成结果之前)
    ↓
P2b 的 repair_tool_pairing 补占位 "[工具结果缺失:该次调用未执行或被中断]"
    ↓
★ 真实信息("卡在 find 命令上")完全丢失
```

### 2.4 第四步：子进程泄漏（额外发现）

**实测**（`Popen` + `communicate(timeout=5)` + 检查子进程存活）：

```
communicate 超时在 5.0s (设定 5s)          ← 这部分正常
结束后 sleep.exe 存活数: 1                  ← ⚠️ 子进程泄漏
```

**原因**：Windows 上
- `os.killpg` **不存在**（POSIX only）
- `start_new_session=True` 在 Windows 上**不是 setsid**（行为与 POSIX 不同）
- dummy **没有使用任何进程组管理**

**后果**：卡死的 `find` 会**一直跑下去**，持续占用磁盘 I/O。

### 2.5 诊断结论汇总

**三个现象对应三个独立缺陷**：

| 现象 | 缺陷 | 根因 |
|---|---|---|
| 卡死、不超时 | **timeout 不能精确中止** | `subprocess.run` 的 kill 只杀直接子进程，之后仍需等 communicate 收尾 |
| （等待时间远超 30 秒） | 同上 | 等的是子进程自然结束 |
| 无日志 | **卡死期间无留痕 + 部分输出被丢弃** | 异常未抛出时无任何记录；异常抛出后 `return` 没用 `e.stdout` |
| （额外） | **子进程泄漏** | 无进程组管理，`os.killpg` 在 Windows 不存在 |

---

## 三、修复方案的可行性验证（动手前完成）

**方案的核心是"用进程组 + `taskkill /T` 杀整棵树"。这必须实测确认可行**，否则方案是空话。

### 3.1 验证过程

**验证脚本**（用唯一标记精确定位自己的进程，避免其他 bash 会话干扰）：

```python
MARK = f"hermesmark{uuid.uuid4().hex[:8]}"
p = subprocess.Popen(
    [BASH, "-lc", f'echo {MARK}; for i in $(seq 1 300); do sleep 1; done'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    creationflags=CREATE_NEW_PROCESS_GROUP,     # 0x00000200
)
```

**验证输出**：

```
标记相关的进程(3 个):
  pid= 36508  ppid= 30600  bash.exe  ← Popen 返回的
  pid= 24100  ppid= 36508  bash.exe  ← git-bash 自己 fork 的第二层

communicate 超时于 3.0s

--- taskkill /F /T /PID 36508 ---
SUCCESS: The process with PID 24100 (child process of PID 36508) has been terminated.
SUCCESS: The process with PID 36508 (child process of PID 30600) has been terminated.

--- taskkill 后剩余的标记进程(1 个)---
  剩的是 powershell.exe(我自己的查询进程,正常)

★ taskkill 后 communicate 在 0.0s 内返回 ✓
```

### 3.2 验证结论

| 验证项 | 结果 |
|---|---|
| `CREATE_NEW_PROCESS_GROUP` 让 bash 独立成组 | ✅ 有效 |
| `taskkill /F /T /PID <bash_pid>` 连子进程一起杀 | ✅ **有效**（输出显示子 bash 也被杀） |
| 杀完后 `communicate()` 立即返回 | ✅ **0.0 秒** |
| 必须用 `Popen`（不能用 `subprocess.run`） | ✅ 确认——`subprocess.run` 内部的 kill 只杀直接子进程 |

**还有一个重要发现**：**git-bash 会多 fork 一层**：

```
Popen 启动的 bash (36508)
    └─ git-bash 又启动了一层 bash (24100)
         └─ 真正的命令进程
```

**所以"只杀 Popen 返回的 pid"是不够的** —— 必须用 `/T`（树）。

---

## 四、修复方案

### 4.1 改动清单

| # | 文件 | 位置 | 改动 |
|---|---|---|---|
| 1 | `tools/terminal.py` | `_run_shell()`（68-92 行） | 从 `subprocess.run` 改为 `Popen` + 进程组 + 超时后 `taskkill /T` |
| 2 | `tools/terminal.py` | 同上 | 超时时**取回部分输出**（`TimeoutExpired.stdout/.stderr`） |
| 3 | `tools/terminal.py` | `terminal_handler()` 的异常段（149-152 行） | 异常分支改为返回**部分输出 + 超时说明** |
| 4 | `tools/terminal.py` | `terminal_handler()` 执行前 | **执行前留痕**（记录"正在执行什么"） |
| 5 | `tools/terminal.py` | 新增 | 跨平台的"杀进程树"辅助函数 |
| 6 | `tools/terminal.py` | `_find_bash()`（47 行） | **加注释**：说明为什么必须返回绝对路径（见九节） |

### 4.2 改动 1+2：`_run_shell` 重写

**现状**：

```python
def _run_shell(command: str, timeout: int = 30):
    bash = _find_bash()
    if bash:
        result = subprocess.run(
            [bash, "-lc", command],
            capture_output=True,
            text=False,
            timeout=timeout,
        )
        return result, "git-bash"
    result = subprocess.run(
        command, shell=True, capture_output=True, text=False, timeout=timeout,
    )
    return result, "cmd" if os.name == "nt" else "sh"
```

**改为**（示意，实际实现需保持项目代码风格）：

```python
def _kill_tree(proc) -> None:
    """杀掉进程及其整个子进程树。

    ============ 为什么不能用 proc.kill()(2026-09-18 实测) ============
    实测:subprocess.run([bash, "-lc", "sleep 20"], timeout=5)
          设 5 秒,实际 20.4 秒才返回 —— 因为:
            1. kill() 只杀 bash.exe 本身(TerminateProcess)
            2. bash 启动的 sleep 还活着,持有 stdout 管道写端
            3. communicate() 要读到管道 EOF 才返回 → 只能等 sleep 自然结束

    另一个实测发现:git-bash 会多 fork 一层
          Popen 的 bash(36508) → 又一层 bash(24100) → 真正的命令
      所以必须用 /T(树)才能杀干净。

    实测验证(PID 36508,子进程 24100):
          taskkill /F /T /PID 36508
          → SUCCESS: 24100 (child) terminated
          → SUCCESS: 36508 terminated
          → communicate 在 **0.0 秒**内返回 ✓
    """
    if os.name == "nt":
        # Windows:taskkill /T 杀整棵树
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        # POSIX:杀进程组(依赖 start_new_session=True)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def _run_shell(command: str, timeout: int = 30):
    """执行命令:优先 git-bash,回退系统默认 shell。

    返回 (stdout_bytes, stderr_bytes, returncode, shell_name, timed_out)。

    ============ 为什么改用 Popen(2026-09-18) ============
    subprocess.run(timeout=N) 的超时不能精确中止(见 _kill_tree 注释)。
    改用 Popen 是为了能自己控制"超时 → 杀整棵树"这一步。
    """
    bash = _find_bash()
    if bash:
        argv = [bash, "-lc", command]
        shell_name = "git-bash"
    else:
        argv = command
        shell_name = "cmd" if os.name == "nt" else "sh"

    kwargs = dict(
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if os.name == "nt":
        # 独立进程组,便于整组终止
        kwargs["creationflags"] = 0x00000200   # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True      # POSIX: setsid

    if bash:
        proc = subprocess.Popen(argv, **kwargs)
    else:
        proc = subprocess.Popen(argv, shell=True, **kwargs)

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        timed_out = True
        # ★ 先取回已经产生的那部分输出(之前被丢弃)
        stdout = e.stdout or b""
        stderr = e.stderr or b""
        _kill_tree(proc)
        # 杀完树后,再收一次尾(此时管道会立即关闭,不会阻塞)
        try:
            rest_out, rest_err = proc.communicate(timeout=5)
            stdout = (stdout or b"") + (rest_out or b"")
            stderr = (stderr or b"") + (rest_err or b"")
        except subprocess.TimeoutExpired:
            pass       # 极端情况:不再等,用已有的部分输出

    return stdout, stderr, proc.returncode, shell_name, timed_out
```

**为什么要"杀完树后再 communicate 一次"**：`taskkill` 之后管道会立即关闭，所以这次 `communicate()` **不会阻塞**（实测 0.0 秒），能取到剩余输出。

### 4.3 改动 3：异常分支返回部分输出 + 超时说明

**现状**：

```python
except subprocess.TimeoutExpired:
    return f"[错误] 命令执行超时（30 秒上限）：{command}"
```

**改为**（在 `terminal_handler` 里，`_run_shell` 不再抛异常而是返回 `timed_out` 标志）：

```python
    stdout_b, stderr_b, rc, shell_name, timed_out = _run_shell(command)

    output_parts = [describe_environment(shell_name)]

    stdout = stdout_b.decode("utf-8", errors="replace").strip() if stdout_b else ""
    if stdout:
        output_parts.append(stdout)

    stderr = stderr_b.decode("utf-8", errors="replace").strip() if stderr_b else ""
    if stderr:
        output_parts.append(f"[STDERR]\n{stderr}")

    if timed_out:
        output_parts.append(
            f"[超时] 命令在 30 秒内没有结束，已被终止（连同它启动的子进程）。"
            f"上面是终止前已经产生的输出（可能为空）。"
            f"如果这是一条需要长时间运行的命令（例如在大目录树里搜索），"
            f"可以考虑缩小范围、或改用更快的查找方式。"
        )
        return "\n".join(output_parts)

    if rc != 0:
        output_parts.append(f"[EXIT CODE: {rc}]")

    return "\n".join(output_parts) if output_parts else "(命令执行成功，无输出)"
```

**关键设计**：
- **保留部分输出**（用户能看到它跑到哪一步了）
- **说明"连子进程一起终止了"**（事实）
- **给出下一步的可能性**（"缩小范围"）——**注意这是"提供选项"而不是"命令"**，符合项目原则

### 4.4 改动 4：执行前留痕

**问题**：卡死期间（`_run_shell` 还没返回）磁盘上没有任何记录。用户手动中止后，历史里只有一句占位文本。

**改法**：在**执行之前**先写一条"开始执行"的记录。两个可选方案：

**方案 A：写进历史（作为一个 tool 消息的占位）**

```python
# 在执行前,先 append 一条 tool 消息,内容为"正在执行"
self.history.append({
    "role": "tool", "tool_call_id": tool_call_id,
    "content": f"[执行中] 正在运行: {command}（上限 30 秒）",
})
self._persist_history()          # 立即落库

# 执行完成后,**替换**这条消息为真实结果
```

**优点**：手动中止后，磁盘上有"我正在跑这条命令"的记录
**缺点**：要改"追加后替换"的逻辑（现在是直接 append 结果）

**方案 B：只写日志文件，不进历史**

在 `_run_shell` 之前写一行到 `logs/` 下的执行日志。
**优点**：简单，不碰历史结构
**缺点**：模型看不到（但手动中止的场景下，模型也不需要看到）

**建议选 B**（改动小、不碰历史结构），**但需要你确认**——因为方案 A 的信息对模型也有价值（"上次我卡在这个命令上"）。

### 4.5 改动 5：跨平台杀进程树

见 4.2 的 `_kill_tree()`。**要保证的**：

| 平台 | 机制 | 依据 |
|---|---|---|
| Windows | `taskkill /F /T /PID` | **已实测有效** |
| POSIX | `os.killpg(os.getpgid(pid), SIGKILL)` | 依赖 `start_new_session=True`（标准做法） |

**POSIX 分支未在本机验证**（本机 WSL 不可用）——按项目惯例，**这一点要在代码注释里标注"未在真机验证"**，并加一条测试确认它至少不会崩。

---

## 五、影响评估

### 5.1 受影响的既有机制

| 机制 | 影响 | 说明 |
|---|---|---|
| `is_tool_error`（`lessons.py`） | ⚠️ **需确认** | 新返回值加了 `[超时]` 前缀。要确认它是否该算"非成功"（**应该算**，因为命令没跑完） |
| 工具护栏（`tool_guardrails.py`） | ⚠️ **可能受轻微影响** | 它比对"结果指纹"。超时输出现在是"部分输出 + 超时说明"，同一命令两次超时的输出可能不同 → 指纹不同 → 检测不到重复。**这是好事还是坏事需要判断** |
| `turn_context` 的执行记录 | ✅ 无影响 | 它只截取结果的一行 |
| 确认机制（`_confirm`） | ✅ 无影响 | 改动在确认之后 |
| 成本/统计 | ✅ 无影响 | —— |

### 5.2 需要判断的一个设计问题

**超时后的输出是否稳定？**

```
场景:同一个 find 命令连续跑 3 次都超时
每次超时时刻不同 → 截获的部分输出可能不同 → 结果指纹不同
    ↓
护栏的"完全相同失败"信号检测不到
    ↓
模型可能反复跑同一个慢命令
```

**两种处理**：

| 方案 | 效果 |
|---|---|
| **a. 让超时结果的指纹稳定**（例如指纹只用 `[超时]` 前缀 + 命令，不用部分输出） | 护栏能检测到"你在反复跑同一个超时命令" ✓ |
| **b. 保持现状**（每次都不同） | 护栏检测不到 |

**建议 a**——因为"反复跑同一个慢命令"恰恰是护栏该报的事实。

### 5.3 风险清单

| 风险 | 可能性 | 后果 | 缓解 |
|---|---|---|---|
| `taskkill` 在某种 Windows 版本上不可用 | 低 | 超时后无法清理 | 加 try/except 兜底退回 `proc.kill()` |
| 杀树时误杀无关进程 | **低但有** | 误杀用户其他程序 | `/T` 只杀指定 pid 的子树；但 git-bash 的多层 fork 可能让树结构复杂。**需测试确认** |
| 部分输出解码出错 | 低 | 输出乱码 | 已有 `errors="replace"` |
| 改动引入新 bug（重写执行路径） | **中** | terminal 工具不可用 | **分两步**：先改执行路径并跑全量测试，再改超时逻辑 |

**关于"误杀"这一项要说清**：`taskkill /T` 杀的是**指定 PID 的子树**。理论上不会误杀无关进程。但因为 git-bash 会多层 fork，**要验证"杀的树"确实只包含这次命令的进程**（验证脚本已经做过这件事：用唯一标记确认剩余进程只有我自己的查询进程）。

---

## 六、验证方法

### 6.1 单元测试

```python
def test_timeout_kills_process_tree():
    """超时后,进程树必须被完全杀掉(不留泄漏的子进程)。

    这修复的是实测发现的缺陷:
      subprocess.run(timeout=5) 在 git-bash 上实际 20.4 秒才返回,
      因为只杀了 bash,sleep 子进程还活着并持有管道。
    """
    # 用带唯一标记的命令启动
    # 断言:1) 在 timeout+2 秒内返回  2) 标记进程数为 0

def test_timeout_returns_partial_output():
    """超时必须返回已经产生的部分输出(不能丢弃)。

    实测:TimeoutExpired.stdout 里有部分输出,旧代码用固定文案 return,把它丢了。
    """
    # 命令:先 echo 一个标记,再 sleep 很久
    # 断言:返回值包含那个标记

def test_normal_command_unaffected():
    """正常命令的行为不能变(回归)。

    代码从 subprocess.run 改成 Popen + communicate,
    正常路径的输出/退出码/环境声明必须与之前一致。
    """
```

### 6.2 定点验证（用真实命令复现）

**用用户遇到的那条命令**：

```bash
find /c/Users /d/ -iname "*.vmx" -not -path "*/Windows/*" 2>/dev/null | head -20
```

**期望**：
1. **30 秒内返回**（不是几分钟）
2. 返回值含 `[超时]` 说明
3. **不留存活的 `find.exe` 进程**（杀完树后检查）
4. 返回值里有它超时前找到的部分结果（如果有）

**检查泄漏的命令**：

```bash
tasklist | grep -i find.exe     # 应为空
tasklist | grep -i sleep.exe    # 应为空
```

### 6.3 真实场景验证

重现用户的操作：

```
1. 启动 dummy
2. 让模型执行那个 find 命令
3. **在它卡住时等 30 秒**（不要手动中止）
4. 观察:
   - 是否在 ~30 秒内返回?
   - 返回内容是否包含 [超时] 说明?
   - dummy 是否继续正常工作(而不是卡死)?
5. 另测一次:在卡住时手动中止
   - 是否留下了"正在执行什么"的记录?(取决于 4.4 选 A 还是 B)
```

---

## 七、不在本方案范围内

| 事项 | 为什么不做 |
|---|---|
| 让 `find` 命令本身变快 | 那是模型该考虑的（它可以缩小范围）。本方案只保证"卡住时能中止并留下信息" |
| timeout 值可配置 | 可以后续加（`DUMMY_TERMINAL_TIMEOUT`）。**先修"中止不生效"这个核心缺陷** |
| 异步执行 + 轮询 | 改动量大（要动 chat 循环的等待模型）；**先看同步超时能否解决** |
| 输出流式回显 | 独立课题（实时显示长命令的输出） |

---

## 八、附录 A：验证脚本与关键输出

### A.1 验证 `subprocess.run` 的 timeout 不精确

```python
t0 = time.time()
try:
    subprocess.run([BASH, "-lc", "sleep 20"], capture_output=True, timeout=5)
except subprocess.TimeoutExpired:
    print(f"TimeoutExpired 在 {time.time()-t0:.1f}s")
```

**输出**：

```
TimeoutExpired 在 20.4s 触发
→ 设定 5s,实际 20.4s 才返回,多等了 15.4s
```

### A.2 验证 `taskkill /F /T` 有效

**启动**（带唯一标记）：

```python
MARK = f"hermesmark{uuid.uuid4().hex[:8]}"
p = subprocess.Popen(
    [BASH, "-lc", f'echo {MARK}; for i in $(seq 1 300); do sleep 1; done'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    creationflags=CREATE_NEW_PROCESS_GROUP,
)
```

**进程树**（用带标记的 CommandLine 精确查询）：

```
--- 标记相关的进程(3 个)---
  pid= 36508  ppid= 30600  bash.exe  ← Popen 返回的
  pid= 24100  ppid= 36508  bash.exe  ← git-bash 自己 fork 的第二层
  pid= 28320  ppid= 30600  powershell.exe  ← 查询进程本身
```

**杀树**：

```
--- taskkill /F /T /PID 36508 ---
SUCCESS: The process with PID 24100 (child process of PID 36508) has been terminated.
SUCCESS: The process with PID 36508 (child process of PID 30600) has been terminated.
```

**结果**：

```
--- taskkill 后剩余的标记进程(1 个)---
  剩的只有 powershell.exe(查询进程本身,零个 bash/命令进程)

★ taskkill 后 communicate 在 0.0s 内返回 ✓
```

### A.3 验证子进程泄漏

```python
p = subprocess.Popen([BASH, "-lc", "sleep 20"], stdout=subprocess.PIPE, ...)
try:
    p.communicate(timeout=5)
except subprocess.TimeoutExpired:
    pass
# 检查 sleep.exe 存活
```

**输出**：

```
communicate 超时在 5.0s
sleep.exe 存活数: 1        ← 泄漏!
```

---

## 九、附录 B：本机环境说明（影响结论适用范围）

| 项 | 值 |
|---|---|
| OS | Windows 10（中文版） |
| shell | `C:\Program Files\Git\bin\bash.exe`（git-bash） |
| Python | 3.11.3 |
| **注意** | `shutil.which("bash")` 返回的是 **Hermes 自带的 git**（`C:\Users\xlinz\AppData\Local\hermes\git\usr\bin\bash.EXE`），而执行 `bash -lc` 时**会被 WSL 拦截**（实测报 `WSL (25 - Relay) ERROR: execvpe(/bin/bash) failed`） |

**最后一条值得单独说明**：dummy 的 `_find_bash()` 返回**绝对路径**（`C:\Program Files\Git\bin\bash.exe`），所以**不会**被 WSL 拦截——**这是 dummy 做对的地方**。

但这条也提示：**如果将来有人把 `_find_bash()` 改成 `shutil.which("bash")`，会引入 WSL 干扰的 bug**。建议在 `_find_bash()` 处加注释说明这一点。
