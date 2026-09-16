# 结构化工具结果 vs 文本标记：学术文献调研

**调研目标**：为「把工具调用结果从"字符串子串匹配判断失败"改为"结构化错误字段"」提供学术依据。
**日期**：2026-09
**方法**：arXiv / ACL Anthology 定向检索，重点 2024–2026。区分【实证结论】与【作者观点】。

---

## 0. 一句话结论

**文献支持"工具结果应结构化"，但支持的论证点与直觉不同。**

- 实证证据**强**支持：返回值必须携带**显式的失败语义 + 修复所需的具体信息**（位置、观测值、可接受的替代值）。
- 实证证据**弱/无**支持：「JSON 键值格式本身比等值散文更好」。有一篇配对实验明确发现 keyed JSON 与自然语言散文效果几乎相同。
- **反对子串匹配**的证据非常强：substring heuristic 判准与人类标注的一致度 κ=0.049（等于抛硬币）。

**给本项目的直接启示**：修 bug 的正确动机不是"结构化很好看"，而是 **①失败语义必须由产生它的那一层（工具实现）显式声明，不要在消费端猜；②错误载荷必须包含修复所需信息，而不只是一个 error 标志。**

---

## 1. 领域概览：学术研究的确切名字

你要找的领域在文献里不叫 "structured tool output"，而叫以下几个名字：

| 检索概念 | 文献里的正式叫法 |
|---|---|
| 错误表示与传播 | **error propagation in agentic systems / TIR (Tool-Integrated Reasoning) agents** |
| 工具结果的刻画空间 | **observation space design / tool interface design** |
| 把失败反馈给模型 | **error feedback to LLM agents / repair feedback / critique** |
| 文本层面失败信号的危害 | **silent errors / fabricated tool execution / over-trust** |
| 退出码语义 | **action/observation grounding, semantic exit status**（研究很少，见 §4） |

---

## 2. 核心论文清单

### 2.1 【最有价值】Structured Feedback Improves Repair in an LLM Agent Loop

- **arXiv**: 2607.14167 (v1)
- **年份**: 2026
- **机构**: VeriHarness 项目（作者列表未在摘要页完整给出）
- **关键词**: validator feedback, repair loop, TextWorld

**【实证结论】** 50 个配对 TextWorld 游戏、四次调用上限：

- 反馈包含**三个字段**（失败位置 `location` + 观测值 `observed` + **可接受的替代值 `expected`**）相比 raw diagnostic 原始报错：
  - Qwen2.5-Coder-14B: 14/50 → 36/50（**+44 个百分点**，95% paired-bootstrap CI 28–60）
  - Llama-3.1-8B: 8/50 → 29/50（**+42 个百分点**，CI 28–56）
- **消融结果（对我们的决策最关键）**：
  - `SameNL`（同样三个值，用**自然语言散文**表达）比 RawDiag **+42 点**；
  - `TypedFields`（keyed JSON + 稳定失败标签）与 `SameNL` 差距仅 **2 点（Qwen）/ 0 点（Llama）**。
  - `LocObs`（只有位置+观测值，**没有替代值**）**基本等于** raw diagnostic 基线。
- **作者结论原话**："the large gain comes from the repair values rather than an observed advantage for the keyed representation" / "Named JSON fields may still be useful for logging and routing, but these results do not show that the keyed format itself improves model reasoning."
- **额外发现**：把 RawDiag 的调用预算从 4 提到 8 **并不能**多解决问题——**纯重试无用，重试必须携带新信息**。

> **启示**：这条对本项目是"半个支持"。我们**不该**用"JSON 能提升模型推理"来论证改法。正确论证是：**失败必须被显式编码，且必须携带修复信息**。JSON 的价值在于**程序可控性/可路由性/可日志化**，以及**防止子串误判**——不是模型推理增益。

---

### 2.2 【最直接的反例证据】Auditing Automated Evaluation, Error Propagation, and Runtime Mitigation in Tool-Using Language Agents (AgentProp-Bench)

