# Agent 记忆系统调研:多层架构 + 论文罗列

> EN TL;DR: This doc records a research pass on agent memory systems (Aug 2026).
> Part 1 verifies Hermes Agent's actual memory architecture against its official
> docs and the local installation: three layers — durable memory (MEMORY.md +
> USER.md, injected every turn), procedural skills (SKILL.md, loaded on demand),
> and session search (state.db SQLite + dual FTS5). Part 2 surveys the paper
> landscape (PlugMem / MemGPT / Mem0 / Zep-Graphiti / HippoRAG / A-MEM / CoALA /
> LongMemEval ...) grouped by approach family, with pros/cons and applicable
> scenarios. Part 3 maps each finding to dummy-agent's roadmap (Phase 2.2
> compression, 2.3b FTS search, 2.4 cross-session memory, Phase 3 learning).

## 1. Hermes 三层记忆(已核实,含本机实证)

### 1.1 结论:命名纠正

社区流传的"对话记忆 / 长期记忆 / 工具记忆"说法方向对,但官方命名不同:

| 流传说法 | 官方名称 | 存储 | 访问模式 |
|---------|---------|------|---------|
| 长期记忆 | durable memory 持久记忆 | `MEMORY.md`(2200 字)+ `USER.md`(1375 字),位于 `~/.hermes/memories/` | 每轮注入 system prompt |
| 工具记忆 | procedural skills 程序性记忆 | `~/.hermes/skills/` 下每个技能一个 `SKILL.md` | 只在相关时加载全文 |
| 对话记忆 | session search 会话搜索 | `state.db`(SQLite + 双 FTS5 索引) | 按需检索,不占上下文 |

"工具记忆"严格说应是"程序性记忆"——skills 存的是"怎么做一件事的流程"
(工具使用步骤、坑、验证方法),确实管着工具的使用知识,实质理解没错。

### 1.2 为什么要分多层

认知科学 + 工程成本两层理由:

1. **认知对应**(PlugMem 的 MSR blog 也引用同一区分):人脑分"记住事件"
   (episodic)、"知道事实"(semantic)、"知道怎么做"(procedural)。三种信息的
   读写频率、容量、过期速度完全不同,塞进一个结构互相拖累。
2. **CoALA 框架**(arXiv:2309.02427,TMLR)的标准分类:
   working memory(当前对话上下文)/ episodic(过往会话)/ semantic(事实)/
   procedural(流程)。Hermes 三层一一对应。
3. **工程上最硬的理由:成本与注意力**。
   - 上下文窗口有限,token 要钱;全部历史/知识塞进提示词 → 窗口爆炸 +
     注意力稀释(lost in the middle)
   - 核心原则:区分"每轮都要看的"vs"用到才看的"——
     事实小而稳 → 常驻;流程大而详 → 相关才加载;历史无限增长 → 按需检索
   - 每层有自己的容量上限、生命周期、一致性要求,分开才能各自管理

### 1.3 具体实现(本机实证)

**第 1 层 持久记忆**
- 两个 markdown 文件,条目用 `§` 分隔(本机 `memories/MEMORY.md` 实测确认)
- 会话开始时读盘,以"冻结快照"注入 system prompt,带使用率标题
  (如 `[79% — 1,749/2,200 chars]`);会话中途不变——故意为之,保 prefix cache
- `memory` 工具:add / replace / remove,子串匹配定位条目,支持原子批量操作;
  写入立即落盘,但系统提示里的快照下次会话才生效
- 超限不自动压缩,直接报错,由 agent 自己合并/删除
  (源码 `tools/memory_tool.py::MemoryStore`,限额来自 config,
  `agent/agent_init.py:1356` "Persistent memory (MEMORY.md + USER.md) -- loaded from disk")
- 安全扫描(注入/外泄模式)+ 自动去重
- 可选外部 provider(Honcho、Mem0、Hindsight、Holographic、RetainDB、
  ByteRover、Supermemory、OpenViking)并行增强,不替换内置

**第 2 层 程序性记忆**
- 每个技能一个 `SKILL.md`(YAML frontmatter + markdown 正文)
- 系统提示里只列"技能名 + 一句话描述"(索引);agent 判断相关时用
  `skill_view` 加载全文——"大而详细但不常驻"的实现手段
