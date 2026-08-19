# Phase 3 研究文档 — 自主学习(Skills + 错误学习 + 自我改进)

> 日期:2026-08-17
> 状态:研究阶段(实施前定稿)
> EN TL;DR: Research for Phase 3 (self-learning): skills (Anthropic
> SKILL.md standard, atomic design, progressive loading), error learning
> (Reflexion / ExpeL papers + Claude Code's CLAUDE.md self-improvement
> loop as the proven engineering practice), self-improvement (detect →
> root-cause → propose → user-approve → modify → verify, reusing our
> existing confirmation & test infrastructure).

---

## 0. 方法论:工程实践 > SOTA 论文

Phase 2.2 的实证:PlugMem 严格按论文实现 15/30,而 Hermes 工程实践方案
(全量常驻 + 写入时蒸馏)87%→93%。**论文给方向,工程实践给答案**——
本研究的每个设计点都以"口碑好的工程实践案例"为主要依据,论文作为
学术框架参考。

---

## 1. 能力域一:技能沉淀(Skills)

### 1.1 工程实践基准(Anthropic Agent Skills,已事实标准化)

SKILL.md 格式已是跨平台事实标准(Claude Code / Codex / Cursor / OpenCode /
Copilot 全部采用,目录各异)。核心设计:

| 设计点 | 实践结论 | 对 dummy 的映射 |
|--------|---------|----------------|
| **原子化原则** | 一个 Skill 只做一件事;多步骤流程拆成多个原子 Skill,用 AGENTS.md 编排——❌ 把"需求分析→代码→测试→文档"塞进一个 Skill | 技能粒度:如"文献综述"拆为「检索」/「整理」/「成稿」?或保持单技能带步骤清单(视复杂度) |
| **渐进式加载** | 元数据仅 ~50 tokens 常驻,完整文档按需加载 ~500 tokens;上下文重复设置减少 ~30% | system prompt 注入**技能索引**(名称+描述,一行一个),用工具/命令读全文 |
| **描述决定触发** | description 必须写明"何时用/何时不用",有专门的描述优化器 | 技能索引的描述质量直接决定 dummy 是否正确触发 |
| **数量上限** | 建议 10-15 个常用技能,过多增加选择负担 | Curator 有存在意义 |
| **先固化后完善** | "不要等想清楚了再写 Skill,先把有效实践固化下来,后续迭代" | 与"小步快跑"一致:有重复流程就固化 v1,再优化 |
| **分层文档** | SKILL.md 简洁(主流程)+ reference.md/examples/ 扩展;引用保持一级深度 | 技能目录:SKILL.md + 可选扩展文件 |

### 1.2 Skill Creator 工作流(元技能)

官方 skill-creator 流程:明确目标 → 访谈边界 → 生成 SKILL.md → 测试评估
→ 迭代 → 描述优化。→ dummy 的"技能创建"子流程可参照:用户说"固化 XX"
→ agent 提问边界 → 生成 SKILL.md → 用测试用例验证。

### 1.3 学术参考

无强相关论文(技能是工程产物);Anthropic 博客 + 社区实践(22.6k stars
agent-skills 仓库)即为权威。

---

## 2. 能力域二:错误学习(Error Learning)

### 2.1 工程实践基准(Claude Code 生态,最直接相关)

**CLAUDE.md 自我改进循环**(被多位资深用户验证的标准做法):
```
Claude 犯错 → 用户说"更新 CLAUDE.md,添加规则防止再犯"
→ Claude 自己写规则 → 下次不再犯
```
- 每次更正后以"更新你的 CLAUDE.md,以便你不会再犯该错误"结尾
- 规则库像代码一样维护(提交 git、团队共享)
- 项目知识超出长度时拆到 notes/,CLAUDE.md 引用

**数据驱动飞轮**(GHA 实践):定期对 agent 运行日志做元分析,找常见错误
模式(bash 错误/权限请求/工程实践不一致)→ 改进 CLAUDE.md / 工具描述。

→ **对 dummy 的映射**:这正是用户举的 terminal 返工例子的形态——错误
发生后生成"教训条目"(规则),注入后续对话。dummy 已有记忆体系,
教训可以走记忆表(新类别)或独立 lessons 表。

