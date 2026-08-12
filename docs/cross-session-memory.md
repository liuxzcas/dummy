# Phase 2.4 — 跨 Session 记忆

> 状态:实施中(2026-08-11)
> 目标:对话中沉淀的事实 → 结构化条目 → 后续会话按需注入,让 agent
> 记得用户偏好与项目事实(PlugMem 知识单元低配版 + Mem0 两阶段法)。

> EN TL;DR: Cross-session memory. After each chat, an LLM pass extracts
> facts (fact/category/confidence) into the `memories` table; conflicts
> resolved by the extractor emitting `replace_id` (newer + higher
> confidence wins). At chat start, the current user input is used as an
> FTS query (source='memory', reuse 2.3b infra) to inject top-k facts
> into the system prompt (≤500 tokens), printed for visibility
> (🧠 注入记忆). CLI: `/memories` / `/memories del <id>`. All extract /
> inject events logged to `logs/memory.jsonl`.

## 1. 数据流

```
对话结束 → LLM 抽取事实(旁路调用,不污染 history)
        → 冲突裁决(LLM 输出 replace_id → 覆盖旧条目)
        → memories 表
                ↓
新对话开始 → 用户输入作查询 → FTS 检索(source='memory', top-k)
        → 注入 system prompt"已知事实(记忆)"段(≤500 token)
        → 打印 🧠 注入记忆(可见性)
```

抽取是**旁路调用**:LLM 返回的事实不进对话历史,对话本身不受影响;
抽取失败(超时/JSON 非法/空)记事件日志并跳过,绝不影响对话返回。

## 2. 表结构

```sql
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,      -- 来源会话(哪次对话抽出来的)
    fact TEXT NOT NULL,            -- 单条事实文本
    category TEXT NOT NULL DEFAULT 'general',  -- 偏好/项目/技术/其他
    confidence REAL NOT NULL DEFAULT 0.8,      -- 抽取时 LLM 给的可信度
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,      -- 被覆盖时更新
    hits INTEGER NOT NULL DEFAULT 0            -- 被注入命中次数(使用统计)
)
```

检索:memories 作为 FTS 第三索引源(source='memory'),复用 2.3b 的
fts_en(porter)/fts_zh(trigram)与片段拆分路由——中文子串检索、
英文词级检索全部白拿。注入查询:`search(query, source='memory', limit=top_k)`。

## 3. 抽取(决策点 1A:对话结束一次性)

- 时机:chat() 返回 final_text 前调用
- Prompt:给出"已有记忆列表(id + fact)",要求 LLM 输出 JSON 数组,
  每项 `{fact, category, confidence, replace_id}`;replace_id 指向
  语义重复的旧记忆(冲突裁决交给 LLM 的语义理解,代码只执行)
- 应用规则:
  - replace_id 命中 → **覆盖**:更新 fact/category/confidence/updated_at
    (保留 id 与 hits,覆盖不重置使用统计)
  - 否则 → 新增
- 容错:调用异常 / JSON 解析失败 / 空列表 → 记 extract_failed 事件,跳过
- 事件日志 `logs/memory.jsonl`:
  - `{"event":"extract","time":...,"session":...,"facts":N,"conflicts":M,"ok":true}`
  - `{"event":"extract_failed","time":...,"session":...,"error_type":...,"ok":false}`
  - `{"event":"inject","time":...,"query":...,"hits":N,"total":M,"tokens":~K}`

## 4. 注入(决策点 2A:每次 chat + 决策点 3A:FTS 检索)

- 时机:chat() 入口,用当前 user_input 作查询
- 检索:`store.search(user_input, source='memory', limit=3)`
- 组装:追加到 history[0](system)content 末尾:

```
已知事实(记忆):
- [category] fact
- [category] fact
```

  只改 system 消息 content,不新增消息(避免角色交替问题)
- 上限:注入字符数 ≤400(≈500 token 的保守值);超出截断
- 显示(可见性,决策:注入条数 + 每条全文):

```
🧠 注入记忆 (2/15 条, 38 tokens):
  - [偏好] 用户偏好中文交流
  - [项目] dummy-agent 的测试用 pytest
```

## 5. CLI

```
/memories          列出全部记忆(id/类别/事实/置信度/来源/时间/命中)
/memories del <id> 删除指定记忆
```

实现为纯函数 `handle_memories_command(user_input, store) -> list[str]`
(与 handle_search_command 同风格,可测)。

## 6. 可观测性(一等设计目标)

记忆系统看不见等于没有:
1. 注入时实时打印(🧠 注入记忆 + 全文)
2. 抽取结果打印(🧠 已抽取 N 条事实,其中 M 条覆盖旧事实)
3. 事件日志 logs/memory.jsonl 全量可审计
4. /memories 主动查看管理
5. session.db 直查 memories 表

## 7. 验证计划

- 抽取:mock LLM JSON 解析正确 / replace_id 覆盖生效 / 非法 JSON 跳过
- 容错:抽取异常时对话正常返回、事件日志有记录
- 注入:FTS 命中记忆 / system 组装正确 / 字符上限 / 不新增消息
- 旁路:抽取调用后 history 长度不变(不污染对话)
- CLI:/memories 列出与删除
- 回归:pytest 30 项;LongMemEval 五类题型 20-30 题回归集并入
  Phase 2 综合测试集(规划第三步)
