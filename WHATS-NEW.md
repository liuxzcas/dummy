# What's New — Phase 3(自主学习)

> 时间跨度:2026-08-17 ~ 2026-08-21(commit `304a508` → `4ee4449`,23 个提交)
> 主题:**从"带记忆的 agent"升级为"会学习的 agent"** — 技能沉淀 +
> 错误学习 + 自我改进(用户监管)。
> EN TL;DR: Phase 3 (self-learning) shipped: skill system (SKILL.md +
> create-skill meta-skill + /skills), error learning (lessons + instant
> reflection + /lessons), self-improvement loop (detect → propose →
> approve → modify → verify → auto-rollback, /improve), environment-fact
> injection (datetime / cwd / skill dir), window 128K + local-endpoint
> stats, diff colorization.

---

## A. 技能系统(Step 1 + Step 2)— 流程固化与复用

| 功能 | 说明 |
|------|------|
| SKILL.md 技能格式 | Anthropic 标准:YAML frontmatter(name/description/type)+ 步骤正文;**原子技能 + 工作流技能**(工作流 steps 引用原子技能,轻量编排) |
| 技能索引注入 system | 每轮注入技能列表(名称+描述+类型标记),**渐进式加载**:全文由 LLM 按需 read_file 读取 |
| `/skills` 命令 | 列出 / show <name> / del <name>(技能在 skills/ 目录,git 可回退) |
| create-skill 元技能 | **自举闭环**:用户"把 XX 固化为技能" → 索引触发 → 访谈边界 → 生成 SKILL.md → validate_skill 校验 → 保存 |
| validate_skill | 创建后自动校验(frontmatter/name/description/type/workflow 引用) |
| 技能自定位 | 索引注入技能目录绝对路径(运行时计算,部署自适应,LLM 零定位成本) |

提交:2b7889f / f9074d8 / 7cd0162 / faec942 / fb6d10b / 21753c2
预置技能:`create-skill` / `search-literature` / `organize-notes` /
`write-summary` / `literature-review`(文献综述工作流)

## B. 错误学习(Step 3)— 从错误中吸取教训

