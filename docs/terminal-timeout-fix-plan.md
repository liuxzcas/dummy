# terminal 工具超时失效：诊断与修复记录

> 编写时间：2026-09-18（诊断与初版方案）
> **实施完成：2026-09-18，提交 `b28d647`**
> 触发问题：dummy 执行 `find /c/Users /d/ -iname "*.vmx" -not -path "*/Windows/*" 2>/dev/null | head -20` 时卡死，未按预期超时，手动中止后无日志
> **本文所有机制结论均经本机实测验证**（见附录 A 的全部实验）

---

## 关于本文档的性质（重要）

**本文档的前半部分（第一~三节）是"诊断阶段"的真实记录** —— 那时还没写代码，结论来自实测。

**第四节"修复方案"原本是动手前的设计**，但**实施过程中发现该设计有错**，实际实现与它不同。为尊重事实，第四节已改写为：

```
原设计（当时认为对的）  ↔  实施中实测发现的真相  ↔  最终实现
```

**被推翻的原设计有三处**（详见 4.0 节）：

| # | 原设计 | 为什么错 |
|---|---|---|
| 1 | 超时时读 `TimeoutExpired.stdout` 取部分输出 | **实测它是 `None`** —— 输出还在管道里没读出来 |
| 2 | 杀树后再调一次 `communicate()` 收尾 | **实测它不会重新读管道**，而是继续阻塞（多等 5 秒） |
| 3 | 用 `p.stdout.read(4096)` 读管道 | **实测会阻塞等填满缓冲区** —— 必须用 `read1` |

**另外实施中还发现两个原设计完全没预料到的问题**（详见第五节 5.4）：
- git-bash 会**多层 fork**（Popen 的 bash 下面还有一层）
- 杀树后 `close()` 管道会**死锁 27 秒**（读线程持锁）

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

## 四、修复方案（已按实际实施改写）

### 4.0 原设计 vs 实施真相 vs 最终实现

**这一节是本文档最重要的部分** —— 它记录了"动手前认为对的"与"实际是对的"之间的差距。

#### 分歧 1：部分输出怎么取？

| | 内容 |
|---|---|
| **原设计** | 超时时读 `TimeoutExpired.stdout` / `.stderr`，它们"里有部分输出" |
| **实测真相** | **它们都是 `None`** |

实测（Python 3.11.3 / Windows）：

```python
p = subprocess.Popen([BASH, "-lc", "echo HELLO_PARTIAL; sleep 30"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    p.communicate(timeout=3)
except subprocess.TimeoutExpired as e:
    print(e.stdout)   # → None
    print(e.stderr)   # → None
    print(e.output)   # → None
```

**为什么**：`communicate()` 用**内部线程**读管道；超时发生时，那个线程还没把数据交回来，**异常对象里就是空的**。

**原设计的错误性质**：**把"应该有"当成了"有"** —— 我根据 `TimeoutExpired` 有这个属性就假定它被填充了，**没有实测**。

#### 分歧 2：超时后怎么收尾？

| | 内容 |
|---|---|
| **原设计** | 杀树后再调一次 `communicate(timeout=5)`，说"管道已关闭，这次不会阻塞（实测 0.0 秒）" |
| **实测真相** | **它不会重新读管道，而是继续阻塞** |

实测：

```
① communicate 超时:        3.00s
   e.stdout = None
② 杀树:                    0.11s
③ 二次 communicate:        5.00s  ← 【又超时】!
   此时管道仍未关闭
总耗时: 8.12s
```

**原设计里那句"实测 0.0 秒"是错的** —— 那是从另一个实验（用 `taskkill` 杀一个**单独**的 bash）得来的，与"Popen + communicate 超时"这个场景**不是同一回事**。**我把两个不同场景的结论混用了。**

#### 分歧 3：怎么读管道？

| | 内容 |
|---|---|
| **原设计** | 没提读法（因为原设计依赖 `communicate`，不自己读） |
| **实施中的弯路** | 改成自开线程读之后，**第一次用的是 `read(4096)`，读不到任何数据** |
| **实测真相** | `read(n)` 阻塞等填满 n 字节；**必须用 `read1(n)`** |

实测（同样的命令，只换读法）：