- **arXiv**: 2604.16706
- **年份**: 2026
- **作者**: Bhaskar Gurram（代码 https://github.com/bhaskargurram-ai/agenthallu-bench）
- **规模**: 14,750 条 execution traces，13 个 LLM agent（9 闭源 + 4 开源），4 个领域；2,000 任务；100 条人类标注

**【实证结论 1 — 直接打击字符串匹配】**
> 子串启发式（substring heuristic，模仿常见 agent benchmark 做法：参考前 20 字符出现在回答前 200 字符中，或任一实词命中即判对）与人类标注的一致性 **Cohen's κ = 0.049**（两位标注者各自），**统计上等同于随机**。
> 作者原话："Our κ=0.049 result is the lowest heuristic-vs-human agreement we are aware of in any agent or QA evaluation study." / "substring heuristics are worse than coin flips in the agent-evaluation context."

对比：3-LLM ensemble κ=0.432；单个 GPT-4o-mini κ=0.567；双标注者间 κ=0.835。

**【实证结论 2 — 错误传播】**
- 参数级错误传播到错误最终答案的人类校准概率 **≈ 0.62**，跨闭源/开源模型复现。
- **"拒绝被污染的输入"能力与"从错误中恢复"能力统计独立**（Spearman ρ = 0.041）——即"检测到失败"和"处理好失败"是两件事，需要分别设计。

**【实证结论 3 — 伪造工具执行】**
- 多个 agent **虚构工具执行**（声称调用了工具并得到结果，实际未调用），最高达 **37.5%** 的 trace。这类失败**在端到端打分中完全不可见**，必须instrument pipeline 才能发现。
- 一个轻量 runtime interceptor 在所有 tool-calling 模型上降低幻觉（最高 −24pp）。

> **启示**：这是"用字符串猜语义不可靠"最强的量化证据。我们当前的子串匹配 bug 属于同一类错误的两个方向（把成功当失败 / 把失败当成功）——文献把它归为 **evaluation instrument invalid ⇒ 所有下游结论在根部就被污染**。

---

### 2.3 【结构化异常处理的理论框架】SHIELDA: Structured Handling of Exceptions in LLM-Driven Agentic Workflows

- **arXiv**: 2508.07935
- **年份**: 2025（v1, 8 月；HTML 显示 2026-08 修订）
- **作者/机构**: Zefan Wang 等（含 Qingsong Wen）；cs.SE
- **方法**: 系统文献综述（1761 候选 → 55 篇入选）+ 真实 agent 异常分析

**【实证/综述结论 — 可直接引用的分类法】**
- 构建了 **36 种异常类型 × 12 种 agent artifact** 的细粒度分类法，按 **Reasoning/Planning 阶段 vs Execution 阶段** 划分。
- 明确指出执行阶段的工具异常类型包括：
  - **Tool Invocation Exception**（工具调用异常）→ 处理模式：Retry with Backoff
  - **Tool Output Exception**（工具**输出**异常）→ 处理模式：**Schema Validation** ← 这正是结构化字段的用武之地
  - **Error Propagation**（错误传播，artifact = Task Flow）→ 处理模式：Abort Task Chain + Rollback
- **作者观点**：现有异常处理"treat exceptions superficially, failing to **trace execution-phase exceptions to their reasoning-phase root causes**"，且恢复逻辑脆弱、缺少结构化升级路径。

> **启示**：文献已经把我们想要的抽象命名了——`Tool Output Exception` 是**独立的一类**，其标准处理策略是 **schema validation**。所以把 success/failure 做成 schema 里的字段，不是我们发明的东西，是文献里的既定 pattern。

---

### 2.4 【"静默错误"问题的开创性工作】Tools Fail: Detecting Silent Errors in Faulty Tools

- **arXiv**: 2406.19228 ／ **EMNLP 2024 main**, pages 14272–14289
- **作者**: Jimin Sun, So Yeon Min, Yingshan Chang, **Yonatan Bisk** — **Carnegie Mellon University**
- **ACL Anthology**: 2024.emnlp-main.790

