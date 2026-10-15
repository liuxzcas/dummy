# 待讨论事项(Discussion Notes)

> 会话中提出的、暂不实施的设计讨论点,集中记录避免遗忘。
> 讨论定稿后移入 roadmap 或设计文档。

## 2026-08-21

### 1. ~~运行时 workspace 路径与环境选择~~(定稿 2026-08-21,phase3-research.md §7)

**话题**:agent 运行时,能否让用户选择一个 workspace 路径和工作环境?

**结论**:行业调研(Claude Code --add-dir / Codex AGENTS 发现链 / Trae / Hermes)显示**无会话中切换工作区的主流做法**。定稿两级方案:
- 第一级:当前工作目录注入 system(已实现 0bdca4c,LLM 不再猜路径基准)
- 第二级:add-dir 式扩展(可选后续)
- 明确不做:多项目独立记忆/技能

### 2. ~~write_file diff 新旧内容标色~~(已完成 2026-08-21,d4dd316)

**话题**:write_file 的 _show_diff 预览中,旧内容(-)标红、新内容(+)标绿?

**初步评估**:改动很小——diff 行按前缀 +/- 用 colors 的 RED/GREEN 着色(复用现有 colors 系统,DUMMY_COLOR=0 自动禁用);不影响确认逻辑与测试(着色是显示层)。

**结论**:已实施并推送(d4dd316):overwrite diff、line 模式确认预览/修改后展示、append 预览统一标色(旧红新绿)。
