# 02 持久化测试(11 条)— session_store.py 存储层

> 阶段:Phase 2.1 综合测试集 T1
> 测什么:会话/消息的写入、读取、恢复、并发、时间、归档、索引一致性

| 测试 | 测什么 | 怎么测 | 为什么测 |
|------|--------|--------|---------|
| test_save_history_upsert_idempotent | 保存幂等 | 同会话同历史保存两次 → 行数不翻倍 | upsert 语义错会重复存储,历史膨胀 |
| test_save_history_tail_delete | 尾部删除 | 保存短历史覆盖长历史 → 多余尾部行被删 | 压缩/重置会重写历史;残留旧行会污染恢复 |
| test_load_history_full_restore | 完整恢复 | 保存 → 加载 → 逐消息对比 | 会话恢复是 /resume 的根基;丢消息 = 丢上下文 |
| test_create_session_and_latest | 会话创建 + 最新获取 | 建多会话 → get_latest_session_id 返回最新;**同秒创建顺序稳定**(rowid 次级键) | **真实 bug 抓取点**:同秒会话排序曾不稳定(T1 抓到,rowid 修复) |
| test_time_stored_utc | UTC 存储 | 断言 created_at 是 UTC 格式(isotime 无 tz 偏移) | 存储层 UTC canonical,展示层转本地——时间基准错乱会跨时区污染 |
| test_concurrent_write_no_loss | 并发写不丢 | 多线程同时写 → 全部落库(busy_timeout + 事务) | SQLite 并发写丢数据是常见坑;busy_timeout 是保命配置 |
| test_compressed_rewrite_consistency | 压缩重写一致性 | 压缩后历史重写 → 读回与压缩产物一致 | 压缩器写回 + 持久化两条路必须一致(否则压缩丢失) |
| test_archive_idempotent_unique | 归档幂等 + 唯一 | 同 tool_call_id 归档两次 → 单行(UNIQUE 约束) | 归档表 UNIQUE(session_id, tool_call_id);重复归档会膨胀 |
| test_archive_retrieve_original | 归档原文取回 | 折叠内容 → 归档 → 按 id 取回原文 | **决策 C 兑现**:折叠掉的信息必须能从归档找回 |
| test_index_rebuild_matches_db | 索引与库一致 | rebuild 后,每条消息/归档在 FTS 中有对应行 | 搜索索引漂移 = 搜不到该搜的内容 |
| test_list_sessions_counts | 会话列表统计 | 多会话后 list 返回数量/顺序 | /sessions 命令的数据基础 |

## 抓到的真实 bug

- **get_latest_session_id 同秒排序不稳定**:created_at 秒级精度,同秒创建的
  会话顺序靠 rowid 次级键修复(T1 首个用例即抓到)
