# 架构评估 — 耦合/内聚审查(2026-08-21)

> 背景:Phase 0 → 现在,功能不断叠加(记忆/压缩/FTS/技能/教训/自我改进),
> 用户担心耦合太强内聚太低,问是否需要整体重构。
> 结论:**不需要整体重构,值得 3 项低成本定向重构**。本文档记录证据与决策。

---

## 1. 现状数据(2026-08-21 实测)

| 模块 | 行数 | 职责 |
|------|------|------|
| core.py | 1225(34 方法) | 编排:对话循环/注入×5/学习/自我改进/会话/打断/用量 |
| session_store.py | 804(27 方法) | 持久化:sessions/messages/archives/memories/lessons/FTS/usage |
| main.py | 494 | CLI:命令分发/交互循环 |
| tools/(registry + 6 handler) | ~1060 | 工具层 |
| compressor.py | 421 | 上下文压缩(L1+L2) |
| llm.py | 332 | OpenAI 兼容 API 层 |
| self_improve.py / memory.py / skills_manager.py / lessons.py | 235/194/171/120 | Phase 2/3 功能模块 |
| prompt.py / colors.py | 134/53 | 提示词/配色 |
| **总计** | **~5357** | |

依赖方向:main → core → {llm/compressor/session_store/memory/lessons/
skills_manager/self_improve/colors/prompt};功能模块间几乎零互依赖。
**无循环依赖**(全部模块可独立导入,实测 OK)。

## 2. 评估结论

### 健康面(骨架对)
1. 分层清晰:API 层 → 存储层 → 功能模块 → 编排层(core)→ CLI
2. 依赖方向单向(core 编排,功能模块不反向依赖)
3. 每功能模块独立可测(115 项测试按模块分文件)
4. Phase 2/3 新功能(memory/lessons/skills/self_improve)都做成了独立
   新文件,没有塞进 core——模块化纪律有效

### 问题面(2 个胖文件 + 重复)
1. **core.py 1225 行混 7 类职责**:对话循环/注入族/学习族/自我改进
   编排/会话管理/打断机制/用量统计——"牵一发而动全身"风险最高的文件
2. **session_store.py 804 行/27 方法**:单类承载 6 类数据表 + FTS 双表
3. **5 个 _inject_* 方法同构**(找 marker → 替换/追加 → 写回):DRY 重复
4. main.py 命令 if 链(可表驱动);run_improvement 局部 import shutil

## 3. 决策:不整体重构,做 3 项定向重构

**不整体重构理由**:
- 骨架对(分层 + 单向依赖 + 无循环 + 模块可测),重构收益来自修坏结构
- 115 项测试是安全网,整体重写撕掉网,风险 > 收益
  (与"可回退 > 防错""现有内容不动只追加"哲学一致)
- 先例:架构 pivot 用新建项目,不重写

**3 项定向重构**(合计 1-2 小时,Phase 3 收尾后做):
| # | 改动 | 收益 | 工作量 |
|---|------|------|--------|
| 1 | core 注入族抽 `_inject_section(marker, block)` | 消重复 ~50 行 | 0.5-1h |
| 2 | main 命令 if 链 → 分发表 | 加命令改一行 | 0.5h |
| 3 | run_improvement 局部 import 提顶部 | 风格统一 | 5min |

**明确不做**:core 拆多文件(injections.py 等)——编排职责天然集中,
拆分引入跨文件状态传递,收益一般风险偏高。

**预警线**:core.py > 1500 行,或注入族再增 2 个以上 → 再考虑拆
"injections 模块"。