**【实证结论】**
- 提出工具错误分类法，区分 **input-based errors**（通常伴随 error message）与 **tool-based failures**（多数是 **silent**，即无显式错误信号）。
- 形式化定义：设 `o` 为工具输出、`o*` 为 oracle、`d(·,·)` 为偏差度量、`ε` 为阈值。仅当 `d(o,o*) > ε` 时才需要干预——即 **"错误"是任务相关的，不是二值的**。
- 三个误差来源：`i`(工具输入) / `c`(上下文) / `t_θ`(工具本身)。
- **实验**：受控计算器（broken calculator）+ 多模态具身 agent（ALFRED）。
  - LLM 在**没有显式错误信号**时必须主动 ① 检测失败 ② 归因来源 ③ 规划恢复。
  - 三种 in-context 干预能让模型"学会怀疑工具"。
  - **关键负面发现**：模型的准确率在**故障数为 0 时极低**——"models tend to reject even many acceptable tool outputs where the mistake is not crucial" / "The models seem to understand when the tool is wrong, but **struggles with telling apart task-critical vs tolerable tool mistakes**."

> **启示**：这是**双向误判**（把成功当失败 + 把失败当成功）的经典实证。论文的核心主张是：**不要让模型从原始输出里"猜"是否有错，错误信号应当在产生它的那一层被显式表达出来**。这直接支撑"工具实现层显式声明成功/失败而非消费端子串匹配"。

---

### 2.5 【拒绝误判 / 过度信任】Agents Trust Tools Too Much: Measuring Reliance on Unreliable Tools

- **arXiv**: 2609.05587
- **年份**: 2026
- **发表**: The 1st Workshop on Interpreting Agent Behavior (IAB)
- **规模**: 14 个 LLM × 3 种工具（web search / LLM sub-agent / code executor）

**【实证结论 — 对退出码语义问题极其相关】**
- **Adoption rate**（把被污染的工具返回写入最终答案的比例）：每个工具均值都 >1/3，**web search 高达 68.0%**。
- **Override rate**（在模型本来能答对的题上，工具返回把答案带偏的比例）：Code Executor 上高达 **39.1%**；而同样的数字由 **User 提供时仅 0.2%**、由 **RAG 提供时仅 1.5%**。
- **作者原话**："The route by which information arrives thus matters most for computed outputs, where a **tool return appears to carry authority that the same numerical claim in the prompt does not**."
- 少数最终答案会向用户提示冲突：Search 仅 5.3%，Code Executor 仅 **1.8%**。
- 更严重：**推理 trace 显示模型常常已经识别出冲突、甚至在内部恢复了正确答案，但最终回复仍只呈现被污染的答案且不提及冲突**。
- 干预尝试（用户 prompt / 工具提供方 metadata / post-training）**都不一致有效**；更糟的是"**higher stated reliability can make a corrupted return more persuasive**"。

> **启示（重要）**：**"工具返回"这个通道在模型眼里自带权威性。** 这意味着工具结果里任何**没有被显式标注为失败**的内容，都会被当作事实接受。这正是我们 subprocess 的非零退出码问题的严重性所在——不标注，模型会自信地用错误结果续写。

---

### 2.6 【自省能力基准】CriticTool: Evaluating Self-Critique Capabilities of LLMs in Tool-Calling Error Scenarios

- **arXiv**: 2506.13977
- **年份**: 2025
- **代码**: https://github.com/Shellorley0513/CriticTool

**【实证结论】**
- 系统分析多个主流 tool benchmark（NESTFUL / API-Bank / T-Eval / BFCL）上的错误分布。
- 表 1 显示**模型在工具调用过程中从错误中恢复的能力很差**（"Recover from error" 定义：某步出错后能成功处理），任务越长越明显。
- 错误来源二分：**internal model-driven errors**（工具选择错误、工具幻觉、参数处理）vs **external environment errors**。
- 评估维度相应二分：internal → **reflect and correct**；external → **retry / skip / finish**。
- **作者观点**：现有 benchmark "filter out erroneous data" 或 "treat errors as suboptimal nodes to expand search space"，因此**无法衡量模型如何检测与处理错误**。

