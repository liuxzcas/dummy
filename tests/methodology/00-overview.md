# 测试方法论总览(115 条)

> 目的:逐条解释 pytest 套件**测什么、怎么测、为什么测**。
> 每文件对应一个测试文件,详见 01~07。
> EN TL;DR: Methodology index — 115 pytest cases across 7 files,
> each explained by subject / method / purpose (failure-mode driven).

## 结构

| 文件 | 条数 | 被测模块 | 阶段 |
|------|------|---------|------|
| 01-compression.md | 30 | compressor.py | Phase 2.2(压缩) |
| 02-persistence.md | 11 | session_store.py(存储) | Phase 2.1(持久化,T1) |
| 03-search.md | 15 | session_store.py(FTS) | Phase 2.3b(全文搜索,T3) |
| 04-memory.md | 16 | memory.py + core 注入 | Phase 2.4(跨会话记忆,T4) |
| 05-skills.md | 15 | skills_manager.py + core | Phase 3 Step 1/2(技能) |
| 06-lessons.md | 16 | lessons.py + core + 存储 | Phase 3 Step 3(错误学习) |
| 07-self-improve.md | 12 | self_improve.py + core | Phase 3 Step 4(自我改进) |

## 方法论原则(为什么这么测)

1. **失败模式驱动**:每条断言对应一个具体失败模式(如"英文搜索被中文子串污染"、
   "同秒会话顺序不稳定"、"教训注入重复累积"),不是泛泛测功能
2. **三层验证**:自动化断言(pytest,本套件)/ 定向脚本(hermes-verify,临时)/
   真实目录端到端——本套件是第一层,可重复、零 API 成本(全 mock)
3. **容错必测**:异常/非法输入/降级路径与正常路径同等重要(抽取三级容错、
   压缩断路器、存储并发)
4. **测试自纠**:断言失败先区分"代码 bug vs 测试 bug"(脚本期望值/查询词
   与语义对齐问题曾多次出现)

## 运行

```bash
python -m pytest tests/ -q    # 115 passed,全 mock,零 API 成本
```

## 测试数统计(参数化展开后)

compression 30(含 validate_history×8、recall_basic×7 参数化)+
persistence 11 + search 15 + memory 16 + skills 15 + lessons 16 +
self_improve 12 = **115**