```
p.stdout.read(1)      → 4 秒内读到 b'HELLO\n'   ✅
p.stdout.read(4096)   → 4 秒内读到 b''          ❌ 阻塞等填满缓冲区
p.stdout.read1(4096)  → 4 秒内读到 b'WORLD\n'   ✅ 有多少读多少
```

**这条差一点让整个方案被误判为"不可行"** —— 我当时以为是"Windows 管道读不出来"，实际是**读法错了**。

#### 分歧 4（额外发现）：收尾方式

**这个问题原设计完全没考虑**（因为原设计用的是 `communicate`，不需要自己收尾）。

改成自开线程后，必须决定"主线程要不要等读线程"。实测三种方式：

| 方式 | 结果 |
|---|---|
| `t.join(timeout=2)` | ❌ 卡满 2 秒（读线程阻塞在 `read1` 上不退） |
| 先 `p.stdout.close()` 再 join | ❌ **更糟**：`close()` 要等读线程放开锁，**实测阻塞 27.38 秒**（死锁） |
| **`sleep(0.3)` 后直接返回** | ✅ **3.44 秒完成，输出完整** |

**最终采用第三种**：读线程设 `daemon=True`，主线程给 0.3 秒让缓冲数据取完就返回。

#### 分歧 5（额外发现）：git-bash 多层 fork

原设计假设"Popen 启动的 bash 就是树根"。实测：

```
Popen 的 bash (36508)
    └─ git-bash 又启动了一层 bash (24100)      ← 原设计不知道这一层
         └─ 真正的命令进程
```

**影响**：只杀 Popen 的 pid 不够，必须 `/T`（树）。**这一条原设计"蒙对了"**（我用了 `/T`），但原因是"直觉上更保险"，**不是**因为知道有多层。

---

### 4.1 改动清单（实际实施）

| # | 文件 | 改动 | 与原设计 |
|---|---|---|---|
| 1 | `tools/terminal.py` | `_run_shell` 重写：`Popen` + **自开 daemon 线程读管道（用 `read1`）** + 主线程计时 + 超时杀树 | ⚠️ **与原设计不同**（原设计用 `communicate` + 二次 `communicate`） |
| 2 | `tools/terminal.py` | 超时返回**部分输出 + 超时说明** | ✅ 同原设计（但取输出的方式不同） |
| 3 | `tools/tasks.py` | **新增**：进程登记表（内存版）+ 跨平台 `kill_process_tree` / `list_descendants` / `is_alive` | ⚠️ **原设计没有这一节**（这是实施中按用户要求补的） |
| 4 | `tools/terminal.py` | `timeout` 参数：默认 120 / 上限 600 | ⚠️ 原设计写"建议后续加"，实施时直接加了 |
| 5 | `tools/__init__.py` | 工具 schema 加 `timeout` 参数 + 改描述 | ⚠️ 原设计没提 |
| 6 | `tests/test_terminal_timeout.py` | **新增 24 条** | ⚠️ 原设计只列了 3 条测试草案 |

**关于 `timeout` 默认值的依据**（原设计没给，实施时调研得到）：

| 系统 | 默认 | 上限 |
|---|---|---|
| Claude Code | 120 秒（`BASH_DEFAULT_TIMEOUT_MS`） | 600 秒 |
| Hermes | 180 秒（`TERMINAL_TIMEOUT`） | 600 秒（`FOREGROUND_MAX_TIMEOUT`） |
| **dummy（原）** | **30 秒** | 无 |
| **dummy（改后）** | **120 秒** | **600 秒** |

**原 dummy 的 30 秒是主流默认值的 1/4 ~ 1/6** —— 合理的长任务（构建/安装）会被误杀。

### 4.2 `_run_shell` 的最终实现（关键部分）