- `skill_manage` 创建/修补;curator 后台管理生命周期
  (usage 统计、过期归档、备份、consolidate 合并)
- 本机已装 80 个技能,按 github/creative/mlops 等分类

**第 3 层 会话搜索**
- 所有会话存 `state.db`:`sessions` + `messages` 两张表
- 关键细节:**双 FTS5 索引**——`messages_fts`(标准分词)+
  `messages_fts_trigram`(trigram 分词,专为中文)——因为标准 FTS5 对中文
  只能整句匹配。这是中文全文搜索的现成答案
- `session_search` 工具三种调用形态(discovery 搜索 / scroll 翻页 / browse
  浏览),纯 SQLite 检索,无 LLM 开销,返回真实消息原文
- 另有 `compression_locks` 表(多会话压缩协调)和 `session_model_usage` 表
  (token 记账)——context compression 在 Hermes 里是系统级组件

## 2. 论文罗列(按派系)

### 派系 1:操作系统式分层内存

核心思想:内存分等级,agent 自己换页。

| 工作 | 机制 | 优点 | 缺点 | 适用场景 |
|------|------|------|------|---------|
| MemGPT/Letta(arXiv:2310.08560,Berkeley) | 模仿 OS:core memory 常驻 + archival memory 按需检索,agent 用工具自编辑内存 | 思想奠基、分层清晰、自编辑 | 重:内存管理本身是一套工具调用 | 长程自主 agent、研究原型 |

### 派系 2:检索增强的原始记忆

存原文/向量,retrieval 决定给什么。

| 工作 | 机制 | 优点 | 缺点 | 适用场景 |
|------|------|------|------|---------|
| MemoryBank(2023,arXiv:2310.00710) | 记忆库 + LLM 决定忘记/更新,SIL 摘要集成 | 简单直接 | 召回依赖 embedding,无结构 | 聊天机器人长期陪伴 |
| Generative Agents(2023,arXiv:2304.03442,Stanford) | 记忆流 + 检索打分(recency×importance×relevance)+ 反思 | 范式级工作 | 打分启发式、贵、反思频率难调 | 模拟/角色扮演 |
| L-MEM / MemInsight(Google,用于 Gemini) | LLM 构造结构化记忆(人物画像+情景),存向量库 | 生产验证 | 与 Gemini 耦合,通用性一般 | 大厂助手 |
| Wormhole Memory(2025) | 跨对话检索,存"对话摘要+检索信号" | 轻量 | 新,生态小 | 多轮跨会话问答 |

### 派系 3:结构化知识记忆

把记忆建成图/笔记/知识单元。

| 工作 | 机制 | 优点 | 缺点 | 适用场景 |
|------|------|------|------|---------|
| Zep/Graphiti(arXiv:2501.13956) | 时序知识图谱:实体+关系+时间边 | 时序推理强、变更追踪 | 抽取成本高、基建重、中文实体抽取要调 | 企业助手、需时序推理 |
| HippoRAG(arXiv:2405.14831,NeurIPS 24) | 海马体索引理论:LLM 抽三元组→KG,Personalized PageRank 多跳检索;HippoRAG 2 加 schema 抽象 | 多跳检索强,比 IRCoT 便宜 10-20 倍、快 6-13 倍 | 建图成本;偏"知识库检索"而非"对话记忆" | 大规模多跳 QA |
| A-MEM(arXiv:2502.12110,NeurIPS 25) | Zettelkasten 笔记法:每条记忆一张笔记,LLM 自主决定链接和演化 | 任务无关、自适应、多跳推理提升 | 笔记质量依赖 LLM、链接/演化开销、难调试 | 研究型长期记忆 |
| PlugMem(arXiv:2603.03296,ICML 26) | 见 2.1 详述 | | | |

### 派系 4:记忆即 API / 产品层(做跨 Session 记忆最该参考)

| 工作 | 机制 | 优点 | 缺点 | 适用场景 |
|------|------|------|------|---------|
| Mem0(arXiv:2504.19413) | 两阶段:抽取事实 + 更新/冲突处理,干净的内存 API | 集成容易、LongMemEval 成绩好、~60K stars | 写入要调 LLM、黑盒 | 生产助手快速接入 |
| LangMem / LangGraph Store | LangChain 生态记忆 SDK | 生态绑定 | 与 LangGraph 耦合 | LangChain 用户 |
| Claude / OpenAI 内置记忆 | 厂商级事实记忆 | 零配置 | 不可移植 | 用官方客户端 |

