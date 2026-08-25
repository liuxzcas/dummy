# 01 压缩测试(30 条)— compressor.py

> 阶段:Phase 2.2(上下文压缩,L1 折叠 + L2 增量摘要)
> 测什么:压缩触发/折叠/摘要/容错/结构校验/事实保留

## 配置与结构(8 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_config_defaults | CompressionConfig 默认值 | 断言 window_tokens=128000、threshold_ratio=0.7、enable_l1/l2=True 等 | 配置默认值是全局契约;改默认会连带压缩阈值与窗口显示 |
| test_result_defaults | CompressionResult 默认字段 | 断言 success/strategy_used/error_type 初始值 | 结果对象是压缩流程的对外契约,缺字段会让调用方崩 |
| test_should_compress_boundary | 触发边界 | prompt_tokens 严格大于 阈值(128000×0.7)才触发;等于不触发 | 边界错位会导致"该压不压"(爆窗口)或"不该压乱压"(烧钱) |
| test_circuit_breaker | 断路器计数 | 连续失败达 max_consecutive_failures 后暂停压缩 | 摘要连续失败时不能无限重试烧 token |
| test_l1_fold_and_originals | L1 折叠 + 原文归档 | 长 tool 结果折叠为头200+标记+尾100;原文进 originals | 折叠是压缩主手段;原文归档是"可追溯"承诺(决策 C) |
| test_l2_summarize_structure | L2 摘要结构 | 摘要消息 role=system、带 _meta、内容含早期对话信息 | L2 摘要消息必须结构合法(role/_meta),否则后续 API 调用失败 |
| test_l2_covers_accumulate_and_unique | covers 防重复 | 多次压缩 covers 累加且不重复 | covers 是"这段已摘要"的标记,漏了会重复压缩同一段 |
| test_strip_meta | _meta 剥离 | strip_meta 递归移除 _meta 字段 | DeepSeek 对未知顶层字段严格;_meta 是内部元数据,发 API 前必须剥 |

## 历史结构校验(8 条,参数化)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_validate_history[0-7] | 8 种历史形态合法性 | 参数化 8 组:正常/无 system/孤儿 tool/tool 缺 id/成对 tool 等 → 断言 _validate_history | 结构非法的历史进压缩会产出坏摘要;校验器是"输入卫生"闸门 |

## 事实召回(8 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_recall_basic×7 | 压缩后关键事实保留 | 忠实 mock 摘要器(按规则回填),7 类事实(路径/代码细节/偏好/决定/数字/未完成项/项目名)埋入早期对话 → 压缩 → 断言事实仍在压缩后历史 | **压缩质量核心**:摘要丢事实 = 信息损耗;每类事实一种丢法 |
| test_recall_tool_result_in_tail | 尾部 tool 结果保留 | 关键事实在尾部 tool 结果(recent_turns_keep 内)→ 压缩后原样保留 | L1 只折叠超长结果;尾部轮次必须原样保留(近期上下文优先) |

## 容错(5 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_fault_timeout_degradation | 摘要超时降级 | mock 摘要器抛超时 → 降级 L1-only(success=True + error_type) | 摘要失败不能阻塞对话;降级是"宁可少压不可崩" |
| test_fault_empty_summary | 空摘要处理 | mock 返回空摘要 → 降级/标记,不产出空消息 | 空摘要注入会让 LLM 丢失上下文,必须拦截 |
| test_fault_circuit_breaker_pause | 断路器暂停 | 连续失败超阈值 → 后续不再尝试压缩 | 与 test_circuit_breaker 联动,验证暂停生效 |
| test_fault_invalid_history_rollback | 非法历史回滚 | 构造非法历史 → 压缩失败且原历史不变(原子性) | **纯函数原子性**:压缩绝不原地改坏历史 |
| test_fault_event_contract | 事件日志契约 | 压缩各结果写入 compression.jsonl 事件(error_type 字段) | 事件日志是可观测性承诺(失败可统计) |

## 集成(1 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_compressed_history_passes_schema | 压缩后历史可再入压缩 | 压缩产物再过一次结构校验/流程 | 压缩产物必须是合法的下一轮输入(链式安全) |