```python
def _run_shell(command: str, timeout: int = DEFAULT_TIMEOUT, task=None):
    # ... 构造 argv / Popen(creationflags=CREATE_NEW_PROCESS_GROUP) ...

    # ---- 启动读线程(必须在 wait 之前,否则管道写满会死锁) ----
    buf = {"out": b"", "err": b""}

    def _reader(stream, key):
        try:
            while True:
                chunk = stream.read1(4096)     # ★ read1:不等填满缓冲区
                if not chunk:
                    break
                buf[key] += chunk
        except Exception:
            pass

    # daemon=True:读线程卡在 read1 阻塞时,不阻止进程退出
    t_out = threading.Thread(target=_reader, args=(proc.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()

    # ---- 主线程计时 ----
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc.pid)          # 杀整棵树
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()                      # 兜底

    time.sleep(0.3)      # 给读线程取完缓冲里剩的数据(不 join,见 4.0 分歧 4)
    return buf["out"], buf["err"], proc.returncode, shell_name, timed_out
```

**三个关键点**（都是实测换来的）：

| 点 | 为什么 |
|---|---|
| 用 `read1(4096)` 而不是 `read(4096)` | `read` 会阻塞等填满缓冲区（分歧 3） |
| 读线程必须在 `wait` **之前**启动 | 否则命令输出填满管道缓冲区后会死锁 |
| 收尾用 `sleep(0.3)` 而不是 `join` | `join` 卡满、`close()+join` 死锁 27 秒（分歧 4） |

### 4.3 `tools/tasks.py`（原设计没有，实施中补的）

**用户的要求**："给 terminal 工具增加一个树来存储它启动的 tasks"

**实施时的判断**：**是"表"，不是"树"**。理由：

```
dummy 的进程关系是【扁平】的 —— 一次工具调用产生一棵进程树,
各次调用之间没有父子关系。

需要记录的是:  "这次调用启动了哪些进程"
不需要表达的是:"进程之间的父子关系"

真正需要"树"的场景是【子 Agent 委派】(Agent A 启动 Agent B),
那是 roadmap 里 Phase 4 的事。届时给 TaskRecord 加 parent 字段即可。
```

**分层原则**（用户 2026-09-18 定的）：

```python
情形一【机制】: 超时后的清理
    超时 → 杀整棵树 → 这是 script 里的固定下一步,不需要问 LLM
    理由:不杀就泄漏,是机制性问题(类比 TCP 连接超时关闭)

情形二【信息】: "我留下了什么进程" 这个事实
    登记表 → 报告给 LLM → LLM 判断要不要处理
    理由:"要不要杀掉残留进程"是语义判断(用户可能故意启动了服务)
```

**数据结构**：

```python
@dataclass
class TaskRecord:
    pid: int                    # 根进程 pid(杀树时以它为根)
    command: str                # 命令原文(报告时让模型知道是哪条命令)
    started_at: float
    status: str                 # running | done | killed
    descendants: list[int]      # 后代 pid —— 兜底名单
```

**为什么记 `descendants`**：`taskkill /T` **依赖父子关系链完整**，树根一死就失效。记下后代 pid 是为了"树根已死时逐个清理"这个兜底路径。

**用户决定：内存版**（不落盘）。理由：覆盖"Ctrl+C 退出时清理"这个实际会遇到的场景；覆盖"dummy 被强杀后的残留"需要落盘 + 处理 pid 复用，风险收益比不高。

### 4.4 其他改动

- **`terminal_handler` 的异常段**：不再有 `except subprocess.TimeoutExpired` 分支（`_run_shell` 改为返回 `timed_out` 标志而不抛异常）；超时返回带 `[超时]` 说明
- **`_find_bash()` 加注释**：说明为什么必须返回绝对路径（见第九节）
- **原设计的"改动 4：执行前留痕"未实施** —— 因为登记表在执行前 `register()`，已经起到了"留痕"的作用（表里会有记录）。**但表是内存的，进程被强杀时表也没了** —— 这一点**与原设计的意图（磁盘留痕）不同**，如实记录。

### 4.5 原设计的实现草案（保留作历史记录）

> **警告**：下面这段是**动手前写的草案**，**已被实际实现取代**（见 4.2）。
> 保留它是因为：它记录了"当时认为对的做法"，与最终实现对照能看清差在哪。
> **不要照它写代码** —— 它有下文标注的三个错误。

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

**为什么"杀完树后再 communicate 一次"这条被推翻了**（2026-09-18 实测）：

原设计的理由写的是"`taskkill` 之后管道会立即关闭，所以这次 `communicate()` **不会阻塞**（实测 0.0 秒）"。

