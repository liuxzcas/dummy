# Dummy Agent

A persistent-memory LLM agent with self-learning capabilities: tool-calling
loop, context compression, full-text search, cross-session memory, skill
system, error learning, and user-supervised self-improvement.

> 中文版: [README_CN.md](./README_CN.md)  |  Changelog: [WHATS-NEW.md](./WHATS-NEW.md)

## Quick Start

**1. Clone and enter the directory**

```bash
git clone https://github.com/liuxzcas/dummy.git
cd dummy
```

**2. Configure environment variables**

| Variable | Required | Description |
|----------|----------|-------------|
| `DUMMY_API` | Yes | API key (DeepSeek / OpenAI / any compatible service) |
| `DUMMY_AGENT_BASE_URL` | No | API base URL, default `https://api.deepseek.com` |
| `DUMMY_AGENT_MODEL` | No | Model name, default `deepseek-v4-flash` |
| `DUMMY_BASH_PATH` | No | git-bash path for the terminal tool (falls back to WSL/cmd on other machines) |

> Compatible with **any OpenAI-format LLM API** (DeepSeek / OpenAI /
> local Ollama / vLLM, etc.) — just set the variables above.
> Requirement: the endpoint must support tool calling.

**3. Launch**

Windows: double-click `start.bat` (auto-detects git-bash for the terminal
tool). Other platforms:

```bash
python main.py
```

Type messages directly to start a conversation; the agent calls tools
automatically. After each reply, a usage line shows model / tokens /
cache hit rate / cost / window usage.

## Commands

| Command | Purpose |
|---------|---------|
| `/search <keyword>` | Full-text search over history + archived tool output (EN/ZH) |
| `/memories` | List remembered facts; `/memories del <id>` to remove |
| `/skills` | List skills; `/skills show <name>` full content; `/skills del <name>` remove |
| `/lessons` | List learned lessons; `/lessons confirm <id>` verify; `/lessons del <id>` remove |
| `/improve <tool>` | Self-improvement: analyze a tool's errors and propose a fix (needs approval; auto-rollback on failure) |
| `/history` / `/history del <n>` | View current session history / delete a record |
| `/sessions` / `/sessions del <id>` | List sessions / delete a session (cascades its data) |
| `/resume` | Resume the most recent session |
| `/reset` | Start a fresh session |
| `/tools` | List available tools |
| `/help` | Show all commands |

## Memory Management (/memories)

When a conversation ends, noteworthy facts are extracted automatically
(write-time distillation: same-topic merge, exact-value preservation,
conflict overwrite) and injected into future sessions on demand.
`/memories` lets you view and manage them:

```
/memories
🧠 记忆 (3 条):
  [3] [偏好] 用户偏好中文交流 (conf=0.9, hits=4, 来自 a1b2c3d4)
  [2] [项目] 测试框架为 pytest (conf=0.8, hits=2, 来自 9f8e7d6c)
```

| Field | Meaning |
|-------|---------|
| `[id]` | Memory ID, used for deletion |
| `[category]` | Category: preference / project / tech / other |
| `conf` | Confidence (0~1) |
| `hits` | Times injected into context |
| `来自` | Source session id (first 8 chars) |

## Skill System (/skills)

Turn recurring workflows into reusable skills that the agent selects
automatically by task:

- **Format**: SKILL.md (YAML frontmatter: name/description/type + steps);
  **atomic skills** (single capability, reused across workflows) +
  **workflow skills** (steps reference atomic skills, lightweight orchestration)
- **Triggering**: a skill index (name + description) is injected into the
  system prompt each turn; the LLM matches the task, reads the SKILL.md
  via read_file, and executes the steps
- **Creating**: just say "turn XX into a skill" — the agent runs the
  create-skill flow (interview boundaries → generate → auto-validate →
  save); the new skill is immediately available
- **Built-in skills**: create-skill, search-literature, organize-notes,
  write-summary, literature-review (literature-review workflow)

## Error Learning (/lessons)

Learn from mistakes to avoid repeating them:

- **Generation**: when you correct the agent (strong correction signal
  detected) or a tool errors, an instant reflection distills a one-line
  rule lesson into the lessons store
- **Injection**: each turn, relevant lessons are retrieved and injected
  (≤5, relevance-based, pending ones marked "待验证")
- **Management**: `/lessons` to view; `/lessons confirm <id>` to verify;
  hits ≥2 auto-verifies; `/lessons del <id>` to remove

## Self-Improvement (/improve)

Fix its own defects under your supervision (e.g. a tool that keeps
failing):

- **Detection**: when a tool's error count ≥3 and error rate >30%, the
  agent hints "type /improve X to analyze and fix" (once per session)
- **Flow**: `/improve <tool>` → LLM read-only root-cause analysis
  (proposal: change list + verification plan) → **you approve** → modify
  → auto-verify (syntax → import → pytest → bootstrap gates)
- **Safety**: auto-rollback on failure (git + backup, double insurance)
  plus lesson recording; forbidden files (.git etc.) are never touched;
  every attempt is logged to `logs/self-improve.jsonl`

## Interrupt & Confirm

- Confirmation-required tools (terminal / read_file / write_file) ask
  before running: **Enter** to allow, **n** to reject; write_file
  supports **d** to view the full diff (old lines red, new lines green)
- Type **`/p`** at any time to interrupt the agent — while a tool is
  running, while waiting for a response, or at a confirm prompt. After
  interrupting, enter your instruction and the agent re-plans; plain
  Enter cancels the interrupt
- Live-listener input has no echo

## Project Structure

```
main.py           Entry point (CLI)
core.py           Agent orchestration (loop + injections + learning + self-improve)
llm.py            LLM client (OpenAI-compatible)
tools/            Built-in tools (terminal/read_file/write_file/web_search/web_extract)
session_store.py  SQLite storage + FTS5 search (messages/archive/memories/lessons)
memory.py         Memory extraction (write-time distillation)
lessons.py        Lesson generation (error learning)
skills_manager.py Skill management (SKILL.md parse/validate/index)
self_improve.py   Self-improvement (detect/propose/verify/rollback)
compressor.py     Context compression (L1 folding + L2 incremental summary)
skills/           Skill library (5 built-in skills)
tests/            pytest suite (115 tests)
docs/             Design docs & verification reports
```

## Documentation

- Phase 3 research & design: `docs/phase3-research.md` (skills / error
  learning / self-improvement / safety / metrics / workspace / 3-store boundary)
- Memory system design: `docs/memory-system.md` (dual-channel injection)
- Full-text search design: `docs/fts-search.md` (dual-table scheme)
- Changelog: `WHATS-NEW.md`
- Regression tests: `python -m pytest tests/` (115 tests, fully mocked,
  zero API cost)

## FAQ

**Prompted for an API key at launch?** Set the `DUMMY_API` environment
variable to skip the prompt.

**Switch models / providers?** Set `DUMMY_AGENT_BASE_URL` and
`DUMMY_AGENT_MODEL`:

```bash
set DUMMY_AGENT_BASE_URL=http://localhost:11434/v1
set DUMMY_AGENT_MODEL=qwen2.5:7b
python main.py
```

**Local ollama/vLLM stats accurate?** Cost shows "本地 (无 API 成本)"
(local endpoint); window usage resolves per-model via a mapping table.

**Do tests cost money?** The `pytest` suite is fully mocked — zero API
cost.