> **启示**：`retry / skip / finish` 这个三选一就是模型对工具失败应有的策略空间。要让模型做这个选择，工具结果里必须能区分"这是可重试的瞬时失败"还是"这是不可恢复的永久失败"——**这正是结构化字段能承载、纯文本难以稳定承载的信息**。

---

### 2.7 【基准层面的静态 vs 动态失败】When Tools Fail (ToolMaze)

- **arXiv**: 2606.05806
- **年份**: 2026
- **机构**: 含 Xiang Wang 等；代码 https://github.com/Zhudongsheng75/ToolMaze

**【实证结论】**
- 提出 **2×2 扰动分类法**：**explicit / implicit × transient / permanent**。
  - **explicit failure** 例：网络错误 404 / 429 / timeout——"clearly block execution paths"。
  - **implicit failure** 例：结构合法但语义被污染的返回（如库存延迟导致负数）。
- 270 工具 corpus，DAG 拓扑复杂度 C1–C4，预指定节点注入（非随机）。
- **结果**：扰动使几乎所有模型性能下降，**implicit semantic failure 下降最陡**；
  - **Perturbation Recovery Rate (PRR) 在这些场景下暴跌约 37%**；
  - 复杂拓扑下 agent 陷入 **futile trial-and-error loops**；
  - **"agentic fault-tolerance improves with model scale 3.66× slower than basic task execution"** — 即动态重规划是**独立瓶颈，模型规模化/提示工程解决不了**。
- 新指标：TSR（task success rate）→ PRR + RC（recovery cost），"strictly penalizing inefficient trial-and-error search"。

> **启示**：**(explicit, transient) vs (explicit, permanent) 是最低要求的区分**。grep exit 1 属于"explicit 但语义是正常结果"，与"explicit 且真错误"必须区分开——文献把这整类归为 perturbation taxonomy 的一个轴。

---

### 2.8 【诊断性失败模式】ToolFailBench: Diagnosing Tool-Use Failures in LLM Agents

- **arXiv**: 2607.04686
- **年份**: 2026
- **代码**: https://github.com/SoHarshh/ToolFailBench

**【实证结论】**
- 1,000 任务，5 专业领域（金融/医疗/法律/网络安全/房产），19 个模型。
- 失败模式分类法：**Tool-Skip / Result-Ignore / Output-Fabrication / Unnecessary-Tool-Use**（+ control tasks）。
- 最好模型 only **86.33% Clean Tool-Use Rate** — 未饱和。
- **同参数量级下 Llama-3.1-70B 与 Qwen2.5-72B 在 control-task accuracy 上差 89 个百分点** — 失败行为高度依赖模型族与训练方式，不能靠规模解释。
- **作者观点**：聚合分数掩盖差异——"A model that never calls a needed tool, a model that calls the tool but ignores the result, and a model that calls the tool but invents extra information can all look similar under aggregate evaluation"。

> **启示**：`Result-Ignore`（调用了工具但忽略结果）与 `Output-Fabrication`（编造结果）是**必须靠 trace 级结构化信号才能区分**的失败。**端到端的对/错打分不能定位工具结果处理的问题。**

---

### 2.9 补充：工具接口设计的理论文章

- **The Art of Tool Interface Design**, arXiv:2503.21036, 2025
- 讨论 agentic framework 中需要 "remove/compress distractions and prepares the LLM with contextually relevant information (**tool specifications, instructions, and tool calling results**)"，主张用**显式状态机/状态转移**（如 "CONFIRMED" 状态）替代纯文本流。属**作者观点/工程实践**，非受控实验。