**这个"实测 0.0 秒"来自另一个场景** —— 当时测的是"用 `taskkill` 杀掉一个**单独**的 bash 进程后，`communicate()` 能立即返回"。

**但与本场景不是同一回事**：

```
场景 A(当初测的): 手动 taskkill 一个 bash → 管道关闭 → communicate 立即返回 ✅
场景 B(实际遇到的): Popen + communicate 已经超时 → 再 communicate → 【继续阻塞 5 秒】❌
```

**原因**：`communicate()` 一旦超时，它的内部状态就已经乱了（读线程被放弃但管道对象未重置），**再次调用不会重新读**，而是继续阻塞。

**实测数据**：

```
① communicate 超时:        3.00s
② 杀树:                    0.11s
③ 二次 communicate:        5.00s  ← 又超时
总耗时: 8.12s（设定 3 秒）
```

**教训**：**从一个场景得出的"实测结论"，不能直接套用到另一个场景** —— 尤其当两者的调用路径不同时。我当初把场景 A 的结论写进了场景 B 的设计里。

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

---

## 十、实施结果（2026-09-18，提交 `b28d647`）

> 本节是**动手之后**的记录。前面第一~九节是诊断与设计阶段的产物。

### 10.1 最终改动的文件

| 文件 | 改动 | 行数变化 |
|---|---|---|
| `tools/terminal.py` | `_run_shell` 重写（Popen + 自开线程 + 杀树）+ `timeout` 参数 + 超时返回说明 | 264 行（改前 194 行） |
| `tools/tasks.py` | **新增**（进程登记表 + 跨平台进程树操作） | 313 行 |
| `tools/__init__.py` | terminal 的 schema 加 `timeout` 参数 + 改描述 | +5 行 |
| `tests/test_terminal_timeout.py` | **新增 24 条测试** | 240 行 |
| `docs/terminal-timeout-fix-plan.md` | 本文档 | — |
| `docs/terminal-timeout-explained-simply.md` | 通俗版（工人/水管/小工） | 415 行 |

### 10.2 端到端实测结果

| 验证项 | 结果 |
|---|---|
| **超时精确中止** | 设定 3 秒 → 实际 **4.1 秒** ✅（原实现：设定 5 秒实际 20.4 秒） |
| **保留部分输出** | `echo <标记>; sleep 20` 超时后，标记出现在返回值里 ✅ |
| **正常命令回归** | `echo` / 退出码 / stderr 全部与改动前一致 ✅ |
| **`timeout` 参数边界** | 99999→夹到600、0/-5→夹到1、`"abc"`/None→回落默认，均不崩 ✅ |
| **无野进程** | 含唯一标记的残留进程数 = 0 ✅ |
| **真实场景** | 用户那条 `find` 命令，设 10 秒 → **11.2 秒**准时中止并带 `[超时]` 说明 ✅ |
| **登记表** | 执行前登记、结束后标记状态、`lingering()` 正确识别存活进程 ✅ |

**关于"11.2 秒 > 10 秒"**：多出的 1.2 秒是杀树（0.12 秒）+ 给读线程收尾的 0.3 秒 + 进程启动开销。**这是设计内的**（4.0 分歧 4 的收尾方式）。

### 10.3 测试

```
tests/test_terminal_timeout.py   新增 24 条
全套                            278 passed / 1 xfailed
改前基线                        254 passed / 1 xfailed
```

**24 条测试覆盖**：
- 缺陷 ①②③ 的回归（超时精确性、部分输出、无额外等待）
- `timeout` 参数的合法值、非法值、越界值
- 正常命令回归（输出/退出码/stderr/环境声明）
- 登记表（登记/状态/`lingering`/`elapsed`）
- 进程树操作（`list_descendants`/`is_alive`/`kill_process_tree`）

### 10.4 实施中未做到的事（如实记录）

