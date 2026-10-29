# 输入层换 prompt_toolkit —— 改造方案

> 编写：2026-10-16
> 状态：**方案（未实施）**
> 相关代码：`input_layer.py`、`main.py`、`core.py`（`_InterruptListener` / `_confirm_provider`）
> 前置提交：`65ba872`（输入层收口 + 拖拽识别）

---

## 一、要解决什么

| # | 问题 | 现状 | 根因 |
|---|---|---|---|
| **1** | 多行/大段粘贴被拆成多条消息 | 输入 `第一行\n第二行` → `input()` 返回 **3 次** | `input()` 以换行符为结束标记 |
| **2** | Win11 中文输入吞字 | 打几个字后丢 | `pyreadline3` 的 `getchar()` 用 `uChar.AsciiChar`（`c_char` 单字节） |
| **3** | Win11 粘贴切片 | 粘贴内容被切成片段 | `pyreadline3` 的 `paste_line_buffer` 每次只取第一行 |
| **4** | 拖拽/粘贴文件路径 | ✅ 已能识别 | — |

**1、2、3 是同一个根因**：`input()` + readline 无法表达"这是一个粘贴块"。

---

## 二、为什么 prompt_toolkit 能解决

Hermes 的实证（`cli.py:14131`）：

```python
@kb.add(Keys.BracketedPaste, eager=True)
```

**现代终端粘贴时发送**：

```
\x1b[200~ 粘贴的全部内容（含换行） \x1b[201~
```

`prompt_toolkit` **认识这个序列**，把整块作为一次 `BracketedPaste` 事件交付，
**里面的换行不会触发提交**。原生 `input()` 不认识它（换行照样当结束符）。

**另外**：`prompt_toolkit` 自己实现行编辑与渲染，**不经过 readline** →
`pyreadline3` 的两个 bug 自然绕开（Win11 吞字、粘贴切片一并解决）。

---

## 三、现状盘点（改造要碰的东西）

### 3.1 真实 `input()` 调用点 —— **只有 4 处**

| 位置 | 用途 | 改造难度 |
|---|---|---|
| `input_layer.py:read_input()` | 主循环（唯一入口） | 低 |
| `core.py:599` | `/p` 打断后收集提示词 | 低 |
| `core.py:1338` | 执行确认 y/n | 低 |
| `core.py:1490` | `_confirm_provider`（确认输入统一入口） | **中**（见 3.2） |

**好消息**：输入已经收口过一轮，改造面比预想小。

### 3.2 最大改造点：`_InterruptListener`（`core.py:145-215`）

现在是 **Windows `msvcrt` 后台线程轮询**：

```python
while not self._stop:
    if not self._paused:
        while self._msvcrt.kbhit():
            ch = self._msvcrt.getwch()
            ...累积到 buffer，回车标记行结束
    time.sleep(0.02)
```

**它直接抢 stdin**。`prompt_toolkit` 运行时也独占 stdin → **两者不能共存**。

**而且它只在 Windows 上有效**：
```python
try:
    import msvcrt          # ← POSIX 上 ImportError → 监听器退化（take_line 恒 None）
except ImportError:
    pass
```
→ **Linux 上 `/p` 打断功能其实是坏的**（这个缺陷换库后可一并修好）。

### 3.3 输出层冲突风险

`ui.py` 只有 3 处 `print()`，`core.py` 37 处。**数量不多，但都是流式输出**
（工具调用/结果/思考中）。

`prompt_toolkit` 应用运行时会接管屏幕渲染 —— 在它之外直接 `print()` 会
**覆盖提示符 / 错位**。Hermes 踩过，代码里有 `_strip_leaked_bracketed_paste_wrappers` 这种补丁。

---

## 四、改造方案

### 4.1 总体架构

