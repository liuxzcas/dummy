# 06 错误学习测试(16 条)— lessons.py + core + 存储

> 阶段:Phase 3 Step 3(教训机制:生成 + 按需注入)
> 测什么:lessons 表 CRUD、信号检测、反思生成(三级解析)、
> 注入(≤5 检索+保底)、触发(纠正/工具错误+限流)、/lessons 命令

## 存储(5 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_lesson_crud | 教训 CRUD | add/list/confirm/delete 全路径 + 状态断言 | /lessons 命令数据基础;confirm 是验证机制 |
| test_lesson_hits_auto_verify | hits 自动转正 | hits=1 仍 pending;hits=2 自动 verified | **自动转正设计**:多次命中 = 实践验证有效 |
| test_lesson_status_filter | 状态过滤 | pending/verified 分查 | 管理/统计需要按状态过滤 |
| test_delete_session_cascades_lessons | 级联删除 | 删会话 → 教训同删 | 会话删除的完整性(四表级联 + lessons) |
| test_correction_signal | 纠正信号检测 | 强词("错了/不对/重来")命中;日常话("帮我/今天天气")不命中 | **防误报**:弱词(应该/不是)不触发,避免每次对话都烧反思 token |

## 反思生成(5 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_tool_error_markers | 错误特征 | [ToolDispatch]/[错误]/[EXIT CODE] 识别;正常输出不识别 | dispatch 返回串的错误判定(Step 3 触发源) |
| test_generate_lesson_valid_json | 标准 JSON 解析 | 合法 JSON → lesson/category 提取 | Reflexion 式反思输出的主通道 |
| test_generate_lesson_fenced | 围栏 JSON 解析 | ```json 围栏 → 提取 | LLM 爱包代码块;第二级容错 |
| test_generate_lesson_invalid_returns_empty | 非法返回空 | 非 JSON/空 → [] | 解析失败降级,不中断 |
| test_generate_lesson_exception_returns_empty | 异常返回空 | LLM 抛异常 → [] | 旁路调用失败无害 |

## 注入(3 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_inject_lessons | 按需检索注入 | FTS source='lesson' 命中 → 注入 "已知教训(规则):" 段 + 幂等 | **D2 按需检索**(不常驻 system);幂等防累积 |
| test_inject_lessons_empty | 空库跳过 | 无教训 → system 不动 | 零成本 |
| test_inject_lessons_pending_marked | pending 标记 | 未验证教训注入时标 "待验证" | **可回退/可信**:新教训不可信,标注让 LLM 参考时存疑 |

## 触发(2 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_learn_from_correction | 纠正触发 | 纠正信号 → 反思生成入库;无信号不触发 | L1-C 触发源 A;即时生成(L2-A) |
| test_learn_from_tool_error_limit | 工具错误限流 | 同轮两次错误 → 只生成 1 条教训 | **防烧 token**:连错连反思是浪费 |

## 命令(1 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_lessons_command | /lessons 命令 | list/confirm/del/用法错误 | CLI 管理入口(含验证操作) |

## 设计背景

- 三库边界(§8):教训=规则(do/don't),与记忆(事实)分库,独立注入段
- 独立 lessons 表 = 数据层可回退(del 即消失,不污染代码)
- 按需检索注入(≤5,L3-B)避免 system 无上限膨胀