### 2.2 学术参考

| 论文 | 核心 | 可迁移点 |
|------|------|---------|
| **Reflexion**(NeurIPS 2023) | 语言强化学习:对反馈信号做语言反思,存 episodic memory buffer,后续尝试引用;HumanEval 91% > GPT-4 80% | "反思文本"的写法:失败后让 LLM 写"为什么失败+下次怎么做"——即教训条目的生成方式 |
| **ExpeL**(AAAI 2024) | 自主收集经验→自然语言抽取知识(insights)→推理时回忆;性能随经验累积一致提升;可迁移 | 抽取/回忆两阶段:与 dummy 记忆体系(写入蒸馏+注入)同构——教训可并入现有蒸馏管线 |

**关键判断**:Reflexion/ExpeL 的框架(dump 反思文本 → 注入)在 dummy 上
可以简化落地——**错误 → 教训条目(蒸馏成一句话规则)→ 注入 system**,
与记忆体系共用机制。论文的"多轮 trial 反思"对交互式 agent 过重,
工程实践(CLAUDE.md 一行规则)更匹配。

---

## 3. 能力域三:自我改进(Self-Improvement)

### 3.1 定义

主动发现自身缺陷(错误模式/返工热点/工具设计问题)→ 生成修复提案 →
**用户监管批准** → 修改自身代码 → 运行验证 → 记录。例:发现 terminal
命令反复报语法错误 → 提案修改 terminal.py → 用户批准 → write_file 改 →
pytest 验证。

### 3.2 学术参考

| 论文/项目 | 核心 | 可迁移点 |
|-----------|------|---------|
| **RepairAgent**(SWE-bench 程序修复) | 状态机模拟人类修复认知步骤;动态 prompt 含当前世界状态/目标/下一步 | 修复流程的步骤化:定位→根因→修复→验证 |
| **SWE-RL** | 自博弈:注入 bug + 修复 bug 训练 | 远期:自我注入缺陷来测试 |
| **Live-SWE-agent**(开源 SOTA) | 运行时自我进化,SWE-Bench Pro 45.8% | 验证了"agent 改自己"的可行性 |

### 3.3 工程实践基准

- **自给自足的循环**(Claude Code 团队建议):让 agent 自动运行构建/测试/
  代码检查验证自己的工作——"创建自给自足的循环"是最重要的建议之一
- **验证反馈循环**:给 agent 反馈循环,质量提升 2-3 倍
- **Human-in-the-loop**:核心功能同步监督,边缘功能自动接受模式
- **LangGraph Self-Healing 教程**:分析 → 生成补丁 → 测试 的显式循环

→ **对 dummy 的映射**:基础设施已就绪——
- 检测:错误事件日志(compression.jsonl 模式)+ 工具返回错误统计
- 提案:LLM 生成修复方案(带根因分析)
- 用户监管:现有确认机制(write_file 确认、/p 打断)天然适配
- 修改:write_file 工具(dummy 改自己代码)
- 验证:72 项 pytest + 质量门
**闭环骨架已有,只差"检测→提案"的编排逻辑。**

---

## 4. 分级实施建议(呼应 Phase 2 Step 模式)

```
Step 1  技能机制基础
        技能目录 + SKILL.md 格式(采用 Anthropic 标准)+ system 注入技能索引
        (~50 tokens)+ /skills 命令(show/list)+ 读全文加载
        验证:索引注入测试 + 用技能执行一次任务

Step 2  技能创建
        手动:"固化 XX"→ 访谈边界 → 生成 SKILL.md(参照 skill-creator 流程)
        半自动(后续):检测重复流程(相似任务 >N 次)提示固化
        验证:创建流程测试 + 真实技能(文献综述 v1)

Step 3  错误学习(教训机制)
        错误 → 教训条目(蒸馏成一句话规则,Reflexion 式反思文本
        + ExpeL 抽取式)→ 注入 system(复用记忆管线,新类别)
        触发:工具报错/返工检测/用户纠正
        验证:注入测试 + 真实场景(terminal 返工 → 教训 → 不再犯)

Step 4  自我改进闭环(用户监管下)
        检测:错误统计(工具失败率/返工次数)超阈值
        → 根因分析(读日志/代码)→ 修复提案(改动清单+验证计划)
        → 用户批准 → write_file 修改 → pytest 验证 → 记录
        安全:只读分析可自动,写代码必须用户批准(复用确认机制)
        验证:端到端演练(人为埋一个可检测缺陷,观察闭环)

Step 5  Curator + 综合验证
        技能/教训/缺陷库管理(/skills del、教训去重、使用统计)
        全链路回归(技能+教训+自我改进)+ 文档
```

