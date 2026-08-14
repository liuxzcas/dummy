# Dummy Agent

A persistent-memory LLM agent featuring a tool-calling loop, context
compression, full-text search, and cross-session memory.

> 中文版: [README_CN.md](./README_CN.md)

## Quick Start

**1. Clone and enter the directory**

```bash
git clone https://github.com/liuxzcas/dummy.git
cd dummy
```

**2. Configure environment variables** (set as user-level variables on
Windows, or enter them at first launch)

| Variable | Required | Description |
|----------|----------|-------------|
| `DUMMY_API` | Yes | API key (DeepSeek / OpenAI / any compatible service) |
| `DUMMY_AGENT_BASE_URL` | No | API base URL, default `https://api.deepseek.com` |
| `DUMMY_AGENT_MODEL` | No | Model name, default `deepseek-v4-flash` |

> Compatible with **any OpenAI-format LLM API** (DeepSeek / OpenAI /
> local Ollama / vLLM, etc.) — just set the three variables above.
> Requirement: the endpoint must support tool calling.

**3. Launch**

Windows: double-click `start.bat`, or run `start.bat` inside the directory.
Other platforms:

```bash
python main.py
```

Type messages directly to start a conversation; the agent calls tools
automatically.

## Commands

| Command | Purpose |
|---------|---------|
| `/search <keyword>` | Full-text search over conversation history (EN/ZH) |
| `/memories` | List remembered facts; `/memories del <id>` to remove |
| `/resume` | Resume the most recent session |
| `/help` | Show all commands |

## Interrupt & Confirm

- Confirmation-required tools (terminal / read_file / write_file) ask
  before running: **Enter** to allow, **n** to reject.
- Type **`/p`** at any time to interrupt the agent — while a tool is
  running, while waiting for a response, or at a confirm prompt. After
  interrupting, enter your instruction and the agent re-plans
  accordingly; plain Enter cancels the interrupt.
- Live-listener input has no echo (invisible while typing; the prompt
  is entered separately after the interrupt).

## Project Structure

```
main.py          Entry point (CLI)
core.py          Agent loop (tool calling + memory injection)
llm.py           LLM client (OpenAI-compatible)
tools/           Built-in tools
session_store.py SQLite storage + FTS5 full-text search
memory.py        Memory extraction (write-time distillation)
compressor.py    Context compression
tests/           pytest suite (72 tests)
docs/            Design docs (memory-system.md, etc.)
```

## Documentation

- Memory system design: `docs/memory-system.md` (dual-channel injection)
- Compression design: see the compression docs under `docs/`
- Regression tests: `python -m pytest tests/` (72 tests); T5 regression set
  `python scripts/phase2_regression.py` (requires a real API)

## FAQ

**Prompted for an API key at launch?** Set the `DUMMY_API` environment
variable to skip the prompt.

**Switch models / providers?** Set `DUMMY_AGENT_BASE_URL` and
`DUMMY_AGENT_MODEL`:

```bash
set DUMMY_AGENT_BASE_URL=http://localhost:11434/v1
set DUMMY_AGENT_MODEL=qwen2.5:7b
start.bat
```

**Do tests cost money?** The `pytest` suite is fully mocked — zero API
cost. Only `phase2_regression.py` uses a real API (~¥0.1 per run).
