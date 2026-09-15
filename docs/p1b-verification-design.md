# P1b 设计依据：收尾前验证门（verify-on-stop）

> 目的：回答"验证门该**严**到什么程度"，并给出 dummy 选定的口径（档 A）。
> 调研于 2026-09-15，覆盖 6 个实现 + 4 篇论文。
> **出处说明**：Hermes 的实现由本机源码逐行核对（附 file:line）；
> 其余各家来自官方文档/源码/论文（附 URL）。凡属推测均标注「未验证」。

---

## 0. 为什么需要这道门（实证）

dummy 的日志里有两次典型事故，都是"没验证就宣布完成"：

| 事故 | 现象 |
|---|---|
| 雷战机那次 | LLM 改完黑屏，说"修好了"，实际根本没验证运行，打开还是黑屏 |
| 网页/PPT 那次 | 文件写出来了、语法没错，但跑不起来，来回返工 |

根因不是模型"不会写代码"，而是**框架允许它自己说"我完成了"**。

---

## 1. 先把"严"拆成三个独立的轴

只谈"严"会把设计带偏（典型误区：把阈值从 5 调到 2，那只是更烦，不是更严）。
真正的严是三个可分别取值的轴：

| 轴 | 松 ⟶ 严 |
|---|---|
| **① 证据内容** | 模型自述"跑过了" → 规范命令 exit code → 真测试输出 → 端到端外部证据 |
| **② 判决者** | 同一模型自评 → 独立 critic 模型 → **确定性脚本（退出码）** |
| **③ 强制力** | 提示词引导（可跳过） → 钩子（可配置） → 管理员锁 + 次数上限 |

**dummy 要加的严在 ① 证据内容；② 已经是硬的（详见 §5）。**

---

## 2. 六个实现的横向对照

| 实现 | ① 什么算证据 | ② 何时检查 | ③ 失败怎么办（含上限） | ④ 能否绕过 | 性质 |
|---|---|---|---|---|---|
| **Hermes** | 跑过"规范命令"，**不校验覆盖范围** | 收尾前 | 合成消息驳回，**上限 2** | 配置/环境变量；CLI 默认开 | 工具内部 |
| **Claude Code** | **钩子退出码**（`exit 2` = 驳回；`exit 1` 只是警告——官方明确警告此坑）；另可用 `prompt` 型（单次 LLM）或 `agent` 型（**独立子代理**，可 Read/Grep/Bash，≤50 轮） | 每轮收尾（`Stop`）+ 每次工具后（`PostToolUse`）+ 子代理结束（`SubagentStop`） | `decision:"block"` 必带 `reason`，**reason 成为下一条指令**；**连续 block 8 次强制放行**（`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`） | **默认关**，须自写 `settings.json`；`disableAllHooks` 全关 | 工具内部（确定性） |
| **Codex CLI** | ①AGENTS.md 里的检查（**纯提示词**）②`Stop` 钩子脚本 | 收尾前 | 钩子 `exit 2` 或 `{"decision":"block","reason":…}` → **reason 变成新一轮 user prompt**；无内建上限，用 `stop_hook_active` 防死循环 | **默认开**（`"Hooks are enabled by default"`）；`[features] hooks = false` 关 | 两层并存 |
| **Aider** | **exit code**（`--test-cmd`，测试命令不接受参数） | **每次编辑后立即** | 把错误原文回灌为下一轮消息；**`max_reflections = 3`**（源码硬编码，超限报 `Only 3 reflections allowed, stopping.`） | `auto-lint` 默认开 / `auto-test` **默认关** | 工具内部 |
| **OpenHands** | **独立 critic 模型**给整条 trajectory 打 0~1 分 + 问题标签 | agent 调 `FinishAction` 时 | 分数 < **阈值 0.6** → 追加 follow-up 驳回；**上限 3** | 独立端点、独立 API Key | 独立模型 |
| **AlphaCodium** | **LLM 写的测试但真执行**、比对 input/output；**test anchors** 锁死已知正确的测试 | 生成阶段 + 迭代阶段 | 真实错误回灌重跑；**修复后必须仍通过全部 anchors** | — | 工具内部 |

