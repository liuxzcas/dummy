# What's New — Phase 3(自主学习)

> 时间跨度:2026-08-17 ~ 2026-08-21(commit `304a508` → `4ee4449`,23 个提交)
> 主题:**从"带记忆的 agent"升级为"会学习的 agent"** — 技能沉淀 +
> 错误学习 + 自我改进(用户监管)。
> EN TL;DR: Phase 3 (self-learning) shipped: skill system (SKILL.md +
> create-skill meta-skill + /skills), error learning (lessons + instant
> reflection + /lessons), self-improvement loop (detect → propose →
> approve → modify → verify → auto-rollback, /improve), environment-fact
> injection (datetime / cwd / skill dir), window 128K + local-endpoint
> stats, diff colorization.

---

## A. 技能系统(Step 1 + Step 2)— 流程固化与复用

| 功能 | 说明 |
|------|------|
| SKILL.md 技能格式 | Anthropic 标准:YAML frontmatter(name/description/type)+ 步骤正文;**原子技能 + 工作流技能**(工作流 steps 引用原子技能,轻量编排) |
| 技能索引注入 system | 每轮注入技能列表(名称+描述+类型标记),**渐进式加载**:全文由 LLM 按需 read_file 读取 |
| `/skills` 命令 | 列出 / show <name> / del <name>(技能在 skills/ 目录,git 可回退) |
| create-skill 元技能 | **自举闭环**:用户"把 XX 固化为技能" → 索引触发 → 访谈边界 → 生成 SKILL.md → validate_skill 校验 → 保存 |
| validate_skill | 创建后自动校验(frontmatter/name/description/type/workflow 引用) |
| 技能自定位 | 索引注入技能目录绝对路径(运行时计算,部署自适应,LLM 零定位成本) |

提交:2b7889f / f9074d8 / 7cd0162 / faec942 / fb6d10b / 21753c2
预置技能:`create-skill` / `search-literature` / `organize-notes` /
`write-summary` / `literature-review`(文献综述工作流)

## B. 错误学习(Step 3)— 从错误中吸取教训

| 功能 | 说明 |
|------|------|
| lessons 教训库 | 独立表(与记忆分库):规则(do/don't),状态 pending/verified,hits 统计 |
| 即时反思生成 | 用户纠正(强信号检测,防误报)或工具错误(dispatch 特征)触发旁路 LLM 反思 → 一句话规则入库 |
| 按需检索注入 | 每轮按任务相关性 FTS 检索教训注入(≤5 条,命中不足补高频),pending 标"待验证" |
| `/lessons` 命令 | 列出 / confirm <id>(转 verified)/ del <id> |
| 自动转正 | hits ≥ 2 自动 verified;每轮工具错误反思限 1 次(防烧 token) |

提交:f2e28d7(含 FTS 第四索引源 + LIKE 退化分支 lesson 源修复)

## C. 自我改进闭环(Step 4)— 用户监管下改自己

| 功能 | 说明 |
|------|------|
| dispatch 错误统计 | 每工具 calls/errors/最近 3 条错误样本;`get_tool_stats` 暴露 |
| 自动检测报警 | 错误数 ≥3 且错误率 >30% → 会话内提示一次"输入 /improve X 可分析修复" |
| `/improve <工具>` | 提案(LLM 只读根因分析:改动清单+验证计划)→ **用户批准** → 修改 → 验证 |
| 验证门链 | 语法门 → 导入门 → pytest 全量 → 启动门(core/main 改动) |
| 自动回退 | 验证失败 → git checkout + 备份还原(双保险)+ 记教训 + 报告 |
| 安全三层分区 | 自由区(技能/文档)/ 受控区(核心代码,改必须批准)/ 禁止区(.git,永不触碰) |
| 改进记录 | logs/self-improve.jsonl(§3.5 量化评估数据:状态/工具/前后错误率) |
| 手动测试指南 | docs/phase3-step4-manual-test.md(10 组测试项 + 9 步流程) |

提交:9056bbf / 384e0af / f08a0d4

## D. 环境事实注入(省 token)

| 功能 | 说明 |
|------|------|
| 当前时间注入 | 每轮刷新 `当前时间: ...`(幂等),省去 date 工具调用 |
| 当前工作目录注入 | cwd 绝对路径 + 相对路径基准说明,LLM 不再猜路径基准 |
| 技能目录注入 | 索引首行技能目录绝对路径,零定位成本 |

提交:c4e8aa7 / 0bdca4c / fb43319

## E. 配置与兼容

| 功能 | 说明 |
|------|------|
| 窗口默认 128K | DEFAULT_WINDOW_TOKENS 全局常量(v4-flash 原生 1M,128K 内性能稳定);压缩阈值与窗口显示共用 |
| 本地部署统计适配 | 本地端点(ollama/vllm)成本显示"本地 (无 API 成本)";模型→窗口映射表(17 个常见本地模型) |
| terminal 用 git-bash | 修复 cmd 语法返工(LLM 按 bash 写,cwd 不认)→ bash -lc 执行;DUMMY_BASH_PATH 可配置,回退链 git-bash → WSL → cmd |

提交:9056bbf 之前(窗口/本地统计/git-bash 在 Phase 3 前已合入,c4e8aa7 前的提交链)

## F. 显示改进

| 功能 | 说明 |
|------|------|
| write_file diff 标色 | 旧内容红、新内容绿(overwrite diff / line 模式预览 / append 预览统一规则) |

提交:619e756 / d4dd316

## G. 文档沉淀

| 文档 | 内容 |
|------|------|
| docs/phase3-research.md | 研究(工程实践 > SOTA)+ 三能力域 + 安全保障 §3.4 + 量化评估 §3.5 + workspace 机制 §7 + 三库边界 §8 |
| docs/phase3-step1/2-verification.md | Step 1/2 验证报告(方法论+过程+结果) |
| docs/phase3-step4-manual-test.md | Step 4 手动测试指南 |
| docs/discussion-notes.md | 待讨论事项清单(已全部定稿) |
| docs/scratch/architecture-review.md | 架构评估(不重构结论 + 3 项定向重构待办) |

提交:304a508 / 94bf8be / 9505026 / 983533b / fb43319 / f08a0d4 / 4ee4449 等

---

## 里程碑

- **Step 1 技能机制**(2b7889f):存储/索引注入//skills
- **Step 2 技能创建**(faec942):create-skill 自举 + 校验
- **Step 3 错误学习**(f2e28d7):教训生成 + 按需注入
- **Step 4 自我改进**(9056bbf):检测→提案→批准→修改→验证→回退
- 测试套件:**72 → 115 项全绿**(skills 12 + lessons 16 + self_improve 12 + 既有)