```
                 ┌─────────────────────────────┐
   main.py  ────► │ input_layer.py              │
   core.py  ────► │   read_input(prompt)        │  ← 唯一实现点
                 │   read_input_with_drop()    │
                 │   confirm_input(prompt)     │
                 │   collect_interrupt_prompt()│
                 └──────────┬──────────────────┘
                            │
                 ┌──────────▼──────────────────┐
                 │ PromptSession（单例）        │
                 │  · history                   │
                 │  · key bindings:             │
                 │      BracketedPaste → 当整块 │
                 │      Enter          → 提交   │
                 │  bottom_toolbar: 提示"输入中"│
                 └──────────────────────────────┘
                            │
                 ┌──────────▼──────────────────┐
                 │ /p 打断：改用 prompt_toolkit │
                 │ 的按键绑定，不再抢 stdin      │
                 └──────────────────────────────┘
```

**关键点**：所有调用方只认 `input_layer` 的函数，**不知道底下是 input 还是 prompt_toolkit**。

### 4.2 分四步实施（每步可独立验证、可回退）

#### 第 1 步：接入 PromptSession（不改任何调用方）

**改动**：`input_layer.py` 内部

```python
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

_session: PromptSession | None = None
_paste_buffer: list[str] = []      # BracketedPaste 收进来的块

def _get_session() -> PromptSession:
    global _session
    if _session is None:
        kb = KeyBindings()

        @kb.add(Keys.BracketedPaste, eager=True)
        def _(event):
            """整个粘贴块作为【一个字符串】收下,不提交。"""
            _paste_buffer.append(event.data)

        _session = PromptSession(key_bindings=kb)
    return _session


def read_input(prompt: str) -> str:
    _paste_buffer.clear()
    line = _get_session().prompt(prompt)
    if _paste_buffer:
        # 粘贴块 + 用户随后输入的内容 → 合并成一个输入
        return "".join(_paste_buffer) + line
    return line
```

**验证**：跑现有 48 条 `test_input_layer.py`（全部 mock `builtins.input`，
要改成 mock `PromptSession.prompt`）；手动测多行粘贴。

**回退**：改回 `return input(prompt)`。

---

#### 第 2 步：多行/大段文本 → 临时文件

**改动**：新增 `maybe_collapse_paste(text) -> str`

```python
def maybe_collapse_paste(text: str) -> str:
    """超长或多行的输入 → 写临时文件，返回占位符。

    判据（2026-10-16 用户定：含换行符 OR ≥2000 字符）:
        · 含换行符（★ 现在终于可达了 —— 之前 input() 永远吃掉了换行）
        · 或字符数 ≥ 2000

    写入位置: <项目>/pastes/paste_<N>_<时间戳>.txt
    返回: "[Pasted text #N: 47 lines → <path>]"
    """
```

**为什么现在"含换行符"这个判据成立了**：
之前 `input()` 把换行当结束标记，返回值里永远没有 `\n`；
`BracketedPaste` 收到的 `event.data` **原始保留换行**。

**验证**：单元测试（3 行文本、1999/2000 字符边界）+ 手动粘贴网页段落。

---

#### 第 3 步：`/p` 打断改造（最大工作量）

**现状**：`msvcrt` 后台线程轮询（Windows only）。

**目标**：用 prompt_toolkit 的按键绑定，跨平台。

**难点**：打断的需求是 **"工具正在跑的时候也能打断"** ——
而那时 prompt_toolkit 的 `prompt()` 并没有在等输入（agent 在跑工具）。

**三个做法**：

| 做法 | 说明 | 评价 |
|---|---|---|
| **a. 保留轮询，但换成跨平台读键** | POSIX 用 `select`+`termios`，Windows 保留 `msvcrt` | ⚠️ 还是要抢 stdin，与 prompt_toolkit 运行时冲突 |
| **b. 工具运行时用 `prompt_toolkit` 的"异步 prompt"** | 工具执行期间跑一个 `prompt()` 任务，收到 `/p` 就取消工具 | ✅ **最干净**，但要改工具执行为异步/可取消 |
| **c. 用 `Application.run_async()` + 协程架构** | 整个主循环变成一个 prompt_toolkit Application | ⚠️ 大重构 |

