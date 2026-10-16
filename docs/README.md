# dummy 文档导航

> 本目录按**开发阶段**分文件夹，顺序即时间先后。
> 想知道"我们怎么一步步走到现在"，先读 **[开发演进史](./00-overview/development-history.md)**。

---

## 按时间（文件夹顺序）

| 文件夹 | 阶段 | 时间 | 主题 | 文档数 |
|---|---|---|---|---|
| [`00-overview/`](#00-overview) | 总览 | — | 设计总文档、路线图、演进史 | 6 |
| [`02-phase2-memory/`](#02-phase2-memory) | Phase 2 | 07-28 ~ 08-13 | 会话持久化 / Context 压缩 / FTS 搜索 / 跨 session 记忆 | 7 |
| [`03-phase3-learning/`](#03-phase3-learning) | Phase 3 | 08-13 ~ 08-25 | 技能系统 / 错误学习 / 自我改进闭环 | 4 |
| [`04-hardening/`](#04-hardening) | **架构反转期** ★ | 09-03 ~ 09-16 | 判断权还给模型、历史层选型、冲突审计 | 5 |
| [`05-cross-platform/`](#05-cross-platform) | 跨平台期 | 09-17 ~ 10-14 | 输出层 / 环境探测 / terminal 超时 / 并发打断调研 | 3 |
| [`90-research/`](#90-research) | 调研 | 跨阶段 | 记忆系统调研、工具结果调研 | 7 |
| [`91-experiences/`](#91-experiences) | 复盘 | 跨阶段 | STAR 复盘、验证记录 | 3 |
| [`99-scratch/`](#99-scratch) | 草稿 | 跨阶段 | 讨论记录、架构评估 | 4 |

**注**：没有 `01-phase0-1/` —— Phase 0/1 的文档极少（原型期的设计文档已归入 `00-overview/design-phase0*.html`）。

---

## 按主题

| 想找什么 | 去哪 |
|---|---|
| **总体设计**（判断的归属、四层架构） | `00-overview/design.html`（中文 `design-zh.html`） |
| **路线图 / 后续计划** | `00-overview/roadmap.md` |
| **开发脉络**（为什么这么走） | `00-overview/development-history.md` |
| 上下文压缩怎么做 | `02-phase2-memory/context-compression.md` + `phase2.2_plan.md` |
| 记忆系统 | `02-phase2-memory/memory-system.md`、`cross-session-memory.md` |
| 搜索（FTS5） | `02-phase2-memory/fts-search.md` |
| 技能系统 / 教训 / 自我改进 | `03-phase3-learning/phase3-research.md` |
| **为什么删掉验证门、护栏为什么退成观察者** | `04-hardening/` + `91-experiences/verification-gate-rollback-star.md` ★ |
| 历史层为什么选"单点收口" | `04-hardening/arch-history-experiment.md` |
| terminal 超时/卡死 | `05-cross-platform/terminal-timeout-fix-plan.md`（技术）+ `terminal-timeout-explained-simply.md`（通俗） |
| **残留进程报告**（含已排除方案的实测） | `05-cross-platform/lingering-process-report.md` |
| 工具并发 / 打断各家怎么做 | `05-cross-platform/agent-concurrency-interrupt-survey.md` |
| 打断语义改进（**已实施**） | `04-hardening/interrupt-semantics-plan.md` |
| 记忆系统外部调研 | `90-research/memory-research.md`、`agent-memory-report.html` |
| 工具结果该怎么结构化 | `90-research/tool-result-*.md`、`research-structured-tool-results.md` |

---

## 各文件夹说明

### 00-overview
**看什么**：项目的"是什么"和"往哪去"。

- `development-history.md` —— **演进史**（推荐先读这个）
- `design.html` / `design-zh.html` —— 总设计文档（中英双版，含 §2「判断的归属」）
- `roadmap.md` —— 路线图（Phase 划分与决策点）
- `design-phase0.html` / `design-phase0-zh.html` —— 最初的原型设计（已归档）

### 02-phase2-memory
**看什么**：怎么让对话"记得住、装得下、找得回"。

- `phase2.2_plan.md` —— 压缩的实施计划（**本项目第一份"先写计划再实施"的文档**）
- `context-compression.md` —— 压缩机制说明
- `phase2-test-suite.md` —— Phase 2 测试集
- `memory-system.md` / `cross-session-memory.md` —— 记忆系统（含方案对照 19/30 → 28/30）
- `memory-persistence.md` —— 持久化
- `fts-search.md` —— FTS5 全文搜索

### 03-phase3-learning
**看什么**：怎么让它"越用越好"。

- `phase3-research.md` —— 完整研究文档（技能/教训/自我改进 + 决策点 D3/D4/D5）
- `phase3-step1-verification.md` —— 技能机制验证报告
- `phase3-step2-verification.md` —— 技能创建验证报告
- `phase3-step4-manual-test.md` —— 自我改进手动测试指南

### 04-hardening ★
**看什么**：**全项目最重要的转折** —— 代码从"替模型判断"退回"只提供事实"。

- `p1b-verification-design.md` —— 验证门的原始设计（**注意：结论已被推翻**，见演进史阶段 4）
- `arch-history-experiment.md` —— 历史层四路并行对照的实验结论
- `cross-component-audit.md` —— 跨组件冲突审计（修掉 4 处互相矛盾的设计）
- `hermes-vs-dummy-tool-loop.md` —— 与 Hermes 的循环机制对照
- `interrupt-semantics-plan.md` —— **打断语义改进方案（待实施，含一个真 bug）**

### 05-cross-platform
**看什么**：跨平台适配 + 输出层 + 实测发现的实现缺陷。

- `terminal-timeout-fix-plan.md` —— terminal 超时失效的诊断与修复（**含 10 个实测实验**）
- `terminal-timeout-explained-simply.md` —— 同上的通俗版（工人/水管/小工）
- `agent-concurrency-interrupt-survey.md` —— 8 个系统的并发/打断机制横向调研

### 90-research
**看什么**：外部调研（不是 dummy 自己的设计，而是"别人怎么做的"）。

- `memory-research.md` —— Agent 记忆系统调研（多层架构 + 论文）
- `agent-memory-report.html` —— 深度报告网页版（15 项工作逐一拆解）
- `knowledge-graph.html` —— 知识图谱
- `tool-result-success-failure-survey.md` —— 工具结果"成功/失败"判定调研
- `tool-result-structuring-design.md` —— 结构化设计
- `research-structured-tool-results.md` —— 结构化工具结果调研
- `agent-memory-report.mobile.html` —— 移动版（**未跟踪文件**）

### 91-experiences
**看什么**：复盘 —— 做对了什么、做错了什么。

- `verification-gate-rollback-star.md` —— ★ **验证门/护栏被推翻的完整 STAR 复盘**（六条教训）
- `tool-interrupt-star.md` —— 工具打断功能开发复盘
- `verification-log.md` —— 关键验证记录

### 99-scratch
**看什么**：过程中的讨论与评估草稿（未成定论）。

- `architecture-review.md` —— 架构评估（耦合/内聚审查）
- `discussion-notes.md` —— 待讨论事项
- `intent-recognition-eval.md` / `web-search-rework-eval.md` —— 评估记录

---

## 关于路径变更（2026-10-14）

**本目录的文件曾全部平铺在 `docs/` 根下**，2026-10-14 按开发阶段重新归类到子文件夹。

- `git` 历史保留完整（用 `git mv`，可 `git log --follow <file>` 追溯）
- 仓库 README / README_CN / WHATS-NEW 里的路径引用已同步更新
- **如果看到旧路径**（如 `docs/phase3-research.md`），对应新位置是 `docs/03-phase3-learning/phase3-research.md`

---

## 关于 `skills-drafts/`

**未跟踪的草稿目录**（不进 git），存技能相关的临时草稿。与主文档体系无关。

---

## 文档之间的引用关系

```
development-history.md (演进史)
    │  串起所有阶段
    ├─→ 00-overview/design.html         (当前架构的权威描述)
    ├─→ 04-hardening/                   (转折期的原始材料)
    │      └─→ 91-experiences/verification-gate-rollback-star.md  (为什么推翻)
    └─→ 05-cross-platform/              (跨平台期的实测记录)

roadmap.md (路线图)
    │  记录 Phase 划分 + 已定/待定决策
    └─→ 各阶段的 research / plan 文档
```

**维护约定**：新增文档放进**对应阶段的文件夹**；如果是跨阶段的外部调研，放 `90-research/`；过程性草稿放 `99-scratch/`。