---

## 5. 待拍板决策点

| # | 决策 | 选项 |
|---|------|------|
| D1 | 教训存储 | A. 并入记忆体系(新 category="lesson",复用蒸馏/注入)<br>B. 独立 lessons 表(与记忆分开管理) |
| D2 | 教训注入时机 | A. 全量注入(≤400 字符,与记忆同批)<br>B. 按需检索(问句提炼→FTS) |
| D3 | 自我改进的检测源 | A. 工具错误统计(dispatch 层计数)<br>B. 会话日志分析(对话后离线扫描)<br>C. A+B |
| D4 | 自我改进写入权限 | A. 只读分析自动 + 写代码必须用户批准(推荐)<br>B. 用户批准后可自主改(带测试门槛) |
| D5 | 技能粒度 | A. 单技能带步骤清单(简单,优先)<br>B. 原子技能 + 编排(Anthropic 推荐,复杂) |

---

## 6. 编排与多 Agent 前瞻(架构决策,2026-08-17)

> 用户提出:Sub-agent、多 agent 合作、以及按任务自主选择执行模式
> (ReAct / Plan-and-Execute / Reflection)。本节记录架构决策——
> **只定边界,不在 Phase 3 实施**。

### 6.1 模式选择 = 技能系统的一部分(轻量,Phase 3 内落地)

执行模式是"元技能":ReAct(现状:思考→工具→观察循环)/ Plan-and-Execute
(先规划再执行)/ Reflection(执行后反思一轮)。任务进来 → LLM 按技能
描述选择模式技能 → 按模板执行。**不需要新架构,技能系统的触发选择
机制天然承载**。模式技能与领域技能并列,靠 description 区分触发时机。

### 6.2 架构决策:编排层独立新项目,dummy 为执行核心

**不做**:在 dummy 内加编排逻辑(任务分解/子 agent 调度/结果汇合会
污染单 agent 执行循环——职责未分离是"牵一发而动全身"的根源)。

**做**:编排层(orchestrator)作为独立项目,通过 **CLI 子进程**调用 dummy:

```
┌─ 编排层(新项目)────────────────────────────┐
│ 任务分解(planner)/ 子 agent 调度 / 结果汇合   │
│ 模式选择(plan-and-execute 的 plan 阶段)       │
│        │ CLI 子进程(稳定接口约定)            │
└────────▼──────────────────────────────────┘
      dummy(执行核心,保持纯净)
      单 agent ReAct 循环 + 工具 + 记忆 + 技能 + 确认
```

要点:
- 每个子任务 = 一个 dummy 进程/会话(`dummy "子任务" --session <id>`)
- 编排层只依赖 dummy 的 **CLI 稳定接口**,不 import 内部实现
- 双项目彻底解耦:任何一方内部重构不影响另一方(接口约定是唯一契约)
- 多 agent 合作 = 编排层调度多个 dummy 实例,每个实例完整(记忆/技能/确认)

### 6.3 顺序安排(先学习,后编排)

1. **现在**:本节即架构决策记录(不实施)
2. **Phase 3**:技能 / 教训 / 自我改进(单 agent 学习能力)+ 模式技能
3. **Phase 3.5+**:真实使用中出现"单 agent 忙不过来"再启动编排层
   (此时 dummy 已有稳定 CLI 与完整能力)
4. **试水选项**:想提前验证多 agent 价值 → dummy 内做"虚拟子 agent"
   (同一实例开子会话执行子任务再汇合,不碰架构),用真实任务验证
   增量价值,有再上编排层
