# 07 自我改进测试(12 条)— self_improve.py + core + registry

> 阶段:Phase 3 Step 4(自我改进闭环:检测→提案→批准→修改→验证→回退)
> 测什么:dispatch 错误统计、检测阈值(I4-A)、提案生成(三级容错)、
> 文件权限(禁止区)、验证门链、闭环各路径(批准/拒绝/无数据/拦截)、
> 改进记录(§3.5 量化)

## 检测源(2 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_dispatch_stats | dispatch 统计 | 正常/错误/未知工具调用 → calls/errors/error_samples 计数,样本 ≤3 | **D3-A 检测源**:错误率统计是自我改进的眼睛;样本供根因分析 |
| test_dispatch_stats_param_error | 参数错误计数 | _error 分支 → calls+errors+样本含 [ToolDispatch] | 参数错也是错误信号,不能漏计 |

## 阈值与提案(4 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_detect_threshold | I4-A 阈值 | ≥3 次且 >30% 触发;次数不足/比例不足/恰好 30% 不触发 | **阈值语义**:次数防偶然,比例防小题大做;严格大于 |
| test_generate_proposal | 提案生成 | mock LLM → root_cause/changes/verification 提取 | 提案是"只读分析"的产物;结构错则无法展示/批准 |
| test_generate_proposal_invalid | 提案容错 | 非 JSON/空/changes 空 → 空 dict | 提案失败降级为"无法生成",不中断 |
| test_is_allowed_file | 文件权限 | 项目内允许;.git 禁止;../ 项目外禁止 | **安全分区**:禁止区永不触碰;项目外拒绝 |

## 验证门链(2 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_verify_change_ok | 门链通过 | 合法 .py → 语法+导入门通过(测试用 run_pytest=False) | 验证门是"改自己"的安全网核心 |
| test_verify_change_syntax_error | 语法门拦截 | 语法错误 → 提前返回"语法门失败" | 语法错是最快失败点,后门无意义(先拦) |

## 闭环路径(4 条)

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_run_improvement_rejected | 拒绝路径 | 批准提示 n → status=rejected,零改动 | **D4-A 写必须批准**:拒绝是用户权利的验证 |
| test_run_improvement_no_data | 无数据路径 | 无该工具统计 → no_data | /improve 容错 |
| test_run_improvement_blocked | 禁止区拦截 | 提案改 .git/config + 批准 → blocked | **安全最后一公里**:即使批准,禁止区也拦(救生艇) |
| test_improve_log_format | 改进记录 | JSONL 追加(status/tool 字段) | §3.5 量化评估数据源;格式错则统计失真 |

## 设计背景

- 安全协议(§3.4 强制前置):五道防线(git 检查点→备份→语法门→
  导入门→验证门+启动门);自动回退(备份+git 双保险)+ 教训联动
- 决策:I1-A(/improve + 自动报警)/ I2-A(批准即授权整条链)/
  I3-A(只改提案列明文件)/ I4-A(阈值)
- 量化(§3.5):改进记录 JSONL 支撑错误率/闭环成功率/回退率统计
