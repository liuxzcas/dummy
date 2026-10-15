# Phase 3 Step 1 验证报告 — 技能机制基础

> 日期:2026-08-17
> 范围:skills/ 目录 + skills_manager.py + core._inject_skills + /skills 命令
> 方法:三层验证(pytest 套件 / 定向 hermes-verify / 真实目录解析)
> EN TL;DR: Step 1 (skills foundation) verification — 82 pytest
> green (10 new skill tests + 72 existing), 17 targeted checks
> (CRUD/index injection/commands), real-directory parse of 4 SKILL.md.

---

## 1. 验证对象与改动

| 文件 | 改动 | 验证重点 |
|------|------|---------|
| `skills/` 4 个 SKILL.md | 新建(3 原子 + 1 工作流) | frontmatter 解析、工作流 steps 引用 |
| `skills_manager.py` | 新建 | CRUD、索引构建、容错 |
| `core.py` | `_inject_skills()` | system 注入、幂等、空库跳过 |
| `main.py` | `/skills` 命令 + /help | list/show/del、错误处理 |
| `tests/test_skills.py` | 新建 10 项 | 上述行为的自动化断言 |
| `tests/test_memory.py` | 修正 test_inject_char_cap | 记忆段范围(止于技能索引前) |

## 2. 验证清单(逐项)

### 2.1 skills_manager(CRUD 与索引)

| # | 验证项 | 方法 | 结果 |
|---|--------|------|------|
| 1 | 扫描 4 个技能(3 原子 + 1 工作流) | list_skills 真实目录 | ✅ |
| 2 | frontmatter 解析(name/description/type) | 断言字段值 | ✅ |
| 3 | 无 frontmatter 的技能目录跳过 | bad-skill 用例(隔离目录) | ✅ |
| 4 | 加载 SKILL.md 全文 | load_skill 断言含步骤正文 | ✅ |
| 5 | 技能不存在返回 None | load_skill("nope") | ✅ |
| 6 | 删除技能目录 | delete_skill 后 load 为 None | ✅ |
| 7 | 删除不存在返回 False | delete_skill("nope") | ✅ |
| 8 | 索引格式:名称+描述+read_file 指引 | build_skills_index 断言 | ✅ |
| 9 | 工作流标记(literature-review [工作流]) | 索引含标记 | ✅ |
| 10 | 空技能库返回空串 | 隔离空目录 | ✅ |
| 11 | 超上限提示(>15 技能) | 代码路径检查 | ✅(静态) |
| 12 | 工作流 steps 引用完整 | load 后断言含 3 个原子技能名 | ✅ |

### 2.2 core._inject_skills(system 注入)

| # | 验证项 | 方法 | 结果 |
|---|--------|------|------|
| 13 | 索引注入 system prompt | 断言 history[0] 含 "## 可用技能" + 技能名 | ✅ |
| 14 | 原 system 内容保留 | 断言前缀保留 | ✅ |
| 15 | 幂等(重复注入不叠加) | 两次注入后 count == 1 | ✅ |
| 16 | 空技能库不动 system | 隔离空目录,content 不变 | ✅ |

### 2.3 /skills 命令

| # | 验证项 | 方法 | 结果 |
|---|--------|------|------|
| 17 | /skills 列表(含数量/工作流标记) | handle_skills_command 断言 | ✅ |
| 18 | /skills show 显示全文 | 断言含技能正文 | ✅ |
| 19 | /skills show 不存在提示 | 断言 "不存在" | ✅ |
| 20 | /skills del 不存在提示 | 断言 "不存在" | ✅ |
| 21 | /skills del 无参数用法提示 | 断言 "用法" | ✅ |

### 2.4 真实目录解析(端到端)

| # | 验证项 | 结果 |
|---|--------|------|
| 22 | 仓库 skills/ 4 个 SKILL.md 全部正确解析 | ✅ |
| 23 | 索引片段格式(工作流标记/read_file 指引/描述) | ✅ |
| 24 | literature-review 工作流 steps 引用 3 原子技能 | ✅ |

## 3. 测试统计

```
pytest tests/test_skills.py   10 passed(新)
pytest tests/                 82 passed(10 新 + 72 既有)
hermes-verify 定向            17 项(16 直接通过 + 1 项脚本断言修正后通过)
```

## 4. 发现与处理

| 发现 | 性质 | 处理 |
|------|------|------|
| test_inject_char_cap 失败(436 > 420) | 既有测试与新功能冲突:记忆段断言范围包含新增技能索引段 | 修正测试:记忆段范围止于技能索引段(记忆容量语义不变) |
| 定向脚本 1f 断言失败 | 脚本断言字符串位置写反(工作流标记在技能名后) | 修正断言,代码行为正确(索引格式 `- name [工作流]:`) |

## 5. 结论

Step 1 技能机制基础验证通过:技能存储/解析/CRUD、索引注入(幂等、
空库安全)、/skills 命令全路径、既有 72 项测试零回归(1 处范围修正
为功能适配,非行为变更)。可进入 Step 2(技能创建流程)。