- **LLM-as-Code: Agentic Programming for Agent Harness**, arXiv:2606.15874, 2026
- 主张：**"the interface between agents is a typed return value rather than a free-form conversation, so a failed agent is a failed call that the program may retry, substitute, or abandon under its own rules."**
- 属**作者观点**，但与我们项目"Python 仿 OpenAI SDK 风格"的定位高度一致。

---

## 3. 逐问回答

### Q1: 学术界对"工具调用结果的错误表示与传播"有哪些研究？

形成了四条清晰主线：

1. **异常分类学线**：SHIELDA (2508.07935) — 36 类异常 × 12 artifacts，把 `Tool Output Exception` 独立成类，标准策略 = Schema Validation。系统文献综述，55 篇。
2. **静默错误线**：Tools Fail (2406.19228, EMNLP 2024, CMU) — explicit vs silent 错误的二分，`ε` 阈值形式化。
3. **传播/审计线**：AgentProp-Bench (2604.16706) — 参数错误传播概率 ≈0.62；拒绝与恢复能力统计独立 (ρ=0.041)；伪造工具执行高达 37.5%。
4. **扰动分类线**：ToolMaze (2606.05806) — explicit/implicit × transient/permanent 2×2；PRR 在 implicit 场景跌 37%。

相关的还有 **ToolFailBench (2607.04686)**、**CriticTool (2506.13977)**、以及更晚的 **Characterizing Faults in Agentic AI (2603.06847)**、**From Spark to Fire (2603.04474)**。

### Q2: 有没有论文论证"结构化字段 > 让模型从文本猜"？

**部分有，但要小心归因。** 最直接的配对实验是 **2607.14167**：

- ✅ **有强证据**：反馈必须包含**具体修复信息**（location + observed + **expected alternatives**）而不是仅仅"rejection message"。（+42~44pp）
- ❌ **无证据**：JSON 键值语法本身优于等值自然语言散文。SameNL ≈ TypedFields（差 0–2pp）。
- ⚠️ `LocObs`（有位置有观测、**无替代值**）≈ raw diagnostic 基线 → **光加字段不够，字段里要有可操作信息。**

**其他支持"不要让模型猜"的实证：**
- Tools Fail (2406.19228)：模型在 0 故障时误判率极高 → 让它从原始输出判断"是否有错"是不可靠的。
- Agents Trust Tools Too Much (2609.05587)：工具通道自带权威性，未显式标注失败的内容会被无条件采纳。
- AgentProp-Bench (2604.16706)：子串启发式 vs 人类 κ=0.049。

### Q3: 有没有反例研究说明文本层面的错误信号会导致 agent 行为异常？

**有，至少四类，且都有实验数据：**

| 异常行为 | 证据 | 来源 |
|---|---|---|
| **把成功当失败**（过度拒绝可接受输出） | 故障数=0 时模型判正率极低；"struggles with telling apart task-critical vs tolerable tool mistakes" | Tools Fail 2406.19228 |
| **把失败当成功**（盲目传播被污染值） | P1 adoption 均值 >1/3，Code Executor override 39.1%，仅 1.8% 会告知用户 | Trust Tools Too Much 2609.05587 |
| **重试循环（futile trial-and-error）** | 复杂拓扑下 agent 陷入无效试错；预算 4→8 不加信息则无增益 | ToolMaze 2606.05806 / 2607.14167 |
| **错误归因 / 边界不清** | 拒绝能力与恢复能力统计独立 (ρ=0.041)；同参数量模型 failure profile 差 89pp | AgentProp-Bench 2604.16706 / ToolFailBench 2607.04686 |

另外 2604.16706 的 substring heuristic κ=0.049 就是"文本层面信号不可靠"最干净的量化反例。

### Q4: 对 "exit code 语义" 的研究

**⚠️ 这是文献里最薄弱的一块——没有找到专门研究"agent 如何理解 Unix 退出码"的受控实验论文。** 诚实报告如下：

