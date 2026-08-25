# Dummy Agent

一个带持久记忆与自主学习能力的 LLM Agent:工具调用循环 + 上下文压缩 + 全文搜索 + 跨会话记忆 + 技能系统 + 错误学习 + 自我改进。

> EN: [README.md](./README.md)  |  更新记录: [WHATS-NEW.md](./WHATS-NEW.md)

## 快速开始

**1. 克隆并进入目录**

```bash
git clone https://github.com/liuxzcas/dummy.git
cd dummy
```

**2. 配置环境变量**(Windows 用户建议设为用户级环境变量,或直接启动时输入)

| 变量 | 必填 | 说明 |
|------|------|------|
| `DUMMY_API` | 是 | API Key(DeepSeek / OpenAI / 其他兼容服务均可) |
| `DUMMY_AGENT_BASE_URL` | 否 | API 地址,默认 `https://api.deepseek.com` |
| `DUMMY_AGENT_MODEL` | 否 | 模型名,默认 `deepseek-v4-flash` |
| `DUMMY_BASH_PATH` | 否 | git-bash 路径(terminal 工具用;默认指向开发机已验证路径,其他机器自动回退 WSL/cmd) |

> 本项目兼容**任何 OpenAI 格式的 LLM API**(DeepSeek / OpenAI / Ollama 本地 / vLLM 等),只需配置上述变量。前提:服务端支持 tool calling。

**3. 启动**

Windows:双击 `start.bat`(会自动检测 git-bash 供 terminal 工具使用)
其他平台:

```bash
python main.py
```

启动后直接输入文字对话即可,agent 会自动调用工具。每轮回答后显示用量统计(模型/本次/累计/缓存命中率/成本/窗口占用)。

## 基本命令

| 命令 | 作用 |
|------|------|
| `/search <关键词>` | 全文搜索历史对话与折叠原文(支持中英文) |
| `/memories` | 查看已记住的事实;`/memories del <id>` 删除 |
| `/skills` | 列出技能;`/skills show <name>` 看全文;`/skills del <name>` 删除 |
| `/lessons` | 列出学到的教训;`/lessons confirm <id>` 验证;`/lessons del <id>` 删除 |
| `/improve <工具名>` | 自我改进:分析该工具错误,提案修复(需批准,改坏自动回退) |
| `/history` / `/history del <序号>` | 查看当前会话记录 / 删除某条 |
| `/sessions` / `/sessions del <id>` | 列出会话 / 删除会话(级联清理其数据) |
| `/resume` | 恢复最近一次会话 |
| `/reset` | 开启新会话 |
| `/tools` | 列出可用工具 |
| `/help` | 查看全部命令 |

## 记忆管理(/memories)

对话结束时会自动抽取值得长期记住的事实(写入时蒸馏:同类合并、精确值保留、冲突覆盖),下次会话按需注入。`/memories` 用于查看和管理:

```
/memories
🧠 记忆 (3 条):
  [3] [偏好] 用户偏好中文交流 (conf=0.9, hits=4, 来自 a1b2c3d4)
  [2] [项目] 测试框架为 pytest (conf=0.8, hits=2, 来自 9f8e7d6c)
  [1] [技术] 部署环境为 Linux (conf=0.7, hits=0, 来自 9f8e7d6c)
```

| 字段 | 含义 |
|------|------|
| `[id]` | 记忆编号,删除时使用 |
| `[category]` | 分类:偏好 / 项目 / 技术 / 其他 |
| `conf` | 置信度(0~1) |
| `hits` | 被注入使用的次数 |
| `来自` | 来源会话 id(前 8 位) |

## 技能系统(/skills)

把重复流程固化为可复用技能,agent 按任务自动选择使用:

- **技能格式**:SKILL.md(YAML frontmatter: name/description/type + 步骤正文);**原子技能**(单能力,跨流程复用)+ **工作流技能**(steps 引用原子技能,轻量编排)
- **触发方式**:每轮对话 system prompt 注入技能索引(名称+描述);LLM 判断任务匹配 → read_file 读取技能全文 → 按步骤执行
- **创建技能**:直接说"把 XX 固化为技能" → agent 走 create-skill 流程(访谈边界 → 生成 → 自动校验 → 保存),新技能立即可用
- **预置技能**:create-skill(创建技能)/ search-literature(检索文献)/ organize-notes(整理)/ write-summary(成稿)/ literature-review(文献综述工作流)

## 错误学习(/lessons)

从错误中吸取教训,避免重犯:

- **生成**:用户纠正(检测强纠正信号)或工具执行错误时,即时反思生成一句话规则教训
- **注入**:任务开始时按相关性检索注入(≤5 条),pending 教训标注"待验证"
- **管理**:`/lessons` 查看;`/lessons confirm <id>` 确认有效;hits ≥2 自动转正;`/lessons del <id>` 删除

## 自我改进(/improve)

用户监管下修复自身缺陷(如工具反复报错):

- **检测**:工具错误率 ≥3 次且 >30% 时,自动提示"输入 /improve X 可分析修复"
- **流程**:`/improve <工具名>` → LLM 只读根因分析(提案:改动清单+验证计划)→ **你批准** → 修改 → 自动验证(语法→导入→pytest→启动门)
- **安全**:改坏自动回退(git 双保险)+ 记教训;禁止区文件(.git 等)永不触碰;修改记录在 `logs/self-improve.jsonl`

## 打断与确认

- 确认类工具(terminal / read_file / write_file)执行前会请求确认:按 Enter 允许,输入 `n` 拒绝;write_file 支持 `d` 查看完整 diff(旧内容红、新内容绿)
- **任何时刻**输入 `/p` 可打断 Agent(工具运行中、等待响应、确认提示时均可):打断后输入提示词,Agent 会按提示词重新规划;直接回车 = 取消打断
- 实时监听输入无回显

## 项目结构

```
main.py           入口(CLI)
core.py           Agent 编排(对话循环 + 注入 + 学习 + 自我改进)
llm.py            LLM 客户端(OpenAI 兼容)
tools/            内置工具(terminal/read_file/write_file/web_search/web_extract)
session_store.py  SQLite 存储 + FTS5 全文搜索(消息/归档/记忆/教训)
memory.py         记忆抽取(写入时蒸馏)
lessons.py        教训生成(错误学习)
skills_manager.py 技能管理(SKILL.md 解析/校验/索引)
self_improve.py   自我改进(检测/提案/验证门/回退)
compressor.py     上下文压缩(L1 折叠 + L2 增量摘要)
skills/           技能库(5 个预置技能)
tests/            pytest 测试套件(115 项)
docs/             设计文档与验证报告
```

## 文档导航

- 设计研究:`docs/phase3-research.md`(技能/错误学习/自我改进/安全保障/量化评估/workspace/三库边界)
- 记忆系统设计:`docs/memory-system.md`(双通道注入)
- 全文搜索设计:`docs/fts-search.md`(双表方案)
- 更新记录:`WHATS-NEW.md`
- 回归测试:`python -m pytest tests/`(115 项,全 mock 零成本)

## 常见问题

**启动时提示输入 API Key?** 设置 `DUMMY_API` 环境变量即可跳过。

**想换模型/换服务?** 设置 `DUMMY_AGENT_BASE_URL` 和 `DUMMY_AGENT_MODEL`:

```bash
set DUMMY_AGENT_BASE_URL=http://localhost:11434/v1
set DUMMY_AGENT_MODEL=qwen2.5:7b
python main.py
```

**本地 ollama/vllm 统计准吗?** 成本显示"本地 (无 API 成本)",窗口按模型映射自动调整。

**测试要花钱吗?** `pytest` 套件全部 mock,零 API 成本。