**建议**：**第 3 步暂不追求"工具运行中可打断"**，先做：
- 保留现有 `msvcrt` 监听（Windows）
- POSIX 上补一个 `select`+`termios` 实现（修复 Linux 上 `/p` 失效的缺陷）
- **在 prompt_toolkit 的 `prompt()` 期间**，用 `pause()` 暂停监听（和现在一样）

**理由**：方案 b/c 需要把工具执行改成可取消的，那是**另一个大工程**（涉及 subprocess 取消、
LLM 请求取消），不该和输入层混在一起。

---

#### 第 4 步：输出层适配

**问题**：`prompt_toolkit` 运行时，直接 `print()` 会破坏它的渲染。

**现状**：`ui.py` 有 3 处 `print()`，`core.py` 37 处。

**做法**：`ui.py` 的 `emit()` 加一个"渲染器"分支：

```python
def emit(self, text: str) -> None:
    if _PT_APP_RUNNING:
        # 用 prompt_toolkit 的打印方式（不会破坏渲染）
        print_formatted_text(ANSI(text))
    else:
        print(text)
```

**关键**：`prompt_toolkit` 只在 `session.prompt()` **期间**接管屏幕；
工具执行期间（prompt 已返回）用普通 `print()` 是安全的。

**所以真正要注意的只有**：prompt 显示时、输出到达。这种情况在 dummy 里存在吗？
→ **`/p` 打断在确认提示里输入时**（`core.py:1490`）。

---

## 五、风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| **输出与提示符错位** | 高（用户可见） | 分步验证；第 4 步专门处理；Hermes 的 `_strip_leaked_bracketed_paste` 说明可以打补丁 |
| **测试失效** | 低（实测影响面小） | 现有测试里 mock 输入的只有 **5 处**（`test_input_layer.py` / `test_interrupt_semantics.py` / `test_lessons.py`）；改成 mock `input_layer.read_input` 即可 |
| **`msvcrt` 监听与 prompt_toolkit 抢 stdin** | 高 | 用 `pause()/resume()` 隔离（现有机制已有） |
| **Windows 上 prompt_toolkit 的 IME 支持** | 中 | **必须真机验证**（你本机 Win10 可测中文输入，Win11 吞字需另测） |
| **依赖体积** | 低 | prompt_toolkit 约 1MB，纯 Python |

---

## 六、验证计划

| 步骤 | 自动化 | 手动 |
|---|---|---|
| 1. PromptSession 接入 | 现有测试改造后全过 | 多行粘贴不拆分；中文输入正常 |
| 2. 长文本转文件 | 新增单测（边界值） | 粘贴网页段落 → 出现 `[Pasted text #1]` |
| 3. `/p` 改造 | 新增单测（POSIX 键盘读取） | 工具运行中按 `/p` 能打断 |
| 4. 输出适配 | 现有测试 | 边输出边输入不错位 |

**回归基线**：当前 `406 passed / 1 xfailed`。

---

## 七、明确不做（本方案范围外）

| 项 | 理由 |
|---|---|
| **工具执行中可中断（异步取消）** | 涉及 subprocess/LLM 请求取消，是独立工程 |
| **多行编辑 UI（上下移动光标编辑多行）** | prompt_toolkit 支持，但 dummy 目前不需要 |
| **自动补全** | 无场景需求（项目原则：不做无场景功能） |
| **语法高亮** | 同上 |

---

## 八、工作量估算

| 步骤 | 代码量 | 时间 |
|---|---|---|
| 1. PromptSession 接入 | ~80 行 | 2 小时 |
| 2. 长文本转文件 | ~120 行 | 2 小时 |
| 3. `/p` 跨平台改造 | ~150 行 | 3 小时 |
| 4. 输出层适配 | ~60 行 | 2 小时 |
| 测试改造 + 新增 | ~300 行 | 3 小时 |
| **合计** | **~700 行** | **约 1.5 天** |

---

## 九、前置条件

**必须先做的**：在 Win11 上验证目标 4 的两个 bug
（吞字是否从 `a7457f5` 引入）—— 因为如果换 prompt_toolkit 后问题依旧，
说明根因判断有误，需要重新定位。