| 项 | 状态 |
|---|---|
| **原设计的"执行前写磁盘留痕"** | ❌ **未做** —— 登记表是内存的，进程被强杀时表也没了 |
| **"残留进程报告给 LLM"** | ⚠️ **机制已就位，但还没接到 core** —— `tasks.lingering()` 能查出"还有哪些进程活着"，`kill_lingering()` 能在退出时清理，但 **`core.py` / `main.py` 还没调用它们** |
| **POSIX 分支的杀进程树** | ⚠️ **未在真机验证** —— 按项目惯例已在代码注释标注（本机无可用 Linux） |
| **`_find_bash()` 加注释** | ✅ **已做** —— 在 `_find_bash()` 的 docstring 里补了实测依据（WSL 劫持现象），并加了"不要改成 `shutil.which`"的警告 |

**第 2 条值得说明**：用户当时说"2. 做表"、"1. 加参数"、"3. 超时杀进程"三件事，**这三件都做完了**。而"把表里的残留进程报告给模型"是**下一步**（需要动 `core.py` 的对话循环）。**本文档如实标注它是"机制就位、尚未接线"。**

### 10.5 实施过程中犯的错（如实记录）

| # | 错误 | 后果 | 怎么发现的 |
|---|---|---|---|
| 1 | 把 `read(4096)` 当读管道的方式 | 读不到任何数据，差点误判"方案不可行" | 对比实验：`read(1)` 能读到、`read(4096)` 读不到 |
| 2 | 用 `t.join(timeout=2)` 收尾 | 每次超时多等 2 秒 | 精确测量各步耗时 |
| 3 | 改用 `close()` + join | **死锁 27.38 秒**（更糟） | 测量发现 `close()` 本身阻塞 |
| 4 | 忘了 `import time`（用了 `time.sleep`） | 全部命令报 `NameError` | 端到端测试 |
| 5 | 测试里数"整机 `sleep.exe` 总数"判断残留 | **误报"有野进程"** —— 那些是别的 shell 会话的 | 查进程树发现父进程不是我的 |

**第 5 条特别值得记**：**测试写错了会掩盖真实情况** —— 当时代码其实已经杀干净了（进程树里我的进程全没了），但测试说"有残留"，让我多查了一轮。

**解法**：用**唯一标记**（`f"e2e{uuid.uuid4().hex[:6]}"`）过滤，而不是数进程名总数。

---

## 十一、这次修复的性质

抛开技术细节，三个缺陷的性质是**同一类**：

```
① timeout 不能精确中止      → 机制不完整（只杀了工人,没杀小工）
② 部分输出被丢弃             → 信息被浪费（已经拿到的线索扔了）
③ 卡住期间无痕迹             → 缺少可追溯性
```

**三层都是"原本可以做对，但没做"** —— **不是设计理念的问题，是实现上的疏漏**。

**与"推翻验证门"那件事性质完全不同**：

| | 验证门那件事 | 这次这件事 |
|---|---|---|
| 性质 | **设计前提错了**（想用硬规则替 AI 判断语义） | **设计没问题，实现没做全** |
| 处理 | 推翻重来 | 补上 |
| 依据 | 实测数据（66 次调用/4 次驳回） | 实测数据（4 个缺陷各自的复现） |

**两类问题的共同点是**：**都是实测数据发现的，不是"再想一遍"发现的。**

---

## 十二、附录 C：实施阶段的新实验（前四个见附录 A）

### C.1 实验 5：`TimeoutExpired.stdout` 是空的

```python
p = subprocess.Popen([BASH, "-lc", "echo HELLO_PARTIAL; sleep 30"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    p.communicate(timeout=3)
except subprocess.TimeoutExpired as e:
    print("stdout:", e.stdout)     # None
    print("stderr:", e.stderr)     # None
    print("output:", e.output)     # None
```

**输出**：

```
stdout: None
stderr: None
output: None
```

**结论**：**不能靠 `TimeoutExpired` 拿部分输出。**

### C.2 实验 6：二次 `communicate()` 会继续阻塞

```
① communicate 超时:        3.00s
   e.stdout = None
   e.stderr = None
② 杀树:                    0.11s (ok=True)
③ 二次 communicate:        5.00s  ← 【又超时】
   此时管道仍未关闭
总耗时: 8.12s
```

**结论**：`communicate()` 超时后状态已乱，**再次调用不会重新读管道**。

### C.3 实验 7：`read` vs `read1`（本方案的关键发现）

同样的命令，只换读法，各观察 4 秒：