### 派系 5:权重级记忆(API 用户不可用,了解即可)

MemoryLLM / M+ / BMAS 等:把知识训进模型或加记忆模块。
优点:推理时不需要检索;缺点:要训练,不适用 API 场景。

### 理论框架与评测

- **CoALA**(arXiv:2309.02427):分类学,读论文前先读它,给坐标系
- **LongMemEval**(arXiv:2410.10813,ICLR 25):500 题,5 类任务
  (信息抽取/多会话推理/时序推理/知识更新/弃答),报告持续交互掉 ~30%
  ——记忆系统事实上的评测标准
- **LOCOMO**(arXiv:2402.17753):对话式记忆基准
- **TsinghuaC3I/Awesome-Memory-for-Agents**:持续更新的论文清单

### 2.1 PlugMem 详述(ICML 2026,Microsoft Research × UIUC)

- **核心转变**:传统记忆存"文本块/实体",它存"知识单元"——命题性知识
  (事实)+ 规范性知识(可复用技能),组织成记忆图
- **三个组件**:
  - Structure:原始交互 → 知识单元 → 记忆图
  - Retrieval:用高层概念/推断意图做路由信号,而非检索长文本
  - Reasoning:检索结果蒸馏成任务导向的简短指导再进上下文
- **成绩**:LongMemEval 90.2 Acc、HotpotQA 79.1 F1 / 91.1% LLM-Judge,
  均为 SOTA;提出"信息密度"(有用信息 / 消耗上下文)作为统一度量
- **优点**:任务无关、即插即用(宣称 6 行代码)、信息密度高、可叠加任务特化
- **缺点**:2026 新工作生态小;三组件管线较重,每步要 LLM;最佳成绩需
  轻量任务适配;抽取质量是天花板
- **适用**:任意 agent 的通用记忆底座,长程决策场景

## 3. 对 dummy-agent 的借鉴(对应 roadmap)

| Roadmap 阶段 | 借鉴来源 | 具体做法 |
|-------------|---------|---------|
| Phase 2.2 压缩 | Hermes compression 组件 | working memory 管理;参考 `compression_locks`(并发压缩协调)与阈值触发 |
| Phase 2.3b FTS 搜索 | Hermes 第 3 层 | **中文搜索直接上 FTS5 trigram tokenizer**,别用默认分词(标准 FTS5 对中文只能整句匹配) |
| Phase 2.4 跨 Session 记忆 | Hermes 第 1 层 + PlugMem/Mem0 | 别存全文:事实抽取 → 结构化条目 → 按需注入,即 PlugMem 知识单元的低配版、Mem0 两阶段法的本质 |
| Phase 3 自主学习 | Hermes skills | "工具使用模式学习"= 程序性记忆,直接对应 skills 机制 |

一句话总结:三层记忆 = 每轮都看的事实 + 用到才看的流程 + 按需检索的历史,
分层的唯一目的就是让上下文里永远只有当前决策需要的东西——这也是 PlugMem
用"信息密度"当度量想表达的事。

## 4. 参考

- Hermes 官方文档:Persistent Memory
  https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
- PlugMem: https://arxiv.org/abs/2603.03296 | GitHub: TIMAN-group/PlugMem
  | MSR Blog(2026-03): From Raw Interaction to Reusable Knowledge
- MemGPT: arXiv:2310.08560 | Mem0: arXiv:2504.19413 | Zep/Graphiti: arXiv:2501.13956
- HippoRAG: arXiv:2405.14831 | A-MEM: arXiv:2502.12110 | CoALA: arXiv:2309.02427
- LongMemEval: arXiv:2410.10813 | LOCOMO: arXiv:2402.17753
- MemoryBank: arXiv:2310.00710 | Generative Agents: arXiv:2304.03442
- Awesome-Memory-for-Agents: https://github.com/TsinghuaC3I/Awesome-Memory-for-Agents

> 调研日期:2026-08-05。论文领域迭代快,引用前建议核对最新版本。
