# 记忆系统设计(方案 4:Hermes 方式 + 历史兜底)

> 状态:定稿(2026-08-12,已合并入 main)
> 演进:四轮实验后选定本方案,完整实验链见 §7。

> EN TL;DR: Memory = dual-channel injection. Primary channel: ALL memory
> entries injected into system prompt (distilled at write time, ≤400 chars).
> Fallback channel: expand question → FTS search full conversation history
> (lossless layer, ≤200 chars) appended. Extraction prompt enforces
> write-time distillation: merge same-topic facts into one refined entry,
> preserve exact values (numbers/proper nouns/lists). T5 regression: 28/30
> (93%) vs 19/30 for retrieval-based injection. Cost delta vs cheapest
> variant: ~¥0.01 per 30-question run.

## 1. 设计目标与背景

跨 Session 记忆要回答:"上次对话里的事实,新会话怎么用上"。
核心矛盾:**检索会丢,常驻会占**。四轮实验量化了这个矛盾:

| 方案 | 机制 | T5 成绩 |
|------|------|---------|
| 1. 检索 top-3 注入 | 问句 FTS 搜记忆条目,注入 top-3 | 19/30 (63%) |
| 2. PlugMem 概念+蒸馏 | 概念 LIKE 路由 + LLM 蒸馏注入 | 15/30 (50%) |
| 3. 全量常驻(方案 4 基础) | 全部记忆注入,写入时蒸馏 | 26/26/24 (80-87%) |
| **4. 全量常驻 + 历史兜底(定稿)** | 3 + 问句提炼词 FTS 搜完整历史附加注入 | **28/30 (93%)** |

关键教训链:
- 检索(方案 1/2)的瓶颈是"问句 vs 事实短语"匹配——FTS 关键词和概念 LIKE
  都会因字面不重叠而落空(方案 2 的 PlugMem 简化版还暴露:无稠密 embedding
  的概念路由是死路)
- 全量常驻(方案 3)绕开检索,但暴露抽取随机性(三轮 26/26/24 波动)
- 历史兜底(方案 4)把"抽取丢的"从**无损层**(完整对话)救回

## 2. 架构:双通道注入

```
chat(user_input)
  └─ _inject_memories(user_input)
       ├─ 主通道:记忆全量注入(蒸馏层,有损但精炼)
       │    list_memories() → confidence 降序 → ≤400 字符 → system prompt
       └─ 兜底通道:历史片段注入(无损层,原始事实)
            expand_query(user_input) → 提炼词 → FTS 搜 messages 源
            → 去重片段 ≤200 字符 → "相关历史记录" 段
```

- **主通道**(记忆):抽取器在写入时把对话蒸馏成精炼事实(§3),注入侧零 LLM
  调用,全量注入(容量内)
- **兜底通道**(历史):问句经 LLM 提炼成 2-4 个检索词(失败回退原问句),
  FTS(2.3b 基建,零 LLM 成本)搜完整对话历史,取相关原始片段附加注入
- 设计哲学:**蒸馏层会波动,无损层永不丢**——两条通道互补

## 3. 写入时蒸馏(EXTRACT_PROMPT)

对话结束(chat() 返回前,旁路调用)抽取一次,三条铁律:

1. **同类合并**:同一主题的新证据合并成一条精炼表述
   ("预算先定 5000 后来改成 8000" → "最终预算是 8000 元")
   ——解决"新旧值并存导致模型困惑"(post_hoc 题型 2/6 → 6/6)
2. **精确值原样保留**:数量/专名/端口/路径/清单不得省略或概括成"若干"
   ——防止清单实体丢失(statistic 题型)
3. **replace_id 覆盖**:同主题更新用 replace_id 指向旧条目,不并存
   (保留 id,更新文本/置信度)

容错:调用异常/解析失败/空结果 → 记 memory.jsonl 事件,返回 (0,0),
绝不影响对话主流程。事件日志可审计(extract / extract_failed)。

## 4. 注入细节