出处：
Claude Code `https://code.claude.com/docs/en/hooks`、`/hooks-guide`；
Codex `https://learn.chatgpt.com/docs/hooks`、`https://openai.com/index/introducing-codex`、`https://developers.openai.com/codex/agent-configuration/agents-md.md`；
Aider `https://aider.chat/docs/usage/lint-test.html`、`https://aider.chat/docs/config/options.html`、`aider/coders/base_coder.py`；
OpenHands `https://docs.openhands.dev/sdk/guides/critic`、`openhands.dev/blog/20260305-learning-to-verify-ai-generated-code`、arXiv:2603.03800；
AlphaCodium `arXiv:2401.08500`、`github.com/Codium-ai/AlphaCodium`。

### 2.1 几个值得单独记下的细节

- **Claude Code 只用 `exit 2` 能拦住**；`exit 1` 是非阻塞警告，官方明确警告这个坑。
- **Codex 的钩子默认开，Claude Code 默认关**；Aider `auto-lint` 默认开而 `auto-test` 默认关。
  → **业界对"默认开"没有共识**，这是产品定位差异，不是对错。
- **上限取值 2 / 3 / 3 / 8 都有**，但**没有一家不设上限**。
- Aider 有一句直接点题的设计原则：**`exit code + 机器可读 reason`（可强制）≫ AGENTS.md 文字（可被跳过）**。

---

## 3. 严格度阶梯（含学术依据）

| 级 | 做法 | 真实例子 |
|---|---|---|
| L1 | 自述"跑过了"，无证据 | 反例见 Huang 2024 |
| L2 | 同模型自评自改 | Self-Refine |
| **L3** | 合成消息驳回 + 要求跑规范命令（**证据仍靠自述**） | **Hermes 现状** |
| L4 | 独立 critic + 阈值 + 自动驳回 | OpenHands（0.6 阈值、≤3 次） |
| L5 | 真测试 + 独立模型 + 不可绕过 | AlphaCodium、CRITIC、OpenHands PR review 的 `require-evidence`（**"Test output alone does not count"**） |

### 3.1 "自验证不可靠"是有论文依据的

- **Huang et al.《LLMs Cannot Self-Correct Reasoning Yet》(ICLR 2024, arXiv:2310.01798)**：
  **没有外部反馈的自我纠错（intrinsic self-correction）不仅不改善、反而更差**；
  Reflexion/RCI 的表观提升来自 oracle labels，把 oracle 拿掉就消失。
- **CRITIC (ICLR 2024, arXiv:2305.11738)**：critique **只有在锚定外部工具**
  （搜索 API / 解释器）时才有效，**去掉工具收益趋零**——论文明确点出 LLM 自验证能力不足。
- **Reflexion (arXiv:2303.11366) / Self-Refine (arXiv:2303.17651)**：有效，
  但强依赖基座模型能力与反馈质量（Vicuna-13B 连稳定执行 feedback 都做不到）。

**结论：把"我完成了"的判断权交给模型自己，在实验上是站不住的。**

---

## 4. 最深的一条结论

这六家的"严"**没有一家是靠"更聪明的评委"，全是把判决外包给一个退出码**。

- Claude Code 的 `Stop` 钩子、Codex 的 `Stop` 钩子、Aider 的 `test-cmd`，本质是同一件事：
  **判决者是 `exit 0`**。
- OpenHands 是唯一引入独立模型的，但它也把连续分数**卡在 0.6 阈值**上——
  把连续判断离散成一个确定判决。

**所以 dummy 该加严的地方是 ①（证据内容），而不是把阈值调小。**

---

## 5. dummy 的现状与缺口

**已有的（② 这一轴其实已经是硬的）**：dummy 的 `terminal` 工具结果里带
`[EXIT CODE: N]`（仅在失败时追加，`tools/terminal.py:137`），
所以框架**能直接从工具结果读出退出码**，不需要相信模型的自述。

**缺的（① 这一轴太松）**：没有任何"证据覆盖范围"的要求——
跑一个无关脚本也算数，等于把 Hermes 的 L3 换了个皮。

---

## 6. 档 A 规格（2026-09-15 选定）

### 6.1 判据

> **本轮的通过证据 = 一次"完整测试套件"的 pytest 执行且成功。**

三个条件全部满足才算：

1. **命令是全量调用**：含 `pytest`，且
   - 不含 node id（`::`，即不指定单个用例）
   - 不出现单个 `.py` 文件（不指定单个测试文件）
   - 不含缩小范围的参数：`-k` / `--keyword` / `--deselect` / `--lf` / `--last-failed`；
     `-m <标记>` 也算缩小，但 `-m pytest` 是模块调用形式，不算