```
p.stdout.read(1)      → 读到 b'HELLO\n'   ✅
p.stdout.read(4096)   → 读到 b''          ❌ 阻塞等填满缓冲区
p.stdout.read1(4096)  → 读到 b'WORLD\n'   ✅ 有多少读多少
```

**结论**：**必须用 `read1`**。`BufferedReader.read(n)` 的语义是"读到 n 字节或 EOF 为止"，对"边跑边输出、可能永远填不满缓冲区"的长命令会一直阻塞。

### C.4 实验 8：三种收尾方式的耗时

```
方式 a) t.join(timeout=2)
     → 读线程 join: 5.01s (alive=True)     ← 卡满
方式 b) p.stdout.close() 再 join
     → 关闭管道: 27.38s                    ← 死锁!close() 在等读线程放锁
     → 读线程 join: 0.00s (已退出)
方式 c) sleep(0.3) 后直接返回
     → 总耗时 3.44s (设定 3 秒),输出完整  ✅
```

**结论**：**用方式 c** —— 读线程设 daemon、不 join。

### C.5 实验 9：git-bash 的多层 fork 结构

```
Popen 返回的 pid = 31568
  pid= 31568 ppid= 21820  bash.exe  <== Popen 启动的
  pid= 26308 ppid= 31568  bash.exe       ← git-bash 又启动的一层

执行 taskkill /F /T /PID 31568:
  SUCCESS: 26308 (child of 31568) terminated
  SUCCESS: 31568 terminated
杀后残留: 0(用唯一标记过滤后确认)
```

**结论**：必须 `/T`（树）。**另外这次的"杀后残留 3 个 sleep"是误判** —— 那 3 个的父进程不是我启动的（`ppid` 分别是 12572/44392/36004，都不是 31568），是其他 shell 会话留下的。**这直接导致了 C.6 的教训。**

### C.6 实验 10：用"唯一标记"而不是"数进程名"判断残留

**错误做法**（我最初写的测试）：

```python
r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq sleep.exe", "/NH"], ...)
sleep_count = r.stdout.count("sleep.exe")     # ❌ 数的是整机数量
assert sleep_count == 0
```

**后果**：误报"有野进程残留"，因为**机器上其他 shell 会话也有 `sleep` 进程**。为此多排查了一轮。

**正确做法**：

```python
MARK = f"e2e{uuid.uuid4().hex[:6]}"
# 命令里带上标记 → 只查命令行含该标记的进程
ps = (f"Get-CimInstance Win32_Process | "
      f"Where-Object {{$_.CommandLine -like '*{MARK}*' -and $_.Name -ne 'powershell.exe'}} | ...")
```

（`-and $_.Name -ne 'powershell.exe'` 是为了排除**查询进程本身** —— 它的命令行里也含标记，这是另一个同类陷阱。）

**结论**：**测试的判据必须能唯一标识"我启动的东西"**，否则会把自己的进程、别人的进程、查询命令自身都算进来。

---

## 十三、附录 D：本机环境与适用范围

| 项 | 值 |
|---|---|
| OS | Windows 10（中文版） |
| shell | `C:\Program Files\Git\bin\bash.exe`（git-bash） |
| Python | 3.11.3 |
| 测试时间 | 2026-09-18 |

**本次所有实测结论都基于这个环境**。在 Linux/macOS 上：

| 结论 | 是否适用 |
|---|---|
| 超时后 `TimeoutExpired.stdout` 为空 | **很可能适用**（这是 `communicate` 的实现细节，非平台相关） |
| `read(4096)` 阻塞、`read1` 不阻塞 | **适用**（`BufferedReader` 的语义，非平台相关） |
| `close()` 死锁 | **未验证** |
| `taskkill /T` 杀树 | **不适用**（Windows 专用；POSIX 分支用 `os.killpg`，**未在真机验证**） |
| git-bash 多层 fork | **不适用**（git-bash 特有） |

**POSIX 分支的实现依据**（未实测，如实标注）：

```python
# tasks.kill_process_tree 的 POSIX 分支
os.killpg(os.getpgid(pid), signal.SIGKILL)
```

**它依赖 `Popen(start_new_session=True)`**（已在该分支的 `Popen` 调用里设置）。**按项目惯例，代码注释里已标注"未在真机验证"。**
