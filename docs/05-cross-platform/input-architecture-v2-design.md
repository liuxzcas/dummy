# 输入层架构改造 —— prompt_toolkit 常驻应用

> 编写：2026-11-11
> 状态：**设计阶段，未实施**
> 触发：用户提出「完整改造」

---

## 一、要解决什么

用户原话：

> "我想了想，hermes 的交互习惯是在 agent 处理 tool calling 时依旧等待用户输入。
>  如果用户按下回车，才打断并注入用户输入的内容，否则用户在 autopilot 任务期间
>  可以在命令行输入和编辑任意东西。"

按优先级拆成四条要求：

| # | 要求 | 现状 |
|---|---|---|
| **R1** | agent 干活时，用户仍可**打字、编辑**（左移/右移/退格/Home/End） | ❌ 做不到：msvcrt 只能抢字符，编辑能力为零 |
| **R2** | **回车**才提交 → 打断当前任务 + 注入这行内容 | ⚠️ 只有 `/p` 前缀才算打断，且要先敲两个字符 |
| **R3** | autopilot 下同样可用 | ⚠️ 刚修好（326e62e），但靠 msvcrt 抢字符，脆弱 |
| **R4** | 不再有"幽灵 /p"这类缓冲区残留问题 | ✅ 已修，但根因是架构（双输入源），补丁式修复治标 |

**根本矛盾**：现在有**两个输入源**在抢控制台

```
main 主线程:  prompt_toolkit.prompt()      ← agent 空闲时读
后台线程:     msvcrt.kbhit() 轮询          ← agent 忙时读
                    ↓
        两者竞争同一个控制台 → 丢字、幽灵缓冲、回显错乱
```

**改造目标：收敛回一个输入源。**

---

## 二、Hermes 的做法（实地查证）

### 2.1 主线程跑 prompt_toolkit 应用

```python
# cli.py:15567
app.run()          # prompt_toolkit Application 事件循环,占住主线程
```

### 2.2 agent 在后台线程

```
主线程:   app.run() —— 渲染输入区、处理按键、重绘
后台:     process_loop 守护线程 —— 跑 agent.chat()
```

### 2.3 两个队列分离职责

```python
# cli.py:6083
self._interrupt_queue.put(text) if self.busy_input_mode == "interrupt" \
    else self._pending_input.put(text)
```

| 队列 | 什么时候进 | 消费方 |
|---|---|---|
| `_interrupt_queue` | agent **忙**时用户回车 | agent 的检查点 → 打断 + 注入 |
| `_pending_input` | agent **空闲**时用户回车 | 主循环 → 下一轮任务 |

### 2.4 agent 侧的消费方式

```python
# cli.py:12430
interrupt_msg = self._interrupt_queue.get(timeout=0.1)
if interrupt_msg:
    ...打断...
```

**0.1 秒超时的 queue.get** —— 简单可靠，没有轮询键盘那套。

---

## 三、dummy 的目标架构

```
┌─────────────────────────────────────────────────────────┐
│ 主线程：prompt_toolkit Application                       │
│   · 输入区（可编辑、有光标）                              │
│   · 状态栏（显示 agent 在忙什么）                          │
│   · 回车 → 按 agent 忙闲分流到两个队列                     │
└─────────────────────────────────────────────────────────┘
              ↓ interrupt_queue        ↓ pending_queue
┌─────────────────────────────────────────────────────────┐
│ 后台线程：agent.chat()                                    │
│   · 工具循环检查点：queue.get(timeout=0.1)                │
│   · 命中 → 杀子进程 + InterruptSignal                     │
│   · 输出 → ui.raw()（线程安全打印）                        │
└─────────────────────────────────────────────────────────┘
```

---

## 四、要改的东西（附现状实测）

### 4.1 输出侧：这是最大的隐性工作量

**实测数字**：

| 文件 | `print()` 调用数 |
|---|---|
| `core.py` | **42** |
| `tools/write_file.py` | 23（大多是 diff 预览） |
| `ui.py` | 3 |
| `main.py` | 少量 |

**问题**：`core.py` 的 42 处 `print()` 在**后台线程**里跑。prompt_toolkit 的
`Application` 占着主线程渲染，后台线程直接 `print` 会**打乱输入区**（这和用户
此前报的"清屏/擦除"症状同源）。

**修法**：所有输出走一个线程安全的 `ui.raw()`：

```python
# ui.py
from prompt_toolkit.application import run_in_terminal

def _safe_print(text, end="\n"):
    if _app_is_active():
        # 暂停输入区 → 打印 → 恢复
        run_in_terminal(lambda: print(text, end=end))
    else:
        print(text, end=end)
```

**注意**：`run_in_terminal` 只在该 Application 正在运行时有效。
agent 空闲（应用在等输入）时可以直接 print；agent 忙时走 `run_in_terminal`。

### 4.2 输入侧：PromptSession → Application

```python
# 现在
session = PromptSession(...)
line = session.prompt("你 > ")      # 阻塞,一次一行

# 改成
app = Application(layout=Layout(HSplit([
    Window(ScrollablePane(...), height=D),     # 输出区(可选)
    Window(FormattedTextControl(status)),      # 状态栏
    Window(BufferControl(buffer=prompt_buffer)), # 输入区
])), key_bindings=kb, full_screen=False)
app.run()                                    # 常驻
```