2. **必须晚于最后一次编辑**（"先跑测试、后改代码"不算数——编辑会让先前证据失效）
3. **必须留痕**：记录命令原文 + 输出摘要（前 200 字符 + sha256 前 12 位）

### 6.2 为什么"全套跑绿"就够严

**全套通过隐含覆盖了被改动的文件**——这是它能当证据的全部理由，
因此不需要维护"模块 → 测试"的映射表。代价是每次改 `.py` 都要跑一遍
（dummy 当前 158 个测试约 13 秒，可接受）。

反过来说，**一旦用 `-k`/`--lf` 把子集挑出来，这个蕴含关系就不成立了**，
所以这些参数必须判为"不算证据"。

### 6.3 配套（业界高度一致，都不是可选项）

| 配套 | 取值 | 为什么 |
|---|---|---|
| **次数上限** | 默认 **2**（业界 2/3/3/8） | 防止**单轮内**反复驳回。**注意它拦不住跨轮重复**——见 §7.5 |
| **防重入** | 由上限计数天然承担 | 对应 Claude Code / Codex 的 `stop_hook_active` 字段 |
| **文档类豁免** | `.md/.markdown/.mdx/.rst/.txt/.adoc/.log/.csv/.tsv` + `LICENSE/CHANGELOG/...` | 无运行时行为可验；改 README 不该被要求跑测试（照 Hermes 的白名单） |
| **判决用退出码** | 不用模型判断 | §4 的结论 |
| **无法验证时的出口** | 驳回文本明说"请说明具体卡点，不要声称已完成" | 防止模型用"假装验证"绕过 |

### 6.4 与 Hermes 的三处差异

1. **加严在证据内容**：Hermes 只要"跑过规范命令"（不校验覆盖范围，L3）；
   dummy 要求**全量测试通过**（L5 的证据轴）。
2. **不探测项目规范命令**：Hermes 有 `project_facts_for` 探测
   pytest/npm/make（`agent/coding_context.py:771-788`），dummy 简化成硬编码规则
   （含 pytest 且不缩小范围）——够用且零维护。
3. **原答案不降级**：Hermes 把被驳回的答案存为 `_pending_verification_response` 兜底；
   dummy 直接把答案与驳回消息都写进历史（模型能看到自己刚说过什么，便于修正），
   简单且不丢信息。

---

## 7. 已知局限（诚实说明）

1. **判据是启发式，不是命令行解析器。** 用引号/变量拼出来的等命令可能误判。
2. **不防刻意伪造。** 例如 `echo pytest` 会被判为"全量 pytest"。
   本门防的是**"没验证就说完成"**，不是对抗性作弊——与项目一贯的安全立场一致
   （本地开发 Agent，靠可见性 + 人在终端前判断）。
3. **只覆盖 `.py` 之外的可验证文件也一律要求 pytest 证据**（如改 `.html`/`.json`）。
   目前 dummy 没有对应的自动验证器，所以这条会显得偏严；如果实际用起来太吵，
   可以按扩展名分档（`.py` 要求 pytest，其余放宽）。
4. **门只在"模型想以纯文本收尾"时开**，不覆盖 `MAX_TOOL_TURNS` 用尽那条路径
   （那种情况本身已经是失败态）。
5. **次数上限只防"单轮内"重复，不防"跨轮"重复**（实测得出，2026-09-15）。
   Hermes 的计数是**每轮清零**的：`agent/turn_context.py:532-534`
   （注释就写着 `Per-turn file-mutation verifier state.`）：
   ```python
   agent._turn_file_mutation_paths = set()
   agent._verification_stop_nudges = 0
   ```
   所以如果**证据永远无法被识别**（例如项目里没有可被探测的规范测试命令，
   模型跑的 pytest 匹配不上任何 canonical command），结果不是"敲两次就停"，
   而是**每一轮都敲一次**。实测连续三轮各触发一次。

   > 由此得出的设计硬约束：光有次数上限不够，**必须保证"存在一条能被识别的
   > 取证路径"**。否则门会退化成每轮噪音。Hermes 靠 `project_facts_for` 探测
   > 规范命令（探不到就要求 ad-hoc 脚本）；dummy 的档 A 是硬编码规则，
   > 天然不存在这个"探不到"的问题——这是简化带来的意外好处。
