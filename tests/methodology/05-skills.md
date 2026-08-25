# 05 技能系统测试(15 条)— skills_manager.py + core 注入

> 阶段:Phase 3 Step 1/2(技能机制基础 + 创建流程)
> 测什么:SKILL.md 解析、CRUD、校验(6 类错误)、索引构建、
> system 注入、datetime/cwd 环境事实注入、/skills 命令

## 技能管理(8 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_list_skills | 目录扫描 + frontmatter 解析 | 隔离目录:正常技能列出、无 frontmatter 的技能跳过 | **失败模式:坏文件混入**——解析失败的技能目录必须静默跳过 |
| test_load_skill | 全文加载 | 读 SKILL.md 全文;不存在返回 None | LLM 按需 read_file 的契约 |
| test_delete_skill | 删除 | 删除目录后 load 为 None;不存在返回 False | /skills del 的数据基础(自由区 git 可回退) |
| test_validate_skill_ok | 校验合法 | 原子 + 工作流(引用存在)都通过 | create-skill 流程第 4 步;不合法不应保存 |
| test_validate_skill_errors | 校验 6 类错误 | 不存在/缺 description/name 不一致/type 非法/缺 steps/引用不存在 | **失败模式全覆盖**:校验逻辑不是"只防一种错" |
| test_build_skills_index | 索引构建 | 名称+描述+[工作流]标记+技能目录绝对路径 | 索引是注入 system 的文本;格式错 = LLM 误读 |
| test_build_skills_index_empty | 空库索引 | 无技能 → 返回空串(不注入) | 空索引注入 = 污染 system |
| test_skills_command_list/show/del | /skills 命令 | list(数量+工作流标记)/ show 全文/ del/不存在/用法 | CLI 是用户管理技能的唯一入口 |

## system 注入(5 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_inject_skills | 技能索引注入 | system 含 "## 可用技能"+ 技能名 + 原内容保留 + 幂等 | 渐进式加载第一环;重复注入 = system 膨胀 |
| test_inject_skills_empty | 空库跳过 | 无技能 → system 不动 | 空注入零成本 |
| test_inject_datetime | 时间注入 | system 含 "当前时间:" 格式正确 + 幂等 | 环境事实注入;省去 date 工具调用(真机发现的 token 浪费) |
| test_inject_datetime_refresh | 时间跨轮刷新 | mock 时钟前移 → 注入值更新 | 跨天会话日期不能过期 |
| test_inject_cwd | 工作目录注入 | system 含 cwd 绝对路径 + 相对路径基准说明 + 幂等 | **workspace §7 第一级**:LLM 不再猜路径基准(真机发现的困惑) |

## 设计背景

- 技能格式:Anthropic SKILL.md 标准(D5=B 原子+工作流)
- create-skill 元技能自举:用户"固化 XX" → 索引触发 → 访谈 → 生成
  → validate_skill 校验 → 保存
- 环境事实注入(时间/cwd/技能目录)与技能索引同模式:运行时计算、
  幂等、每轮刷新
