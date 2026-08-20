---
name: literature-review
description: 文献综述完整流程(检索 → 整理 → 成稿)。当用户要求"综述 XX 领域/调研 XX 主题/写文献综述"时使用。原子步骤细节见引用的技能。
type: workflow
steps:
  - search-literature
  - organize-notes
  - write-summary
---

# 文献综述

按顺序执行以下原子步骤。每个步骤的完整细节见对应技能的 SKILL.md(用 read_file 读取):

1. **search-literature** — 检索文献,得到候选列表
2. **organize-notes** — 整理候选,去重归类,得到结构化材料
3. **write-summary** — 基于材料撰写综述文稿

## 编排说明

- **每步执行前必须先 read_file 读取该技能的 SKILL.md**(步骤细节/注意事项以技能文件为准,不凭印象执行)
- 每步完成后,把结果作为下一步的输入(候选列表 → 材料 → 文稿)
- 步骤间用户可打断调整(如先看候选列表再决定是否继续)
- 材料不足时回到步骤 1 补充检索,不硬写