| 功能 | 说明 |
|------|------|
| lessons 教训库 | 独立表(与记忆分库):规则(do/don't),状态 pending/verified,hits 统计 |
| 即时反思生成 | 用户纠正(强信号检测,防误报)或工具错误(dispatch 特征)触发旁路 LLM 反思 → 一句话规则入库 |
| 按需检索注入 | 每轮按任务相关性 FTS 检索教训注入(≤5 条,命中不足补高频),pending 标"待验证" |
| `/lessons` 命令 | 列出 / confirm <id>(转 verified)/ del <id> |
| 自动转正 | hits ≥ 2 自动 verified;每轮工具错误反思限 1 次(防烧 token) |

提交:f2e28d7(含 FTS 第四索引源 + LIKE 退化分支 lesson 源修复)

## C. 自我改进闭环(Step 4)— 用户监管下改自己

| 功能 | 说明 |
|------|------|
| dispatch 错误统计 | 每工具 calls/errors/最近 3 条错误样本;`get_tool_stats` 暴露 |
| 自动检测报警 | 错误数 ≥3 且错误率 >30% → 会话内提示一次"输入 /improve X 可分析修复" |
| `/improve <工具>` | 提案(LLM 只读根因分析:改动清单+验证计划)→ **用户批准** → 修改 → 验证 |
| 验证门链 | 语法门 → 导入门 → pytest 全量 → 启动门(core/main 改动) |
| 自动回退 | 验证失败 → git checkout + 备份还原(双保险)+ 记教训 + 报告 |
| 安全三层分区 | 自由区(技能/文档)/ 受控区(核心代码,改必须批准)/ 禁止区(.git,永不触碰) |
| 改进记录 | logs/self-improve.jsonl(§3.5 量化评估数据:状态/工具/前后错误率) |
| 手动测试指南 | docs/phase3-step4-manual-test.md(10 组测试项 + 9 步流程) |

提交:9056bbf / 384e0af / f08a0d4

## D. 环境事实注入(省 token)

| 功能 | 说明 |
|------|------|
| 当前时间注入 | 每轮刷新 `当前时间: ...`(幂等),省去 date 工具调用 |
| 当前工作目录注入 | cwd 绝对路径 + 相对路径基准说明,LLM 不再猜路径基准 |
| 技能目录注入 | 索引首行技能目录绝对路径,零定位成本 |

提交:c4e8aa7 / 0bdca4c / fb43319

## E. 配置与兼容

| 功能 | 说明 |
|------|------|
| 窗口默认 128K | DEFAULT_WINDOW_TOKENS 全局常量(v4-flash 原生 1M,128K 内性能稳定);压缩阈值与窗口显示共用 |
| 本地部署统计适配 | 本地端点(ollama/vllm)成本显示"本地 (无 API 成本)";模型→窗口映射表(17 个常见本地模型) |
| terminal 用 git-bash | 修复 cmd 语法返工(LLM 按 bash 写,cwd 不认)→ bash -lc 执行;DUMMY_BASH_PATH 可配置,回退链 git-bash → WSL → cmd |

提交:9056bbf 之前(窗口/本地统计/git-bash 在 Phase 3 前已合入,c4e8aa7 前的提交链)

## F. 显示改进

| 功能 | 说明 |
|------|------|
| write_file diff 标色 | 旧内容红、新内容绿(overwrite diff / line 模式预览 / append 预览统一规则) |

提交:619e756 / d4dd316

## G. 文档沉淀

| 文档 | 内容 |
|------|------|
| docs/phase3-research.md | 研究(工程实践 > SOTA)+ 三能力域 + 安全保障 §3.4 + 量化评估 §3.5 + workspace 机制 §7 + 三库边界 §8 |
| docs/phase3-step1/2-verification.md | Step 1/2 验证报告(方法论+过程+结果) |
| docs/phase3-step4-manual-test.md | Step 4 手动测试指南 |
| docs/discussion-notes.md | 待讨论事项清单(已全部定稿) |
| docs/scratch/architecture-review.md | 架构评估(不重构结论 + 3 项定向重构待办) |

提交:304a508 / 94bf8be / 9505026 / 983533b / fb43319 / f08a0d4 / 4ee4449 等

---

## 里程碑

- **Step 1 技能机制**(2b7889f):存储/索引注入//skills
- **Step 2 技能创建**(faec942):create-skill 自举 + 校验
- **Step 3 错误学习**(f2e28d7):教训生成 + 按需注入
- **Step 4 自我改进**(9056bbf):检测→提案→批准→修改→验证→回退
- 测试套件:**72 → 115 项全绿**(skills 12 + lessons 16 + self_improve 12 + 既有)

---

# What's New — Phase 3 工具循环加固

> 时间跨度:2026-09-14 ~ 2026-09-18(commit `4ee4449` → `135228e`,44 个提交)
> 主题:**重新划定 Agent 与模型的职责边界** —— 把"替模型做判断"的代码
> 改成"向模型提供事实"的代码。
> EN TL;DR: Tool-loop hardening. The agent stopped judging for the model:
> the verification gate (346-line hard-rule judge) was replaced by a factual
> turn-context report; tool guardrails dropped interception entirely and
> became a pure observer; error/encoding/cost reporting replaced guessing.
> Plus: pluggable UI layer (plain/line/silent), environment encoding probe
> (detect, never set), platform-conventions skill, prompt-cache-safe system
> prompt, and four real bugs fixed (tool-pairing self-heal, cost double-count,
> load_dotenv ordering, llm.chat content extraction).

---

## A. 输出层(新增能力)

| 功能 | 说明 |
|------|------|
| **拼印分离** | `ui.py`:**拼**(业务层产出 `Message` 对象,只有数据不含样式)与**印**(渲染层决定怎么画)拆成两层。将来换 GUI 只改渲染层,业务代码不动 |
| 可插拔渲染 | `DUMMY_UI=plain\|line\|silent`。`plain` 与改动前视觉**完全一致**(可无痛回退);`line` 加竖线与分隔线;`silent` 静音对话流 |
| 竖线的边界 | 只作用于**对话流**(工具调用/结果/思考/回复)。工具内部的确认面板(隐私提示/执行确认/diff)保持原样——它们是"问用户话",不是"叙述过程" |
| `raw` 不静音 | `silent` 下**对话流静音但 `raw` 保留**。理由:`raw` 承载的是程序 stdout(`/help`、会话列表),用户 `> log.txt` 重定向时必须保留 |
| 收口范围 | `main.py` 47 处 + `core.py` 工具循环 3 处 + `tools/` 4 处交互面板 |

提交:`e01b1dc`(收口+渲染器) / `b41d4a1`(接上分隔线)

## B. 环境感知(新增能力)

| 功能 | 说明 |
|------|------|
| **terminal 声明环境编码** | 返回首行从 `[SHELL: git-bash]` 扩展为 `[SHELL: git-bash \| os=win32 \| console=cp936 \| py_io=utf-8 \| fs=utf-8]` |
| **设计红线:探测不设置** | 绝不 `setenv LANG`、不执行 `chcp`、不改 `stdout` —— 那会把"当前平台的解法"写死进代码。2 条测试锁死这条红线 |
| 跨平台探测 | Windows 从 `chcp` 解析;POSIX 从 `LC_ALL`/`LC_CTYPE`/`LANG` 解析;都失败 → `unknown`(不猜) |
| **平台约定速查技能** | `skills/platform-conventions/`:表 + 兜底搜索。含 Windows 写中文 `.bat` 的二选一方案、CRLF、BOM 检查、路径写法、`cmd //c` 双斜杠 |
| `write_file` 报告实际换行符 | 返回值加 `[换行 CRLF]`(读文件头判断,**不是假设**) |

**动机(实测)**:一次任务 18 次工具调用里 **11 次**在跟编码搏斗——模型不知道控制台是什么编码,只能反复调 `chcp`、写 `python -c "decode(...)"` 探测、做 A/B 对照。修后同一任务降到 3 次。

提交:`5698c57`(环境探测) / `c45fa43`(技能表) / `1f6bdeb`(换行符报告)

## C. 判断权归还(核心哲学变更)

| 变更 | 从 | 到 |
|------|-----|-----|
| **收尾验证门** | `verify_stop.py`(346 行硬规则判决者:全量 pytest/晚于编辑/豁免名单/驳回上限 2) | `turn_context.py`(170 行**信息提供者**:陈述本轮改了什么、跑了什么,由模型判断) |
| **工具护栏** | block(第 N 次起不执行) → pause(拦下这一次) | **report(纯观察者,零拦截)** |
| **失败判定** | 子串匹配工具输出(`"[EXIT CODE: "` 当失败标记) | 三类判据:`NOOP_MARKERS` / **解析退出码数字** / 框架级标记 |
| **成本计算** | `prompt×¥3 + cached×¥0.1`(cached 被算两遍) | `(prompt-cached)×¥3 + cached×¥0.1` |

**删除**:`verify_stop.py`(346 行)与其 30 条测试 —— 零消费者,留着会让读代码的人以为它还在工作。

提交:`a02d0c5`(门→说明) / `20156ed`+`d743aed`(护栏两次收紧) / `3537789`(失败判定) / `66d794f`(成本) / `7dac807`(删 verify_stop)

## D. 提示词与缓存

| 功能 | 说明 |
|------|------|
| prompt 第 3 条 | 补"**结果拿不到时该怎么办**":GUI 是否弹出/需人工操作/看不见的环境 → 直接说"验证不了",不要反复构造模拟测试 |
| prompt 第 4 条 | "**一次只调用一个工具**"(原来只说"一步一步来",实测它一次调 4 个 `read_file`) |
| **system prompt 稳定化** | 删僵尸字段"运行时间"、删重复的"当前目录"、"当前时间"降精度到天 → **system 在同一天内逐字节不变** |
| 注入顺序修正 | 会变的内容(日期)必须**最后注入**——原来它是第一个被调用的,被后续注入挤到了中间 |

**动机(实测)**:prompt cache 按前缀匹配,system 变一字节则其后全部按未命中计费(缓存价 ¥0.1/M vs 输入价 ¥3.0/M,**差 30 倍**)。旧实现变更点在第 776 字符,后面 641 字符每轮白算。

提交:`838846d`(第 3 条) / `3635f09`(第 4 条) / `16a3caa`(system 稳定化) / `c45fa43`(注入顺序)

## E. 修复的实际缺陷

| 缺陷 | 现象 | 修复 |
|------|------|------|
| **tool_calls 配对残缺** | Ctrl+C 打断后残缺落库 → 每次发 API 都 400 `insufficient tool messages` | **四层防线**:出口(`_settle_history`)/入口(`resume_session`)/咽喉(chat 两处保险丝)/修正(`/history del` 整组增删) |
| **`load_dotenv()` 顺序错** | `.env` 里 `DUMMY_UI` 等配置**静默失效**(import 时还没加载) | 提到模块顶部,在所有项目模块 import 之前 |
| **`llm.chat()` 取值方式错** | 4 处用 `resp.get("content")` 取值,而真实返回的 `ChatCompletionMessage` **没有 `.get()`** → 内容恒空,且被 `except` 吞掉不报错 | 新增 `llm.message_content(resp)` 作为唯一取值入口 |
| **`is_tool_error` 双向误判** | 成功的 terminal 判为失败、被取消的 write_file 判为成功 | 三类判据 + 4 条回归测试,探针复测 **0 误判 / 0 漏漏判** |
| 跨组件冲突 | 4 处互相矛盾的设计(压缩器重写历史 vs 新增组件等) | 逐一修复并补文档 |

**教训机制受 `llm.chat` 那个 bug 影响最深**:`lessons.generate_lesson` 从 Phase 3 Step 3 起**从未生效过**,但因为不报错,一直没被发现。

提交:`9d27737`(配对自愈) / `8f85477`(load_dotenv) / `860a7c0`(llm.chat) / `3537789`(is_tool_error) / `ed2b234`(冲突审计)

## F. 历史层架构选型(三路并行对照实验)

| 项 | 说明 |
|----|------|
| 方法 | 三个 worktree 并行实现三种方案,用**统一判据**对照 |
| 结果 | 路线 1(受管类)/路线 2(投影)失败;路线 3(单点收口)与路线 5(原子批次)都通过"磁盘永不出现非法状态" |
| **采纳** | **路线 3** —— 因为路线 5 为 SOFT1(磁盘永不非法)牺牲了"Ctrl+C 时保住已完成工具结果" |
| 教训 | **权衡要比"频率 × 单次代价"**,不能只比哪种更优雅。用户推翻了作者原来的优先级判断 |

提交:`b100d43` / `60ea179` / `ee06936` / `097cdfb`

## G. 文档沉淀

| 文档 | 说明 |
|------|------|
| `docs/design.html` / `design-zh.html` | **总设计文档**(中英双版,11 章)。原 Phase 0 文档归档为 `design-phase0*.html` |
| **§2 判断的归属** | 新增章节:哪些判断给代码、哪些给模型(本项目最核心的架构原则) |
| `docs/agent-concurrency-interrupt-survey.md` | 工具并发与打断机制横向调研(1370 行,8 个系统) |
| `docs/interrupt-semantics-plan.md` | 打断语义改进实施方案(894 行) |
| `CHANGELOG.md` | 按主题整理的变更记录 |
| `docs/agent-concurrency-*.md` 等 | 见 `docs/` 目录 |
| `pytest.ini` | 让外部工具能识别本项目的规范测试命令 |

提交:`c4f05b9` / `cbf2b66` / `08d084c` / `198c95c` / `5988026` / `135228e`

---

## 里程碑(本版)

- **测试套件**:**115 → 254 项**(+139),1 xfail
- **代码规模**:7015 行 Python(不含测试与 scripts)
- **工具**:5 个(terminal / read_file / write_file / web_search / web_extract)
- **新增模块**:`ui.py`(257) / `env_probe.py`(105) / `turn_context.py`(170)
- **删除模块**:`verify_stop.py`(346 行 + 30 条测试)
- **技能**:5 → 6(`platform-conventions`)

## 本版确立的核心原则

> **协议合法性归代码,安全确认归人,语义判断归模型。**

判断方法:问"这个问题是协议/事实,还是语义?"

| 类别 | 谁判断 | 例子 |
|------|--------|------|
| 协议合法性 | **代码硬保证** | `tool_calls` ↔ `tool` 配对、角色交替 |
| 安全确认 | **人** | 执行命令前确认、写项目外文件前确认 |
| 语义判断 | **模型** | "任务完成了吗"、"什么算证据"、"这次重复是否合理" |

本版的每一处改动都是这条原则的具体应用。