**关键参数**：
- `full_screen=False` —— 保持滚动历史（用户要能上翻）
- 不用 `ScrollablePane`（那需要 full_screen）→ 输出仍走 stdout

### 4.3 打断机制：删掉 msvcrt

```
删除：
  · core._InterruptListener 整个类
  · tools.terminal.set_interrupt_probe / _default_on_poll
  · core._drain_interrupt 的监听器版本
  · peek_interrupt / consume_interrupt
  · 所有 /p 相关提示文案

新增：
  · 两个 queue（interrupt / pending）
  · agent.check_interrupt() → queue.get_nowait()
```

**`/p` 是否保留？**
```
用户没说要保留 /p。
新机制下"回车即打断",/p 前缀变成多余 ——
但保留它作为"显式打断"的兼容写法成本很低(判 startswith 即可)。
★ 待用户确认。
```

### 4.4 工具执行期间的可打断性

**现状**：`terminal` 工具 50ms 轮询 `on_poll`。
**改造后**：`on_poll` 改成 `queue.get_nowait()`，其余不变 —— **能复用**。

---

## 五、实施步骤（分五步，每步可独立验证）

| 步 | 内容 | 代码量 | 验证方式 | 风险 |
|---|---|---|---|---|
| **1** | `ui.raw()` 改成线程安全（`run_in_terminal` + 探测） | ~40 行 | 单测 + 手工 | 🟢 低 |
| **2** | 加两个 queue，agent 检查点改读 queue | ~60 行 | 单测 | 🟢 低 |
| **3** | 新建 `pt_app.py`：Application + 输入区 + 状态栏 | ~250 行 | 手工 | 🟡 中 |
| **4** | `main.py` 主循环改造：app.run() + agent 线程 | ~150 行 | 手工 | 🟡 中 |
| **5** | 删 msvcrt 监听器 + `/p` 文案调整 | **-200 行** | 全套测试 | 🟢 低（净删代码） |

**顺序理由**：1、2 步是基础且可独立验证；3、4 步是核心改造；
5 步是清理（删代码最安全，放最后）。

---

## 六、必须先确认的三个问题

### Q1：`/p` 还保留吗？

```
a. 保留：回车即打断，/p 前缀仍然接受（兼容旧习惯，成本≈0）
b. 删掉：只有"回车提交非空内容"才算打断
```

**建议 a** —— 万一用户习惯了敲 /p，也不至于失效。

### Q2：agent 忙时输入的内容，除了打断还要做什么？

```
现在: 打断后要么等提示词(非 autopilot),要么忽略(autopilot)
Hermes: 打断 + 把这行内容作为用户消息注入
```

**你的要求原文**："按下回车，才打断并注入用户输入的内容"
→ **打断 + 注入**。那么 autopilot 下也注入（不再丢弃）。

### Q3：空闲时输入的内容（pending 队列）怎么处理？

```
现在的行为: 直接进下一轮 chat()
改造后:     一样。但要注意"agent 刚结束、用户已打了字"的时序
```

---

## 七、已知风险

### R1：Windows 上 prompt_toolkit Application 的稳定性

**实测过的坑**（本项目已有记录）：
```
· TERM=xterm-256color + MSYSTEM=MINGW64 → NoConsoleScreenBufferError
· 需要真实控制台,管道环境下测不了
```

→ **必须由用户在真实 Windows Terminal 里验证**，我只能保证单测。

### R2：后台线程 print 与输入区打架

这正是用户报的"清屏/擦除"症状的**同类问题**。
`run_in_terminal` 是官方解法，但如果 Application 状态判断错了，
可能出现内容丢失。

→ 第 1 步单独做，先验证输出稳定，再动输入。

### R3：中文输入法

**未验证**：prompt_toolkit 在 Windows 下与微软拼音的兼容性。
现状（`PromptSession.prompt`）已在使用，理论上没问题，
但换成 `Application` 常驻后行为可能不同。

→ 第 3 步做出来后**第一时间测中文输入**。

### R4：清屏问题未定位

**前提问题**：用户 11-05 报的"工具轮间清屏"至今没找到根因
（修了 `reserve_space_for_menu=0` + `renderer.reset()` 仍偶发）。

**如果带着未定位的问题做这次大改造，出问题会难以归因。**

→ **建议先做第 1、2 步（不动输入层），观察清屏是否变化。**

---

## 八、不做的事

| 项 | 理由 |
|---|---|
| 输出区做滚动面板（ScrollablePane） | 需要 full_screen=True，会失去终端原生滚动历史 —— 与用户"要能上翻"的诉求冲突 |
| 自动补全 | 用户没要求，且会触发 `reserve_space_for_menu` 那类问题 |
| 多行输入框 | 粘贴已经有两档方案，够用 |
| 语音/图片附件入口 | 与本次改造无关 |

---

## 九、待办清单（实施时逐条勾）

- [ ] Q1/Q2/Q3 三个问题用户确认
- [ ] 第 1 步：`ui.raw()` 线程安全 + 测试
- [ ] 第 2 步：双队列 + agent 检查点改造 + 测试
- [ ] 第 3 步：`pt_app.py`（Application + 输入区 + 状态栏）
- [ ] 第 4 步：`main.py` 主循环改造
- [ ] 第 5 步：删 msvcrt 监听器
- [ ] 手工验证：中文输入 / 光标编辑 / 回车打断 / autopilot 模式 / 长任务
- [ ] 清屏问题复测（改造前 vs 改造后）