**学术侧最接近的：**
- **ToolMaze (2606.05806)** 的 perturbation taxonomy 把 `404 / 429 / timeout` 归为 **explicit transient failure**，即"clearly block execution paths"。但它**没有**讨论 exit code 的语义二义性（如 grep 1 = 无匹配）。
- **SHIELDA (2508.07935)** 把 `Tool Invocation Exception` 的处理模式定为 `Retry with Backoff`——**这恰恰是错误示范**：如果所有非零退出码都走 retry，grep 无匹配会导致无意义重试。文献的分类法并未处理"退出码语义取决于具体命令"这一层。
- **CriticTool (2506.13977)** 的 `retry / skip / finish` 三选一策略空间**隐含**了"需要先知道这个失败是可重试的"这一前提。
- **2607.14167** 的核心结论可迁移：**稳定性来自 validator 返回的 `expected` 字段**。对 grep 而言，`expected` 就应该是"exit 1 + 空输出 + 无 stderr = 无匹配，这是一个成功的查询结果"。

**非学术但工程上已被验证的实践证据（可作为"工业界共识"引用，需标注为非同行评审）：**
- 一个真实 issue（earendil-works/pi #3051）完整记录了我们遇到的**同一个 bug**：bash 工具把 grep/diff 的 exit 1 记为 `isError: true`，传播进 agent loop 写入 session JSONL。其修复方案正是本任务要做的：
  - `BashToolDetails` 增加 **`exitCode: number | null`** 字段，注释写明 **"always set; non-zero does not imply isError"**；
  - bash executor 对**语义性 exit 1** 命令 resolve 而非 reject；
  - 判定规则：`grep`/`diff` exit 1 且无真实错误输出 → `isError: false`；exit ≥2，或 exit 1 但带真实错误输出（`No such file`/`Permission denied`/`error TS`）→ `isError: true`；
  - **输出文本（含 `Command exited with code N` 提示）始终原样返回给 LLM，只有 error flag 改变。**
  - 作者的关键架构论点：**"This is the correct layer for this fix: the bash tool itself knows the command and exit code and can make the semantic call locally, before the error propagates into the agent loop."**
- **Lanser-CLI (arXiv:2510.22907, Princeton)** 为 LSP 工具设计了覆盖 15+ 失败模式的详细错误分类法，动机是"a tool that produces false negatives or crashes outright is far more dangerous than one that returns extraneous results"。（此处为二手引用，未读原文）

---

## 4. 对本项目的启示（可执行的决策）

### 4.1 论证要说对（重要）

**不要**用这段话论证：「JSON/结构化字段能提升模型对错误的理解能力」——2607.14167 的配对实验没有支持这一点。

**要用**这两段论证：

1. **正确性论证（最硬）**：子串匹配判断工具成败，是**在消费端重新猜测产生端已知的事实**。文献量化了这类启发式的可靠性：κ=0.049，等同抛硬币（2604.16706）。而且它是**双向失效**的——既会误报失败（Tools Fail: 0 故障时误拒），也会漏报失败（Trust Tools Too Much: 39.1% override 无提示）。
2. **层次论证**：**只有产生结果的那一层知道语义**。bash 工具知道命令行与 exit code，因此能本地做语义判断；消费端只能看到拼接后的字符串。文献对应概念是 SHIELDA 的 `Tool Output Exception` + `Schema Validation`，以及 2607.14167 的 "validator should return what it knows about the failed candidate"。

### 4.2 结构化字段的最小必要设计（有实证支撑）

依据 2607.14167 的消融，字段设计的最小集是：

```python
# 必须有
ok: bool                  # 显式成败，由工具层声明 —— 不要在消费端推断
error_location: str       # 失败位置（如 "args[1]" / "exit_code"）
observed: str             # 观测到的值（如 "exit status 1"）
expected: str             # ⭐ 可接受的替代值 —— 消融显示增益主要来自这里
```

