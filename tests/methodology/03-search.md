# 03 全文搜索测试(15 条)— session_store.py FTS

> 阶段:Phase 2.3b 综合测试集 T3(从 ad-hoc 20 项固化)
> 测什么:FTS5 双表(fts_en porter / fts_zh trigram)、路由、
> 退化路径、索引范围、排序、压缩后一致性

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_english_word_level_precision | 英文词级精确 | "search" 命中含 search 的文本;**不命中含 research 的文本** | **失败模式:子串污染**——词级搜索不能把 research 当 search 命中 |
| test_porter_stemming | 词干化 | "searching"/"searched" 命中 "search"(porter 词干) | 英文文献检索要容忍词形变化;用户后续大量英文文献 |
| test_prefix_query | 前缀查询 | "compre*" 命中 "compression" | 前缀是词级搜索的常用形态,必须支持 |
| test_chinese_trigram | 中文 trigram | ≥3 字中文片段命中(trigram 切词) | 中文无空格分词,trigram 是 FTS5 标准方案 |
| test_chinese_two_char_like_fallback | 2 字 LIKE 退化 | 2 字中文(trigram 盲区)→ LIKE 退路命中 | **trigram 固有缺陷的兜底**;没有它 2 字词永远搜不到 |
| test_mixed_query_segments | 混合查询拆分 | "L1 压缩" 拆成 "L1"+"压缩" 分别查并集 | **实测修正**:整体查不命中(trigram 含空格窗口),按连续同语言片段拆分是正确解 |
| test_archive_original_retrievable | 归档原文可搜 | 折叠掉的内容只能从归档搜到 → 断言命中 archive 源 | **索引范围契约**:关键信息被 L1 压掉后必须能从归档救回 |
| test_source_filter_match_path | source 过滤(MATCH 路) | search(source='message') 只返回该源 | /search 命令按源过滤;过滤错会混入无关结果 |
| test_source_filter_like_path | source 过滤(LIKE 路) | 2 字查询 + source 过滤同样生效 | **真实 bug 抓取点**:LIKE 分支曾漏 memory 源(2.3b 修复) |
| test_system_template_not_indexed | system 模板不入索引 | 索引重建后 system 模板(role=system)搜不到 | system 模板是每轮重复的噪声,索引它 = 污染命中 |
| test_bm25_ordering | BM25 排序 | 相关度高的文档排前面(关键词密度) | 搜索结果排序错 = 用户翻半天找不到 |
| test_memory_source_searchable | 记忆源可搜 | memory 源(MATCH 路)命中 | 记忆是第三索引源(2.4);搜不到记忆 = 注入兜底失效 |
| test_memory_two_char_like | 记忆源 2 字 LIKE | 2 字中文记忆 LIKE 命中 | **真实 bug 抓取点**:LIKE 分支 UNION 缺 memory 源,2 字中文记忆检索不到(2.3b 修复) |
| test_edge_cases | 边界 | 空查询/特殊字符(%, _)/无命中 → 不崩 | LIKE 转义(% _ \)漏转义会全表命中或语法错 |
| test_index_after_compressed_rewrite | 压缩重写后索引正确 | 压缩重写历史 → rebuild → 新内容可搜旧内容不可搜 | 索引与库一致性在压缩场景必须成立(与持久化 10 联动) |

## 设计背景(为什么双表)

FTS5 tokenizer 是表级:英文 porter(词级+前缀+词干)与中文 trigram
无法共存于一张表 → 双表;查询按连续同语言片段拆分路由。
英文性能不妥协(用户后续处理大量英文文献,实测决策)。
