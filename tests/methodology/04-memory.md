# 04 跨会话记忆测试(16 条)— memory.py + core 注入

> 阶段:Phase 2.4 综合测试集 T4(从 ad-hoc 33 项固化核心)
> 测什么:记忆 CRUD、抽取(写入时蒸馏)、三级容错、注入(全量常驻 +
> 历史 FTS 兜底)、旁路不污染

## 存储与检索(4 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_memory_crud | 记忆 CRUD | add/list/delete 全路径 + 字段断言 | /memories 命令的数据基础 |
| test_replace_id_overwrite_commits | replace_id 覆盖提交 | 同事实覆盖 → **commit 生效**(UPDATE 分支) | **真实 bug 抓取点**:UPDATE 分支缺 commit,冲突覆盖静默失效(2.4 抓到) |
| test_hits_increment | hits 计数 | 注入后 hits+1 | hits 是"哪些记忆被用过"的统计(排序与展示) |
| test_memory_searchable_match_and_like | 记忆可搜索(双路径) | MATCH 路 + 2 字 LIKE 路都命中 | 记忆是 FTS 第三源;注入兜底依赖它 |

## 抽取解析(3 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_parse_facts_json_fence_regex | 三级解析 | 标准 JSON/围栏代码块/正则兜底 三种形态都能解析 | LLM 输出格式不稳定;三级容错是生产必须 |
| test_parse_facts_invalid_dropped | 非法丢弃 | 非 JSON/空/字段缺失 → 安全丢弃(返回空) | 解析失败不能崩主流程 |
| test_extractor_write_and_conflict | 抽取写入 + 冲突裁决 | mock LLM 返回事实+replace_id → 新事实写入、冲突覆盖 | **写入时蒸馏核心**:同主题合并、冲突覆盖(方案 4 的写入侧) |

## 容错与事件(2 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_extractor_exception_degrades | 抽取异常降级 | LLM 抛异常 → 返回 (0,0) 不中断 | 抽取是旁路;失败不能影响主对话 |
| test_extractor_event_log | 事件日志 | extract/extract_failed 写入 memory.jsonl | 可观测性承诺(失败可统计,error_type 可分析) |

## 注入(4 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_inject_full_injection | 全量注入 | 记忆全部注入 system("已知事实(记忆):" 段) | **方案 4 定稿**:全量常驻注入(87%→93% 的关键) |
| test_inject_hits_all_injected | 注入即计数 | 注入后所有注入条目的 hits+1 | 统计准确是 /memories 的信任基础 |
| test_inject_no_accumulation_across_chats | 跨轮不累积 | 多次 chat 注入 → 记忆段只出现一次(幂等) | **失败模式:注入重复累积**——system 膨胀 |
| test_inject_char_cap | 容量上限 | 超 400 字符截断,confidence 高优先 | 记忆段容量受控;无限注入 = system 膨胀 |

## 兜底与隔离(3 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_inject_history_fallback | 历史 FTS 兜底 | 抽取丢失的实体 → 问句提炼词 FTS 搜历史注入("相关历史记录:" 段) | **无损兜底层**:抽取波动丢实体时,历史原始片段救回(93% 定稿的关键) |
| test_extract_bypass_no_pollution | 旁路不污染 | 抽取用独立消息,不写入 history | 抽取消息混入 history = 污染对话上下文 |
| test_memories_cli | /memories 命令 | list/del/错误处理 | CLI 是用户管理记忆的唯一入口 |

## 设计背景

记忆体系四方案实验(检索 63% → PlugMem 50% → 全量常驻+蒸馏 87%
→ +历史 FTS 兜底 93%)定稿方案 4;本套件固化其核心契约。