- `expected` 是最容易被忽略但**收益最大**的字段。对 grep：`expected = "exit 1 with empty stdout and no stderr is a valid 'no match' result"`。
- 依据 2606.05806 的 taxonomy，建议再加：
  ```python
  error_class: Literal["explicit_transient", "explicit_permanent", "implicit_semantic", "ok"]
  # 支持 CriticTool 的 retry/skip/finish 三选一决策
  ```
- 依据 2609.05587，"工具的返回自带权威性"，所以 **`ok=False` 时错误信息必须比正常输出更显眼**，不能让模型把 error text 当成数据。

### 4.3 关于 JSON vs 散文

- 2607.14167 显示**格式不是增益来源**。因此选择 JSON 的正当理由应是**程序侧**的：可路由、可日志、可断言、可 `if not result.ok:` 分支、可用于 trace 级评测（2607.04686 的 failure-mode 标注需要结构化 trace）。
- 若在 prompt 渲染层把结构转成人话，**不会损失实证上的修复增益**——这是一个可以在实现中利用的自由度（且往往省 token）。

### 4.4 关于输出文本

工业修复方案（pi#3051）的一个细节值得照抄：**即使 `ok=False`，原始输出文本仍原样返回给 LLM**，只翻转 error flag。因为 2607.14167 证明模型需要 `observed`/`expected` 才能修复；同时不要因为"标记为错误"就丢弃原始数据。

### 4.5 反例证据也说明"别只做半套"

- 只加 `is_error` 而**不加可操作信息**：对照 2607.14167 的 `LocObs`（有位置有观测，无替代值）≈ raw 基线。**半套结构化 ≈ 无效果。**
- 结构化之后**不要指望纯重试用**：预算 4→8 在无新信息时无增益。

---

## 5. 证据强度分级表

| 论文 | 年份 | 关键结论 | 证据类型 | 对我们的支持度 |
|---|---|---|---|---|
| **2607.14167** Structured Feedback Improves Repair | 2026 | 带 location+observed+expected 的反馈 +42~44pp；JSON vs 散文无差异；纯重试无效 | **RCT 式配对实验**（50 游戏 × 4 策略 × 2 模型，880 行，2,652 次 LLM 调用） | ⭐⭐⭐⭐⭐ 支持"结构化"，**反对"JSON 优于散文"** |
| **2604.16706** AgentProp-Bench | 2026 | 子串启发式 κ=0.049（≈随机）；传播概率 0.62；拒绝/恢复独立 ρ=0.041；伪造执行 37.5% | **人类标注校准的大规模实证**（14,750 traces，13 模型，100 人类标注 κ=0.835） | ⭐⭐⭐⭐⭐ 直接反证我们的子串匹配 |
| **2406.19228** Tools Fail (EMNLP 2024) | 2024 | 静默错误须显式表达；模型 0 故障时误判极高；难分 task-critical vs tolerable | **受控实验**（计算器 + ALFRED 具身） | ⭐⭐⭐⭐⭐ 双向误判证据 |
| **2609.05587** Agents Trust Tools Too Much | 2026 | 工具通道自带权威；override 39.1%（vs User 0.2%）；仅 1.8% 告知冲突；干预均不一致有效 | **受控注入实验**（14 模型 × 3 工具） | ⭐⭐⭐⭐⭐ 支持"失败必须显式标注" |
| **2606.05806** ToolMaze | 2026 | explicit/implicit × transient/permanent 2×2；PRR 跌 37%；fault-tolerance 缩放效率低 3.66× | **基准 + 注入实验** | ⭐⭐⭐⭐ 支持错误分类字段 |
| **2508.07935** SHIELDA | 2025 | 36 异常 × 12 artifacts；`Tool Output Exception` → Schema Validation | **SLR（1,761→55 篇）+ 单案例研究** | ⭐⭐⭐⭐ 提供分类法与术语 |
| **2607.04686** ToolFailBench | 2026 | Tool-Skip / Result-Ignore / Output-Fabrication / Unnecessary-Tool-Use；聚合分掩盖差异 | **基准**（1,000 任务，19 模型） | ⭐⭐⭐ 支持 trace 级结构化 |
| **2506.13977** CriticTool | 2025 | 模型错误恢复能力差；retry/skip/finish 策略空间 | **基准** | ⭐⭐⭐ 支持"失败类型可区分"的必要性 |
| **2605.17453** Trust No Tool / **2603.03116** Corrupt Success | 2026 | 不可信工具反馈下的风险；过程感知评测 | 基准 | ⭐⭐ 参考 |
| **2503.21036** Art of Tool Interface Design | 2025 | 工具规格/结果/指令需被压缩组织；显式状态机 | **作者观点/工程实践** | ⭐⭐ 仅作旁证 |
| **2606.15874** LLM-as-Code | 2026 | agent 间接口应是 typed return value 而非自由文本 | **作者观点** | ⭐⭐ 与项目定位共鸣 |
| pi#3051 / Lanser-CLI 2510.22907 | 2026 | exit code ≠ isError；语义判断应在工具层做 | **工业实践（非同行评审）** | ⭐⭐⭐⭐ 我们的 bug 的完整对照案例 |