6. **证据没有绑定"在哪个目录跑的"**（构造性缺口，未修）。
   `tools/terminal.py` 的 `_run_shell(command)` 不固定工作目录，命令可以
   `cd 别处 && pytest`，跑的是别的项目的套件也会被记为证据。
   不修的理由：要堵它得解析命令里的 `cd`（或强制注入 cwd），而"先 cd 再跑"
   本身是必要且常见的写法（`cd /d/Engineering/dummy && pytest tests/ -q`）。
   这属于**需要模型刻意误导**才能触发的缺口，与 §7.2「不防刻意伪造」同一类，
   不在本门的目标范围内。

---

## 7.1 已修的两个真实漏洞（2026-09-15 审计发现）

审计动机：不能带着任何"已知可能的 bug"上线。逐个判据过了一遍证据路径，
发现两个**会产生假通过（false pass）**的真漏洞：

| 漏洞 | 症状 | 修法 |
|---|---|---|
| **管道/后续命令掩盖退出码** | `python -m pytest tests/ -q 2>&1 \| tail -3` —— shell 报告的退出码是 **tail 的（恒为 0）**，于是**测试失败也显示退出码 0**，被记成"验证通过"。`pytest … \| tail` 是极常见写法，**不是边角案例** | 改判据为"命令必须**以** pytest 调用收尾"（最后一段是 pytest，退出码才等价于测试结果） |
| **被取消的命令算通过** | 用户按 `n` 取消时 handler 返回 `"[用户取消] 命令未执行"`——它既不含 `[SHELL:` 也不含任何错误特征，`is_tool_error` 判 False → **一条从未运行的 pytest 被记为通过证据** | 要求返回串带 `[SHELL:` 标记（terminal 真执行时首行必有，取消时必无） |

第二个漏洞同时收紧了 `echo pytest` 这类蒙混（原来能被判成"全量 pytest"）。
两条都加了回归测试（`test_piped_pytest_is_not_evidence`、
`test_cancelled_pytest_is_not_evidence`、`test_pytest_must_be_the_last_shell_segment`、
`test_pytest_must_be_actually_invoked`）。

**同源发现的 Hermes 侧问题（A/B 实测确认）**：Hermes 的验证账本按**整条命令**的
退出码判状态（`tools/terminal_tool.py:2748` 传 `returncode`，
`agent/verification_evidence.py:421` 用它算 `status`），所以同样的管道掩盖问题
在它那边也存在：

```
python -m pytest tests/no_such_test_file.py -q 2>&1 | tail -2
    → 退出码 0  → status: "passed"   ✗   （pytest 实际退出码是 4）
python -m pytest tests/no_such_test_file.py -q
    → 退出码 4  → status: "failed"   ✓
```

这是"判决外包给退出码"这条原则的一个反面教材：**退出码本身也会被 shell 语义
骗过**。dummy 的档 A 通过"要求 pytest 收尾"回避了它。

---

## 8. 实现位置

| 位置 | 内容 |
|---|---|
| `verify_stop.py` | 纯账本 `VerificationLedger` + 判据（`is_verifiable_path` / `is_write_success` / `is_full_suite_pytest_command`）+ 驳回文本 |
| `core.py` | 接线四处：import、`__init__` 建账本、`chat()` 开头 `reset_for_turn()`、工具循环后 `note_tool_result()` |
| `core.py`（收尾点） | 在 `final_text = response_message.content or ""` 之后调 `build_stop_nudge()`，命中则追加驳回消息并 `continue` |
| `tests/test_verify_stop.py` | 22 项：判据 + 账本 + 端到端 |

环境变量：

```
DUMMY_VERIFY_ON_STOP=0          # 关掉验证门
DUMMY_VERIFY_MAX_ATTEMPTS=4     # 改驳回上限(默认 2)
```

---

## 9. 后续可选升级（未做）

- **L4：独立 critic 模型**（OpenHands 路线）——给 trajectory 打分 + 阈值驳回。
  成本：每次收尾多一次 LLM 调用；需设计 rubric。**当前不建议**：
  dummy 是教学项目，而 L5 的证据轴（真测试 + 退出码）已经是最硬的部分。
- **Aider 式"每次编辑后立即验证"**（比收尾前一次更严）——若实际使用中
  发现"改了好几次才在收尾时一次验证"漏掉了中间错误，可以升级到这一档。
