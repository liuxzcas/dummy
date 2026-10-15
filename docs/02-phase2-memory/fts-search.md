# Phase 2.3b — 全文搜索(FTS5 双表方案)

> 状态:实施中(2026-08-10)
> 目标:对话历史 + 折叠原文的全文检索,提供 `/search` 命令,
> 为 Phase 2.4(跨 Session 记忆)提供检索地基。

> EN TL;DR: SQLite FTS5 dual-table search — `fts_en` (porter tokenizer, for
> English/numbers, word-level + stemming + prefix) and `fts_zh` (trigram, for
> Chinese, any ≥3-char substring). Query routed by CJK presence; 2-char
> Chinese queries degrade to LIKE. Index scope: `messages.content` (post-fold)
> + `tool_result_archive.content` (folded originals). Rebuild-on-search (data
> is small). CLI: `/search`.

## 1. 作用(三层)

| 层 | 谁在用 | 价值 |
|----|--------|------|
| 人类检索 | 用户调试/复盘 | `/search` 按内容定位对话,替代翻 session |
| Agent 检索地基 | Phase 2.4 跨 Session 记忆 | 记忆注入需要"按需检索历史事实",FTS 即检索层 |
| 决策 C 兑现 | 折叠原文 | L1 压掉的中间信息可搜回(此前"可查不可搜") |

## 2. 数据流与索引范围(关键前提)

- `messages.content` = **折叠后**的 tool 结果(头200+标记+尾100),原文不在其中
- `tool_result_archive.content` = **折叠前完整原文**(唯一保存处)
- 所以"全文"= 两个表都索引,带 `source` 列区分(`message` / `archive`)
- 排除 `role='system'` 消息:模板内容无检索价值,避免结果被 system prompt 污染

## 3. 方案:FTS5 双表(不是单 trigram,也不是双列单表)

**需求约束**:英文文献处理不能损失英文搜索质量(词级精确 + 词干 + 前缀)。

- **单 trigram**:英文变子串匹配(搜 "search" 命中 "research")——已否决
- **双列单表**:FTS5 的 tokenizer 是**表级**的,不支持列级指定——不可行
- **双表(采用)**:tokenizer 各自独立

```sql
CREATE VIRTUAL TABLE fts_en USING fts5(
    source UNINDEXED, session_id UNINDEXED, seq UNINDEXED, content,
    tokenize = 'porter'          -- 英文:词级 + 词干化(compression~compressing) + 前缀
);
CREATE VIRTUAL TABLE fts_zh USING fts5(
    source UNINDEXED, session_id UNINDEXED, seq UNINDEXED, content,
    tokenize = 'trigram'         -- 中文:任意 ≥3 字符连续子串
);
```

- UNINDEXED 列只存储不参与分词(搜索只对 content 生效)
- 原文冗余进 FTS 表,不依赖 messages 行 id 对齐(压缩重写 sequence 安全)

**查询路由**(按查询词性质;实测修正 2026-08-10):

| 查询词 | 走哪张表 | 能力 |
|--------|---------|------|
| 纯英文/数字 | fts_en | 词级 + 词干 + `compre*` 前缀 |
| 中文 ≥3 字符 | fts_zh | 中文子串命中 |
| 中文 2 字符 | **LIKE 退化** | trigram 无法匹配 2 字 token |
| 混合(如 "L1 压缩") | **按连续同语言片段拆分**,逐段查各自表,并集去重 | 中英都覆盖 |

实测修正记录:
- 混合查询最初设计"两表并集"(整体短语分别查两表)——不可行:trigram 对
  "L1 压缩" 切成 "L1 " / "1 压" / " 压缩"(含空格窗口),与文本 token 集
  不一致必然不命中;porter 表又要求中英片段连续出现。正确解是按片段拆分:
  "L1" → fts_en,"压缩" → LIKE。
- 前缀查询 `transfor*` 不能包成短语(短语内 `*` 是字面量);且分段正则
  `[A-Za-z0-9_]{2,}` 会吃掉 `*`,需在拆分后把结尾 `*` 还给最后英文片段。

## 4. 两个已知坑(设计内处理)

1. **中文 2 字查询 trigram 失效**("压缩""折叠"都是 2 字):
   trigram 索引/查询都是 3 字符窗口,2 字查询匹配不到任何 token。
   对策:`LIKE '%词%'` 全表扫(转义 % _)。dummy 数据量小,毫秒级。
2. **trigram 对英文是子串匹配**:英文查询绝不走 fts_zh(只走 fts_en),
   避免 "search" 命中 "research" 的噪音。

## 5. 索引维护:搜索前全量重建

- `rebuild_search_index()`:清空两表 → 从 messages(排除 system)+ archive
  全量重插。数据量小(几千行),毫秒级。
- `/search` 与 `search()` 调用前自动重建(惰性,保证与库一致)。
- 增量同步(游标 + dirty 标记)留作数据量大时的演进方向,不做第一期。

## 6. 接口

- `SessionStore.search(query, source=None, limit=20) -> list[dict]`
  - 字段:`source / session_id / seq / snippet / score`(bm25)
  - snippet 用 FTS5 `snippet()` 提取命中上下文
- CLI:`/search "关键词"`(main.py 接入)

## 7. 验证计划

- 英文:词级精确( "search" 不命中 "research")/ porter 词干
  ( "compression" 命中 "compressing")/ 前缀 `transfor*`
- 中文:3 字子串命中 / **2 字 LIKE 退化** / 混合查询片段拆分
- 归档表:模拟 L1 折叠(原文中间含 "port 8080",messages 无该串)→
  搜索命中 archive 行
- 排序:bm25 相关度、source 过滤

验证结果(2026-08-10,ad-hoc 20 项 + 回归 30 项全过):以上全部通过。