---

## 6. 已知的调研局限（诚实声明）

1. **未找到专门研究"LLM 理解 Unix 退出码语义"的学术论文。** 这是一个真实的文献空白。§3-Q4 的答案主要由 perturbation taxonomy（间接）和工业 issue（非同行评审）支撑。若需要强学术依据，可能得引用 2606.05806 的 explicit/implicit taxonomy 作为最近的代理。
2. **部分论文（2603.*, 2604.*, 2605.*, 2606.*, 2607.*, 2609.*）年份在一篇 2024–2026 综述的检索窗口内较新**，部分可能是 pre-print、未经充分同行评审；下表已标注证据类型。**2607.14167 的样本偏小**（50 个合成 TextWorld 游戏、两个量化模型、HumanEval 仅 15 题），作者自己也声明"we have not tested repository-scale repair or production agent systems"。
3. **2604.16706 是单作者工作**，人类标注仅 100 条、2 位标注者，作者自陈"a larger, more diverse annotator pool is future work"。
4. 未逐篇精读全文（部分只读了 abstract + intro + conclusion + 关键表格）；2607.14167 和 2609.05587 的中段数据表未完整展开。

---

## 7. 参考链接汇总

- 2607.14167 — https://arxiv.org/abs/2607.14167
- 2604.16706 — https://arxiv.org/abs/2604.16706 ｜ code: https://github.com/bhaskargurram-ai/agenthallu-bench
- 2406.19228 — https://arxiv.org/abs/2406.19228 ｜ https://aclanthology.org/2024.emnlp-main.790
- 2609.05587 — https://arxiv.org/abs/2609.05587
- 2606.05806 — https://arxiv.org/abs/2606.05806 ｜ code: https://github.com/Zhudongsheng75/ToolMaze
- 2508.07935 — https://arxiv.org/abs/2508.07935
- 2607.04686 — https://arxiv.org/abs/2607.04686 ｜ code: https://github.com/SoHarshh/ToolFailBench
- 2506.13977 — https://arxiv.org/abs/2506.13977 ｜ code: https://github.com/Shellorley0513/CriticTool
- 2408.04682 — ToolSandbox (Apple, NAACL 2025) https://arxiv.org/abs/2408.04682
- 2603.06847 — Characterizing Faults in Agentic AI
- 2603.04474 — From Spark to Fire: Error Cascades in LLM-MAS
- 2605.17453 — Trust No Tool (TRUST-Bench)
- 2603.03116 — Beyond Task Completion: Corrupt Success
- 2503.21036 — The Art of Tool Interface Design
- 2606.15874 — LLM-as-Code: Agentic Programming for Agent Harness
- 2603.12011 — Can RL Improve Generalization of LLM Agents?（observation space shift 相关）
- 2510.22907 — Lanser-CLI (Princeton, LSP error taxonomy，二手引用)
- 工业对照：https://github.com/earendil-works/pi/issues/3051