| 项 | 值 |
|----|----|
| 记忆段容量 | ≤400 字符(≈500 token 保守值) |
| 历史段容量 | ≤200 字符(去重,按提炼词取 top-2 命中) |
| 排序 | confidence 降序,同置信新条目优先(重要事实先注入) |
| 防累积 | 注入前剥离旧"已知事实(记忆):"与"相关历史记录:"段 |
| 显示 | 🧠 注入记忆 (N/M 条, ~X chars + 历史 Y 条) + 每条全文 |
| 命中统计 | hits 只递增实际注入的条目 |

## 5. 可观测性

- 注入时实时打印(🧠 条数 + 全文 + 历史条数)
- 抽取结果打印(🧠 已抽取 N 条事实(M 条覆盖))
- 事件日志 logs/memory.jsonl(extract / extract_failed,可统计失败率)
- `/memories` CLI 查看/删除
- session.db 直查 memories 表

## 6. 性能与成本

**T5 回归集(30 题,真实 DeepSeek)**:
- 成绩:28/30 (93%)——long 6/6, stat 6/6, post 6/6, conv 5/6, pers 5/6
- 单轮波动 ±2 题(抽取随机性);历史兜底把波动下限从 80% 抬到 93%

**Token 成本**(基于实际 prompt 长度定量测算):
- 30 题 ≈ 104K token ≈ 0.11 元(输入 1 元/M,输出 2 元/M)
- 月度(40 次对话 × 8 轮)≈ 568K ≈ 0.61 元
- 与最省方案(3)差异 <5%——**成本不是方案选择的因素**;
  真正的大头是"每轮抽取"(EXTRACT prompt 400 token + 历史累积),
  想省 token 应优化抽取频率而非换方案

## 7. 实验记录(四轮迭代,分支保留)

| 分支 | 方案 | 结果 | 结论 |
|------|------|------|------|
| main 前身 | 检索 top-3 | 19/30 | 检索是瓶颈 |
| plugmem-v2 | PlugMem 简化(概念+蒸馏) | 15/30 | 无 embedding 的概念路由是死路 |
| hermes-memory-v3 | 全量常驻 + 写入时蒸馏 | 26/26/24 | 检索问题消失,暴露抽取波动 |
| hermes-memory-v3+兜底 | + 历史 FTS | **28/30** | 无损层兜住蒸馏层波动 |

借鉴来源:
- Hermes 内置记忆(实证查阅 2026-08-12):全量常驻注入 + 写满时整合,
  session_search 用 FTS 搜完整会话而非记忆条目
- PlugMem(arXiv:2603.03296):知识单元 + 写入时蒸馏思想(论文的
  reasoning 模块与我们的蒸馏殊途同归);其稠密 embedding 检索是我们的
  兜底通道在向量时代的演进方向

## 8. 已知边界(记录,不修)

1. **同义词鸿沟**:提炼词与历史文本无字面重叠时兜底也失效
   ("工作习惯" ↔ 历史"先看文档")——关键词检索的终极边界,
   向量检索(如 PlugMem 的 NV-Embed)是演进方向
2. **抽取随机性**:同一对话多次抽取结果略有差异(±2 题波动),
   历史兜底已大幅缓解;完全消除需多轮一致性约束
3. **清单丢失**:统计题清单(三个城市)在蒸馏时可能被压缩,
   "精确值原样保留"规则已缓解,仍受 LLM 遵从度影响

## 9. 相关文件

- `core.py`:`_inject_memories`(双通道注入)/ `_retrieve_history_evidence`
  (兜底通道)/ `_extract_memories`(写入时蒸馏触发)
- `memory.py`:EXTRACT_PROMPT(蒸馏铁律)/ MemoryExtractor / expand_query
- `session_store.py`:memories 表(CRUD/冲突覆盖/hits)+ FTS memory 源
- `tests/test_memory.py`:72 项套件中的记忆部分
- `scripts/phase2_regression.py`:T5 回归集(30 题,五类题型)
