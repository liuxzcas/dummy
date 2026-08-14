# Dummy Agent

一个带持久记忆的 LLM Agent:工具调用循环 + 上下文压缩 + 全文搜索 + 跨会话记忆。

> EN: [README.md](./README.md)

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

> 本项目兼容**任何 OpenAI 格式的 LLM API**(DeepSeek / OpenAI / Ollama 本地 / vLLM 等),只需配置上述三个变量。前提:服务端支持 tool calling。

**3. 启动**

Windows:双击 `start.bat`,或在目录内运行 `start.bat`
其他平台:

```bash
python main.py
```

启动后直接输入文字对话即可,agent 会自动调用工具。

## 基本命令

| 命令 | 作用 |
|------|------|
| `/search <关键词>` | 全文搜索历史对话(支持中英文) |
| `/memories` | 查看已记住的事实;`/memories del <id>` 删除 |
| `/resume` | 恢复最近一次会话 |
| `/help` | 查看全部命令 |

## 记忆管理(/memories)

对话结束时会自动抽取值得长期记住的事实(写入时蒸馏:同类合并、精确值保留、冲突覆盖)。`/memories` 用于查看和管理:

**查看全部记忆**

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
| `conf` | 置信度(0~1),抽取器对该事实可信度的把握 |
| `hits` | 被注入使用的次数(0 = 尚未用过) |
| `来自` | 记忆来源会话 id(前 8 位) |

**删除记忆**(不可恢复)

```
/memories del 3      → 已删除记忆 #3
/memories del 999    → 记忆 #999 不存在
/memories del abc    → 用法: /memories del <id>
```

说明:记忆的写入由 Agent 在对话中自动完成,`/memories` 只负责查看与管理。

## 打断与确认

- 确认类工具(terminal / read_file / write_file)执行前会请求确认:按 Enter 允许,输入 `n` 拒绝
- **任何时刻**输入 `/p` 可打断 Agent(工具运行中、等待响应、确认提示时均可):打断后输入提示词,Agent 会按提示词重新规划;直接回车 = 取消打断
- 实时监听输入无回显(输入内容不可见,提示词在打断后单独输入)

## 项目结构

```
main.py          入口(CLI)
core.py          Agent 主循环(工具调用 + 记忆注入)
llm.py           LLM 客户端(OpenAI 兼容)
tools/           内置工具
session_store.py SQLite 存储 + FTS5 全文搜索
memory.py        记忆抽取(写入时蒸馏)
compressor.py    上下文压缩
tests/           pytest 测试套件(72 项)
docs/            设计文档(memory-system.md 等)
```

## 文档导航

- 记忆系统设计:`docs/memory-system.md`(双通道注入)
- 压缩机制:见 `docs/` 下压缩相关设计文档
- 回归测试:`python -m pytest tests/`(72 项);T5 回归集 `python scripts/phase2_regression.py`(需真实 API)

## 常见问题

**启动时提示输入 API Key?** 设置 `DUMMY_API` 环境变量即可跳过。

**想换模型/换服务?** 设置 `DUMMY_AGENT_BASE_URL` 和 `DUMMY_AGENT_MODEL`:

```bash
set DUMMY_AGENT_BASE_URL=http://localhost:11434/v1
set DUMMY_AGENT_MODEL=qwen2.5:7b
start.bat
```

**测试要花钱吗?** `pytest` 套件全部 mock,零 API 成本;只有 `phase2_regression.py` 走真实 API(约 0.1 元/轮)。
