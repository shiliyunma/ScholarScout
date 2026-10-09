# 16 Hermes 架构对照与借鉴方案

> 状态：**v1.0 待拍板** ｜ 2026-10-09
> 起因（用户原话，2026-10-09）：「ScholarScout 这个项目的架构相关信息与 hermes 的架构有哪些是相同的，我希望我这个毕设项目尽可能多的借鉴 hermes 的架构，尤其是**记忆系统**与**子 Agent** 这一块。」
> 参照物：**Hermes Agent v0.21.2**（本机源码 `C:\Users\Lenovo\AppData\Local\hermes\hermes-agent`）。所有 Hermes 结论均**现查本副本**，带 `文件:行号 / 函数名`；读不到的写 `unknown` 或 `我推断`，**不编造内部实现名**。
> 格式沿 `docs/14-Agent能力提升方案.md`（本文原要求的 `docs/15-DeepTutor对照.md` **在本仓库不存在**，已核对 `docs/` 全目录与全盘 `find`，故按 `docs/14` 的风格写）。
> 关联：`docs/01` §1/§3.2/§5.13/§5.14、`docs/02` §8、`docs/03` §1/§2、`docs/05` 指标、`docs/14` D 档。

**本文分四节**：§1 逐层对照表 → §2 记忆系统专题 → §3 子 Agent 专题 → §4 不建议借鉴清单。每条结论都标了证据。

---

## §1 逐层对照表（相同 / 不同 / 可借鉴）

结论只有三档：**可直接照搬**（SS 已具备或能原样借鉴）／**拍扁改造**（思想可用，但要落到"本地单用户桌面 + 4 依赖"的形态）／**不适用**（Hermes 的产品需求，搬来即破坏定位）。

| # | 层 | ScholarScout 现状（证据） | Hermes 做法（源码证据） | 相同点 | 差异原因（是否本地桌面命定） | 结论 |
|---|---|---|---|---|---|---|
| ① | 分层与调用方向 | `ui→rpc→runtime→tools→{providers,state}` 单向；三条硬规则（`docs/01 §1`）；门禁 `tests/test_layering.py` | `cli/gateway/desktop/acp → run_agent.AIAgent → agent/conversation_loop.run_conversation → agent/turn_*.py 阶段 → tools/registry → providers + hermes_state` | **本质相同**：都是"外入口→单 agent 门面→对话循环→工具注册表→provider/state"的单向分层 | 否。Hermes 多一层"网关/多入口"（`gateway/run.py`，5676 行），SS 只有桌面一个入口 | **可直接照搬**（已具备）。Hermes 把循环拆成 31 个 `turn_*.py` 阶段文件对 SS 无必要——`loop.py` 才 652 行 |
| ② | Agent 主循环 | `runtime/loop.py:191 run_turn`；`for _round in range(tool_budget+1)`（:303），最后一轮 `tools=None`（:307）；`stop_reason` 六值；按 session 取消（`TurnContext.running`，:96/:302） | `agent/conversation_loop.py:1573 run_conversation` / `:1423 _run_conversation_turn`；`while api_call_count < max_iterations and iteration_budget.remaining > 0`（:1514）；`agent/iteration_budget.py:25 IterationBudget`（线程安全 consume/refund）；工具并发在 `agent/tool_executor.py` | **本质相同**：ReAct 式"调模型→执行工具→回灌→再调"，都有轮次/工具上限与 `stop_reason` | 否。Hermes 有 per-agent 预算对象、阶段化、并发工具执行、5 类压缩引擎；SS 单进程单轮、顺序执行、三层裁剪（规则+字符+模型压缩） | **拍扁改造**：SS 循环已够；`TurnContext`/`TurnResult`（:86/:116）已等价于 Hermes 的 `_LoopState`，不必再造 |
| ③ | 工具注册与声明 | `runtime/registry.py`：`register`(:76)/`tool`(:88)/`get_tools`(:121)/`openai_tools`(:138)/`invoke`(:142)；**点 ↔ 双下划线映射** `wire_name`(:30)/`canonical_name`(:38)；7 个工具 `tools/__init__.register_tools` | `tools/registry.py register`（技能速查锚点 :654）；`model_tools.py:867 handle_function_call`；`toolset` 概念（`toolsets.py`，36 个 toolset，本副本实测） | **本质相同**：都是"声明式注册表 + schema 发给模型 + 统一 handler 路由" | 否。SS 被迫做 `wire_name` 映射是因为 OpenAI 兼容 API 只收 `^[a-zA-Z0-9_-]+$`（`registry.py` 模块注释、`docs/03 §7`）；Hermes 工具名本就含 `_` 不含点，无这层 | **可直接照搬**（已具备）。SS 的映射方案反而更有论文价值，保留 |
| ④ | 上下文装配与提示词 | `runtime/prompts.build_system_prompt`(:126)：BASE + 流程档 + 澄清 + `RESEARCH_STATE_RULE` + 风格档；每轮把 **台账+画像+记忆追加到 messages 尾部**（`loop.py:269-283`）；提要在 `system` 之后历史之前（`loop.py:181`） | `agent/system_prompt.py:605 build_system_prompt_parts` 分 **stable/context/volatile** 三段；`_memory_parts`:460 进 volatile(:652)；`agent/prompt_caching.py` 缓存断点；外部记忆 prefetch 注入到用户轮（`agent/turn_context.py:90 build_memory_context_block`） | **形式相似**：都分层组装，都关心"前缀稳定/服务端缓存命中" | 否。Hermes 三段式是为多缓存断点服务的复杂机制；SS 用"**尾部追加动态块**"达成同样目的（`docs/14` A6，提交 `6c628ea`） | **拍扁**：SS 不必学三段式；"把易变内容放最后"这一条 SS 已在做，照旧 |
| ⑤ | 会话与持久化 | `state/sessions.py` + `messages` 表（`UNIQUE(session_id, idx)`）+ `research_states`（迁移 `0010`）+ SQLite WAL + FTS5(trigram)（`docs/02 §4`）；**正文不入库**（决议 5，`docs/03 §1`） | `hermes_state*.py` 家族（`SessionDB`，schema 见 `hermes_state_schema.py`）+ `messages_fts`/`messages_fts_trigram`（:256）+ `sessions.system_prompt_hash`（:190） | **本质相同**：SQLite+FTS5、按 session 存 messages、记 `system_prompt_hash` 保历史可比（SS：`loop.mark_system_prompt`，:640） | 否。"正文不入库"是 SS 的特有决议（毕设口径），非 Hermes 差异 | **可直接照搬**（已具备） |
| ⑥ | 记忆系统 | **已落地**：`runtime/memory.py` + `user_facts`（迁移 `0012`）+ `runtime/learner_profile.py`（迁移 `0011/0014/0015`）；`loop.py:280/:461` 注入与抽取 | 双账 MEMORY.md/USER.md（`tools/memory_tool_store.py`）+ 可插拔 `MemoryProvider`（`agent/memory_manager.py`）+ 后台复盘 fork（`agent/background_review.py`） | **本质相同**（跨会话事实→注入每轮），但**账本数量与写入时机不同** | 否。差异在"产品复杂度"：Hermes 面向多平台多用户，SS 单用户单机 | **拍扁改造**（**详见 §2**） |
| ⑦ | 子 Agent / 委派 | **没有**。核实：只有单个 `loop.run_turn`，无子进程/子循环；`tools/__init__.register_tools` 只 7 个工具，无 delegate；`docs/14` C1 明确"不建议做多智能体编排" | `tools/delegate_tool.py:417 delegate_task`：建**子 AIAgent**（新会话、父 toolset 减去黑名单、独立预算），父只见 summary | **不同**：SS 完全无此能力 | 否。但 Hermes 的委派是为"上下文爆炸 + 并行独立工作流"设计，SS 单机成本敏感 | **拍扁改造**（**详见 §3**）；真·多智能体**不做** |
| ⑧ | 技能 / 可扩展机制 | 无运行时技能加载；`prompts/*.md` **静态外置**（"提示词即配置、版本=内容哈希"，`docs/03 §1`）；扩展 = 加一个 `tools/*.py` 工具或一个 provider | 技能系统：`agent/skill_utils.py`、`tools/skills_tool.py`（`skills_list`/`skill_view`）、`tools/skill_manager_tool.py`（`skill_manage` 自改）；SKILL.md frontmatter；索引进 volatile 段（`system_prompt.py:299 _skills_prompt`）；`agent/skill_preprocessing.py` 支持 `${HERMES_SKILL_DIR}` 与 `!\`cmd\`` | **形式相似**：SS 的"外置提示词 + 版本=哈希"与 Hermes 的"技能即文件（可版本化/可编辑）"**语义相近** | 否。SS 单用户毕设，无"跨会话复用程序性记忆"的需求 | **拍扁**（可选）：把可复用的"调研套路"沉淀成文件、让模型按需读取（`skill_view` 的最小版）。**非必须，先不做** |
| ⑨ | 评测与可观测 | `eval/`（`metrics.py`/`run_eval.py`/`report.py`/`sets/`）+ `docs/05` 指标（tier 一致率/出处覆盖率/ungrounded…）+ `tool_traces` 表（"所有调用类指标的取数入口"，`docs/01 §3.3`） | `evals/` + `agent/monitoring/` + `agent/insights.py`；工具进度事件 / 实时 transcript | **形式相似**：都有离线评测集与工具轨迹 | 否。SS 把口径写进论文求"可复算"，Hermes 面向线上质量与后台复盘 | **不适用**：SS 评测体系已自成一体，不必照搬 |
| ⑩ | 通道 / GUI | 桌面壳 pywebview + **JSON-RPC 2.0 over stdio**（单一入口，`docs/01 §2`）；`ui/` 原生 HTML/CSS/JS，无构建 | `gateway/run.py` + `gateway/platforms/`+`plugins/platforms/`（22 个平台）+ Electron 桌面（`apps/desktop`）+ dashboard + TUI + ACP（IDE） | **形式相似**：都"渲染层与 agent 核心解耦" | **是，命定不同**。Hermes 多平台是产品需求；SS 单机桌面是课题定位 | **不适用**（见 §4） |

**一句话总览**：SS 的**分层、工具注册、会话持久化**三块与 Hermes **本质同构**（已具备，无需抄）；**记忆**两边同构但我们少两样东西（第二本账、周期复盘）；**子 Agent** 我们完全没有、且不宜照抄全量；**通道/GUI、技能市场、多租户**是 Hermes 的产品外壳，搬来即违反 SS 的定位与硬约束。

---

## §2 记忆系统专题（重点）

### 2.1 Hermes 的记忆到底怎么做的（源码 + 函数/类名级证据）

Hermes 的记忆有**两层**：内置的**文件账本**（默认，永远在）+ 可插拔的**外部 provider**（可选，最多一个）。

**（1）存什么**
- **两本账**：`MEMORY.md`（agent 自己的笔记：环境/约定/工具坑/教训）+ `USER.md`（用户画像：name/role/preferences/style）。证据：`tools/memory_tool_store.py:20 MEMORY_BLOCK_HEADERS = {"memory": "MEMORY (your personal notes)", "user": "USER PROFILE (who the user is)"}`。
- 条目用分隔符拼接：`tools/memory_tool_store.py:23 ENTRY_DELIMITER = "\n§\n"`。
- 字符上限：`memory_char_limit=2200`（≈800 tokens）、`user_char_limit=1375`（≈500 tokens）。证据：`hermes_cli/config_defaults.py:1218-1219`（memory 段）。

**（2）存在哪**
- 目录：`get_hermes_home() / "memories"`。证据：`tools/memory_tool.py:41 get_memory_dir()`。
- 文件名：`USER.md` / `MEMORY.md`。证据：`tools/memory_tool_store.py:184 _path_for()`。
- **不是 SQLite**，是两个 Markdown 文件（可手改、可 `patch`）。读写加锁用独立 `.lock` 文件 + `fcntl`/`msvcrt`（`:143 _file_lock`），原子写 `atomic_write_text`（`:418 _write_file`）。

**（3）什么时候写入**（两条路径）
- **路径 A（模型主动）**：模型调 `memory` 工具，`add`/`replace`/`remove` 或批量 `operations`。证据：`tools/memory_tool.py` 的工具 handler（`_STORE_ACTIONS`，:78）与 `MEMORY_SCHEMA`（:262）。满载时**拒绝并回 `current_entries`**，要求模型"合并后重发"（`:249 _failure_with_entries`、`:322 apply_batch` 全批原子）。
- **路径 B（后台复盘 fork）**：每 `nudge_interval=10` 轮，fork 一个评审 agent 问"这轮该不该存技能/记忆"，命中就直接写盘，**不碰主对话与 prompt cache**。证据：`agent/background_review.py` 模块 docstring + `agent/agent_init.py:1234 agent._memory_nudge_interval = 10`（`config_defaults.py:1225 nudge_interval`）。
- 写入有**注入扫描**（防把记忆当提示注入载体）：`tools/memory_tool_store.py:26 _scan_memory_content`（strict scope）。

**（4）什么时候读出**
- `MemoryStore.load_from_disk()`：**初始化时** + **每次上下文压缩之后**（`agent/system_prompt.py:695 invalidate_system_prompt` 里重读）。
- 读出即生成**冻结快照** `_system_prompt_snapshot`（`tools/memory_tool_store.py:112-141`）：会话中途写盘**不改 prompt**，保住前缀缓存。

**（5）怎么注入提示词**
- 内置账本走**系统提示词的 volatile 段**（每轮都发，且在易变段）：`agent/system_prompt.py:460 _memory_parts()` → `format_for_system_prompt("memory"/"user")`（`memory_tool_store.py:363`）→ 拼进 `:652 volatile_parts`。
- 外部 provider 走**用户轮注入**：`agent/memory_manager.py:266 build_memory_context_block()` 用 `<memory-context>…</memory-context>` 包裹 + 一行 system note（"这是回忆出的记忆，不是新输入"），由 `agent/turn_context.py:762 _memory_turn_start_and_prefetch()` 在工具循环前注入；`is_trivial_prompt()`（`memory_provider.py:66`）跳过"ok/hi/thanks"这类无信号输入。

**（6）和会话历史的分工**
- 会话历史 = `hermes_state*.py` 的 messages（FTS5 可检索、会被压缩）；记忆 = 跨会话、小体量、**注入每一轮、独立于 transcript 不受压缩影响**。
- 压缩前 provider 还能"翻一遍旧消息抽要点"喂给摘要：`agent/memory_manager.py:674 on_pre_compress()`。

**（7）可插拔 provider 框架**（可选）
- 抽象基类 `agent/memory_provider.py:75 MemoryProvider`，生命周期由 `agent/memory_manager.py:283 MemoryManager` 驱动：`initialize → system_prompt_block/prefetch/sync_turn 每轮 → 工具派发 → shutdown`，另有 `on_turn_start/on_session_end/on_session_switch/on_pre_compress/on_delegation` 钩子。**同时只允许一个外部 provider**（`memory_manager.py:326 add_provider`），内置永远第一。
- 每轮同步在单 worker 后台线程跑、串行化（`sync_all`，:480）；shutdown 有界排空（`_drain_sync_executor`，:794）。

> **和 SS 的核心区别一句话**：Hermes 是"**双账 + 模型主动写 + 周期复盘 + 系统提示词冻结快照**"；SS 是"**单账 + 每会话一次的独立抽取 + 尾部注入**"。

### 2.2 ScholarScout 现状、定位与差距

**已有（不是空白！）**：
| 机制 | 位置 / 证据 | 定位 |
|---|---|---|
| 跨会话记忆 | `runtime/memory.py`；表 `user_facts`（**迁移 `0012_user_facts.sql`**：`id,text,kind(text∈preference/fact/goal),source_session,created_at,updated_at`） | 关于**用户**的稳定事实 |
| 抽取 | `memory.extract_facts`(:221)、`should_extract`(:256，"每会话最多一次")、`parse_facts`(:167)；提示词 `prompts/memory-extract.md` | 一次小模型调用，`purpose="memory"` 记账（`loop.py:470`、`rpc/learning.py:201`） |
| 判重 | 本地近似文本 `_find_similar`(:54)，阈值 `DEDUPE_THRESHOLD=0.75`(:35)；语义判重交模型 | 复用 `research_state.similarity`（一把尺子） |
| 注入 | `memory.render_facts_note`(:147)，预算 `NOTE_BUDGET_CHARS=600`(:37)，超限只发最新；注入点 `loop.py:280` | 追加到 messages 尾部 |
| 出口 | RPC `memory.list/add/update/remove/extract`（`rpc/learning.py:134-201`） | "能看能改能删" |
| 画像（掌握度） | `runtime/learner_profile.py`：`build_profile`(:179)、`render_profile_note`(:279)；数据来自 `card_reviews`（迁移 `0011`）+ `card_review_log`（`0014/0015`） | **算出来的**（零模型调用，确定性） |

**差距（对 Hermes，逐条带证据）**：

| # | 差距 | 证据对比 | 影响 |
|---|---|---|---|
| G1 | **只有一本账**：没有"agent 自己的笔记"（环境/工具坑/约定）。现在这类事实被塞进 `kind='fact'`，与用户事实混在一起注入 | SS `memory.py:41 KINDS=("preference","fact","goal")`（单表无 target）；Hermes `memory_tool_store.py:20` 明确分 `memory`/`user` 两本 | 环境/教训类信息与"用户是谁"同池，注入时无法分区、语义判重也会误合并 |
| G2 | **写入时机太稀**：`should_extract` 每会话最多一次（`memory.py:256-277`），只在"用户≥2 轮或产出过产物"时 | Hermes 有 `nudge_interval=10` 的周期复盘（`agent_init.py:1234`） | 长会话中途出现的稳定事实（例如"我改成用 PyTorch 了"）要等下一次会话才可能被记 |
| G3 | **主模型不能在对话中主动记**：抽取由 loop 在轮末用一个**独立调用**做，主模型从不知道"记了什么" | Hermes 有 `memory` 工具（`memory_tool.py:262`） | 用户说"记住这个"时，SS 没有即时落点（只能等抽取或手点 RPC） |
| G4 | **无注入扫描** | Hermes `_scan_memory_content`（`memory_tool_store.py:26`） | 本地单用户，风险低；**可接受不补** |
| G5 | **注入预算超限时无"合并引导"**：只静默丢最旧的注入行 | Hermes 满载会**拒绝 add 并回 `current_entries`** 让模型合并（`memory.py:249`）；`apply_batch` 全批原子（:322） | 记忆一多，注入内容越来越旧；用户/模型都不知道该整理 |
| G6 | **注入位置不同**（非缺陷）：SS 尾部追加（`loop.py:283`），Hermes 系统提示词 volatile 段 | —— | SS 的选择更省（不重发头部）；**保留** |

**一个关键事实（先讲清，免得改造踩坑）**：SS 的注入块是在 `build_messages`（`loop.py:161`）**返回之后**才 `messages.append` 的（`loop.py:283`），也就是说 **台账/画像/记忆不参与 `_fit_history` 裁剪，永远贴在最重要的尾部位置**。这与 Hermes"记忆冻结进系统提示词"是两种达同一目的的做法，改造时别把这条弄丢。

**成本基线（供改造估算）**：`render_facts_note` ≤600 字、`render_profile_note` ≤~200 字；按 `context_budget.estimate_tokens`（chars/3）≈ **每轮固定 +270 tokens**（有内容时）。对比 Hermes 的上限 2200+1375=3575 字符（≈1300 tokens）——**SS 已远比 Hermes 省**，改造时要守住这条。

### 2.3 可落地的改造方案（分 P0/P1/P2）

> 职责划分先于编码（铁律 #9）：每条都先定 **哪一层 / 叫什么 / 放哪个目录**。
> 成本敏感（硬约束 5）：每条给 token 影响与成对开关；**零新依赖**（硬约束 2）。

#### P0-1 ▶ 双账拆分：给 `user_facts` 加 `target`（照 Hermes 的 MEMORY.md/USER.md）｜≈0.5–1 天
- **层与落点**：`state/`（迁移）+ `runtime/memory.py`（渲染/校验）+ `rpc/learning.py`（参数）。**不新建模块**（能在一个函数里解决就不设计一层）。
- **存储（迁移 `0016_memory_target.sql`）**：
  ```sql
  ALTER TABLE user_facts ADD COLUMN target TEXT NOT NULL DEFAULT 'user';
  ```
  取值 `'user'`（谁在用）｜ `'memory'`（agent 自己的环境/约定/工具坑笔记）。**用代码校验**（照 `memory.py:41 KINDS` 的写法），避免 SQLite `ADD COLUMN` 对 `CHECK/UNIQUE` 的限制；`docs/02 §8` 补一段 DDL。
- **接口形状**：
  ```python
  # runtime/memory.py
  TARGETS = ("user", "memory")
  def add_fact(db, text, *, kind="fact", target="user", source_session=None) -> dict: ...  # 增 target
  def render_facts_note(db) -> str:   # 拆成两段注入
      # 【关于这个用户的长期记忆】…（target='user'）
      # 【我自己的工作笔记】…（target='memory'）
  ```
- **写入时机/读出**：不变（沿用 `should_extract`/`extract_facts`）；抽取提示词 `prompts/memory-extract.md` 增一个 `target` 字段让模型分道。
- **与 `_fit_history`/系统提示词的关系**：不变——仍在尾部注入（`loop.py:283`），不进裁剪。
- **成本**：预算仍 600 字（两段合占），**每轮 token 不增**（只是分区）。
- **验证**：`tests/runtime/test_memory.py` 加 3 个用例——target 分道渲染、非法 target 拒绝、`memory.list` 回 target；`scripts/check.sh` 全过。

#### P0-2 ▶ 注入超限时的"合并引导"（照 Hermes `_failure_with_entries`）｜≈0.5–1 天
- **层与落点**：`runtime/memory.py::render_facts_note`（一个函数）+ `prompts/memory-extract.md`（一行）。
- **接口形状**：`render_facts_note` 在"总长 > `NOTE_BUDGET_CHARS`"时，**追加一行**："（记忆较多，已只发最新的；可在设置里整理 / `memory.remove` 删旧项）"；`extract_facts` 在"新增会超预算"时把 `current_entries` 一起回给模型（照 Hermes `_failure_with_entries`），提示"请合并后重发"。
- **成本**：仅在超限时 +1 行（≤40 字）；正常路径零影响。
- **验证**：造 8 条记忆把注入顶满，断言出现引导行且 `render_facts_note` 仍是纯函数（不读钟、可单测）。

#### P1-1 ▶ 周期复盘 nudge（照 Hermes `nudge_interval`）｜≈1–2 天
- **层与落点**：`runtime/memory.py::should_extract` 增参数 + `runtime/loop.py` 调用点 + `config`。**新增配置键 `generation.memory_nudge_turns`（默认 `0=关`）**，保住"成本口径不变"（照 `memory_by_model` 的成对开关风格，`docs/01 §5.13`）。
- **接口形状**：
  ```python
  def should_extract(db, session_id, *, nudge_turns: int = 0, since_turn: int = 0) -> bool:
      # nudge_turns>0 且 距上次抽取的用户轮数 ≥ nudge_turns → True（续写式，复用上次提要）
  ```
- **成本**：开启后约每 `nudge_turns` 轮多一次小调用（`purpose="memory"` 已记账）；**默认关 = 零增量**。给 `docs/01 §5.13` 增一行口径。
- **验证**：`test_memory.py` 断言 `nudge_turns=0` 行为与现状逐字节一致（成本不回归）；`nudge_turns=3` 时第 4 轮触发一次。

#### P1-2 ▶ 主模型可写记忆（照 Hermes `memory` 工具，**arms-gated**）｜≈1 天
- **层与落点**：`tools/` 加一个内部工具 `note.remember`（或把 `memory.add` 暴露给模型），**默认不注册**（`config.ENABLE_MEMORY_TOOL=0`）。
- **为什么必须 gated**：加工具会改 `system_prompt_hash`（`prompts.py:151`、`docs/01 §3.7`）→ **改实验臂、历史不可比**（硬约束 7：改口径=改文档+重跑）。所以**先改 `docs/01` 与提示词档，再动代码**。
- **成本**：工具声明 ≈ +300 字/轮（`docs/14` A4 同量级）；关掉则为 0。
- **验证**：`tests/runtime/test_prompt_arms.py` 断言关开关时哈希不变；开时哈希变且 `docs/01` 有记录。

#### P2 ▶ 记忆搬进系统提示词稳定段｜≈2–3 天，**默认不做**
- 与 A6 缓存策略（`docs/14` A6）冲突：把逐轮变化的记忆放进头部会**让前缀每轮变**、缓存失效。**若坚持 Hermes 式，必须先重新论证缓存收益**。列此仅为"完整对照"，**建议不做**，理由同 §4。

**改造后统一验证口径（写进 `docs/05`）**：记忆类指标只加一条**只读、零模型**的"记忆命中率"——`memory.*` 注入非空的轮次 / 总轮次；以及 `usage_records.purpose='memory'` 的调用次数与花费（可在 `session.usage` 看到，`docs/01 §3.2`）。

---

## §3 子 Agent / 委派专题（重点）

### 3.1 Hermes 的子 Agent 机制（源码 + 函数/类名证据）

**（1）委派入口**
- 单工具 `delegate_task`：`tools/delegate_tool.py:417 delegate_task(goal, context, tasks, max_iterations, role, background, output_schema, images, action, subagent_id, message, parent_agent, credentials_cfg)`；注册在 `:711 registry.register(name="delegate_task", toolset="delegation", …)`，schema `DELEGATE_TASK_SCHEMA`(:608)。单任务（`goal`）或批量（`tasks=[...]`）。

**（2）上下文隔离**
- 子 agent 是**一个全新的 `AIAgent`**（`tools/delegate_tool.py:236` 在 `_build_child_agent` 里 `AIAgent(...)`），带 `ephemeral_system_prompt=child_prompt`（由 goal+context 生成，`:203 _build_child_system_prompt`）、`skip_context_files=True`、**`skip_memory=True`**（:240）。
- **父只看得到委派调用与最终 summary，看不到子 agent 的中间工具调用与推理**（模块 docstring :9-11）。
- 子有自己的 `session_id`、独立 `SessionDB` 句柄（`:85 _open_child_session_db`）、`parent_session_id`（血缘）。

**（3）工具集**
- 继承父的 toolset，**减去黑名单** `DELEGATE_BLOCKED_TOOLS = {delegate_task, clarify, memory, send_message, cronjob_manage}`（`tools/delegate_tool_toolsets.py:14`）；children never gain tools the parent lacks（`_resolve_child_toolsets`，:69）。
- `role` 是**深度派生**的，不是模型点名的：`child_depth < max_spawn_depth` 才给 `orchestrator`（能再委派），否则 `leaf`（`delegate_tool.py:189-191`）。

**（4）并行度**
- `max_concurrent_children=10`（`config_defaults.py:1274`）；批量走 `_Batch`/`_run_batch`（`tools/delegate_tool_dispatch.py`）。
- **默认 `max_spawn_depth=1`（扁平）**（`config_defaults.py:1279`）。

**（5）结果回收**
- 只回 summary；按**父剩余上下文**动态裁剪：`_apply_summary_budget`（`tools/delegate_tool_results.py:266`），静态上限 `max_summary_chars=24000`，超了 head+tail 存盘并给 `read_file offset=` 指针（`:187 _trim_summary_with_footer`）。
- 成本归卷：`_rollup_children_cost`（:377）、`_fire_subagent_stop_hooks`（:345，产 `subagent_stop` 钩子）。

**（6）失败与提前终止**
- 子有独立 `IterationBudget`（`delegate_tool.py:247 iteration_budget=None` → 新建，`config_defaults.py:1260 max_iterations=250`）；心跳 `_HEARTBEAT_INTERVAL=30`，空闲 15 周期/卡工具 40 周期判 stale（`delegate_tool.py:72-78`）。
- 控制动作 `action ∈ {list, steer, stop}`（`:432 _handle_control_action`，`delegate_tool.py` schema `:669`）；`stop` 结束单个子、**部分结果仍返回**。

**（7）用户可见性**
- 顶层委派**总在后台跑**，结果为**一条合并消息**（`_DESCRIPTION_HEAD`）；实时 transcript 落 `cache/delegation/live/<id>/task-<n>.log`（`tools/delegation_live_log.py`），是 side channel，不影响消息/缓存。
- `subagent_start`/`subagent_stop` 生命周期钩子（`delegate_tool.py:287`、`results.py:345`）。TUI 有 `/agents` 看板与 `subagent_id` 注册表（`tools/delegate_tool_registry.py`）。

### 3.2 ScholarScout 现在有没有类似能力？——**没有（已核实）**

- 只有一个 `loop.run_turn`（`runtime/loop.py:191`），单进程单轮、顺序执行工具（`for call in reply.tool_calls:`，:370），**无子循环、无子进程、无并发**。
- 注册表只有 7 个工具（`tools/__init__.register_tools`），**没有 delegate**。
- `docs/14 §3` **C1** 已把"多智能体编排（检索员/分析员/审稿员）"列为 **C 档（大改，需先论证）**，并在"明确不建议做"里给了依据：**Anthropic 自述多智能体 token 约 10–15×**（<https://www.anthropic.com/engineering/multi-agent-research-system>）。
- **结论**：不照抄 Hermes 的 `delegate_task`（它是为"上下文爆炸 + 多独立工作流 + 多平台"设计的）。但"把重活拆成隔离的、攒着预算跑的独立步骤"这个**思想**是可拍扁落地的。

### 3.3 拍扁改造方案（本地单用户桌面形态）

**总原则**：不做多进程、不做真·多 agent、不定新依赖（`concurrent.futures` 是 stdlib）。用**同进程内的"子运行"**替代"子 agent"，用**一个总额度池**替代"每个子 agent 独立预算"，**成本上限由 `runtime.max_tool_calls_per_turn` 统一兜住**。

三个候选，按"收益/成本"排序落地（可任一单独做）：

#### MVP ▶ P0：多来源并行检索（`runtime/subtasks.py::fetch_many`）｜**已落地**
- **它替代什么**：`research.search` 里"逐个来源顺序检索"的循环 —— 换成线程池并发扇出（Hermes 里"并行子 agent 各抓一路"的**思想**，但不是 agent）。
- **层与落点**：`runtime/subtasks.py`（新文件，runtime 层；命名刻意用 `subtasks` 不用 `agents`，明示"不是 agent"）；**只在 `tools/research_search.py::research_search` 这一处被调用**（替换原 `for name in wanted:` 顺序循环）。
- **接口形状**（provider 维度，不是 source_id 维度）：
  ```python
  # runtime/subtasks.py
  from dataclasses import dataclass
  @dataclass(frozen=True)
  class ProviderFetch:
      provider: str; ok: bool; n: int
      error: str | None; interval_s: float | None; latency_ms: int | None
      cards: tuple[SourceCard, ...] = ()
      def as_status(self) -> dict: ...   # 键集与旧顺序循环逐字一致

  def fetch_many(ctx: ToolContext, providers: Mapping[str, Any], *, wanted: Sequence[str],
                 query: str, per_provider: int, year_from: int | None = None,
                 sort: str = "relevance", missing: Mapping[str, str] | None = None,
                 concurrency: int | None = None) -> list[ProviderFetch]:
      """线程池并行检索（stdlib concurrent.futures.ThreadPoolExecutor）。
      按 wanted 顺序逐条返回；单来源失败就地兜住、不阻塞其它；**只并行出网，不并发调模型**。"""
  ```
- **成本控制**：并发上限读 `runtime.max_parallel_fetches`（默认 3），`concurrency` 参数可临时覆盖。**不并发调模型**（摘要/判权仍归主循环）——避免费用尖峰。实际限速仍由 `fetch/http.py` 按 host 串行化，调大并发也不会绕过任何来源的 `min_interval_s`。
- **与现有 7 工具的关系**：工具数**不变**；`fetch_many` 是**内部函数**，不注册成模型可见工具、不动 `prompts.py`/`tools/__init__.py`（保住 `system_prompt_hash`）。
- **RPC/事件对接**：不新增 `chat.*` 事件；`provider_status` 的键集与顺序保持不变（既有契约测试钉住）。
- **落库/可观测**：复用 `tool_traces` 现有列，**不新建表**；`docs/05 §3.3` 加两条指标"并行抓取成功率""并行抓取平均时延"。
- **验证**：`tests/runtime/test_subtasks.py`——并发上限生效、单来源失败不影响其它、异常不挂死；既有 `tests/tools/test_research_search.py` 全部保持绿。

#### 进阶 ▶ P1：顺序子任务（ReWOO 式"先规划后执行"）｜≈2–3 天
- **它替代什么**：Hermes 的"orchestrator→leaf"委派。这里**顺序**跑 K 个"mini-loop"，每个子任务**独立消息列表 + 独立小预算**，只把**最终文本**并回主 loop（复刻"父只见 summary"的隔离语义，但不并发、不开进程）。
- **层与落点**：`runtime/subtasks.py::run_subtask/run_subtasks`（同一模块，紧挨 MVP）。
- **接口形状**：
  ```python
  @dataclass
  class SubTaskResult:
      goal: str; text: str; refs: dict[str, list[str]]
      tool_calls: list[str]; usage: dict[str, int]; stop_reason: str

  def run_subtask(ctx: ToolContext, goal: str, *, toolset: str = "research",
                  max_tool_calls: int = 2, emit: Emitter | None = None) -> SubTaskResult:
      """一个隔离的 mini-loop：自建 messages、自己的工具子集与预算，只回最终文本。"""

  def run_subtasks(ctx, goals: list[str], *, per_task_budget: int = 2,
                   total_budget: int | None = None) -> list[SubTaskResult]:
      """顺序跑（不做并发）——成本可预测；total_budget 达上限即停，剩余标 stop_reason='budget'。"""
  ```
- **子任务来源**：复用现有拆解 `research_state.decompose_with_reason`（`docs/14` B1 未做，但 `research_state` 已有 `questions`），把 `questions` 里 `open` 的喂给 `run_subtasks`。
- **防止成本失控**：① **总额度池**（`total_budget`，默认 = 主 loop 剩余预算的一部分，建议 ≤ 主预算的 1/2）；② 每个子任务预算小（默认 2 次工具）；③ **顺序**跑（不是 Hermes 的并发），峰值可预测；④ 走 `usage_records.purpose="subtask"`（新 purpose 值）如实记账，`session.usage` 可见。
- **与 RPC/事件对接**：新增**两类通知**（不动现有 `chat.delta`）：
  `chat.subtask_start {turn_id, seq, index, goal}`、`chat.subtask_done {turn_id, seq, index, stop_reason, refs}`。`docs/01 §4` 事件表补两行 + 契约测试 `tests/contract/test_stream_events.py` 补断言。
- **落库/可观测**：每个子任务的工具调用落 `tool_traces`（`tool_name` 前缀 `subtask:`，或加 `parent_turn_id`/`sub_index` 列——**优先复用现有列**，不轻易加表）；看板 `research.board`（`docs/01 §5.10`）可选加一节"子任务"。
- **验证**：`tests/runtime/test_subtasks.py`——预算上限生效、隔离（子任务不污染主 messages）、失败隔离（一个子任务异常不影响其他）；端到端用 `eval/sets/e3_tasks.jsonl` 对比"总 token"必须落在预算内。

#### 进阶 ▶ P2：审阅 / 评分拆成独立子步骤｜≈1–2 天
- **它替代什么**：Hermes 的"评审 fork"（`agent/background_review.py` 的后台复盘）。这里做成**一次独立的只读核查调用**。
- **层与落点**：`runtime/review.py::review_answer(...)`（一个新文件；或并入 `grading/`——按职责它更像 `grading`：判断）。**建议放 `grading/review.py`**（职责 = 判断，且已有 `support_checks` 可复用）。默认**关**（`generation.review_by_model=false`）。
- **接口形状**：
  ```python
  # grading/review.py
  def review_answer(db, session_id, answer_text: str, *, llm, topic: str | None = None) -> dict:
      """对最终答案做一次支撑核查，产出 {rows:[{claim, source_id, strength}], weak_count, usage}。"""
  ```
- **成本**：每次 1 次小调用（`purpose="review"`），默认关 = 0。
- **与现有关系**：复用 `support_checks`（迁移 `0006`）与 `eval/metrics.py` 同口径（`docs/01 §5.10`）。
- **不做**：真·多进程并发子 agent（C1，成本 10–15×，`docs/14`）。

**统一验证**：`docs/05` 增一节"拆解式执行的指标"——子任务数、子任务预算使用率、总 token 对比串行基线（用 `eval/sets/e3_tasks.jsonl` 复测），口径与现有 `usage_records` 对齐。

---

## §4 不建议借鉴的（省时间的部分）

| 不建议抄 | Hermes 里的位置 | 理由 | 强行借鉴会破坏哪条硬约束 |
|---|---|---|---|
| **消息通道 / 网关 / 多平台适配** | `gateway/run.py`（5676 行）、`gateway/platforms/`、`plugins/platforms/`（22 个） | 单机桌面毕设不需要 Telegram/Discord/…；这是产品外壳 | 破坏"分层单向 + 前端只调 RPC"（`docs/01 §1`）：多入口会逼出一个 gateway 层 |
| **插件市场 / skills hub / 技能同步** | `tools/skills_hub*.py`（十几个）、`agent/skill_bundles.py` | 单用户、单一提示词集；无分发需求 | 破坏依赖政策（`docs/项目规则 §4`）——市场类实现会引第三方依赖/网络 |
| **多租户 / profiles / 凭证池** | `~/.hermes/profiles/`、`agent/credential_pool*.py`、`agent/credential_sources.py` | 只有一个用户、一个 `.env` | 破坏"密钥只进 .env"（硬约束 4）与"职责划分"（多一层身份解析） |
| **可插拔记忆 provider 框架** | `agent/memory_manager.py`、`agent/memory_provider.py` | 我们只有一本/两本内置账，外部 provider 抽象是**为多后端准备的过度抽象**（模块自己都限制"同时只能一个外部"） | 违反"不要为了好看堆抽象"（本文纪律）——一个 `runtime/memory.py` 足够 |
| **MCP / 外部工具协议** | `mcp_serve.py`、`agent/native/` | 无外部工具生态需求 | 破坏 4 依赖边界（`docs/项目规则 §4`） |
| **多引擎上下文压缩 / context_engine** | `agent/context_engine.py`、`agent/context_compressor.py`（5299 行）、`micro_compaction.py`、`native_compaction.py` | 我们已有三层压缩（规则+字符+模型，`docs/01 §5.14`），再叠引擎是浪费 | 违反"能用一个函数解决就不要设计一层" |
| **MoA / curator / cron / kanban / estop** | `agent/moa_loop.py`、`agent/curator.py`、`cron/`、`agent/kanban_stop.py`、`agent/estop.py` | 全是后台自治/多 agent 编排件，成本成倍 | 破坏"用户成本敏感"（硬约束 5） |
| **服务端缓存断点三段式系统提示词** | `agent/prompt_caching.py`、`system_prompt.py` stable/context/volatile | 我们用"尾部追加动态块"已达目的（更简单） | 会与 A6 的简单缓存策略冲突（`docs/14` A6） |
| **桌面 Electron / dashboard / TUI / pets / widgets** | `apps/desktop/`、`hermes dashboard`、`ui-tui/`、`agent/pet/` | 技术栈已锁定"原生 HTML/JS + pywebview" | 破坏技术栈锁定（`docs/项目规则 §1`：禁 Electron/Node） |
| **AIAgent 作为"子 agent"整体复制** | `tools/delegate_tool.py` 的 `_build_child_agent` | 它是重量级（新会话+新 DB 句柄+心跳+凭证租约+成本归卷）；SS 若要"子"，用同进程 mini-loop 即可 | 破坏"单进程桌面 + 4 依赖 + 成本敏感" |

---

## §5 unknown / 我推断 清单（未在源码直接读到、不当作事实）

| 项 | 状态 | 说明 |
|---|---|---|
| Hermes 内置记忆的"具体注入字符位置在系统提示词里的第几段" | 已核实 | volatile 段（`system_prompt.py:652`），**非** unknown |
| Hermes `nudge_interval` 触发复盘时**具体读了哪些消息** | `我推断` | 只核到 `background_review.py` docstring 说"fork 评审 agent 问该不该存"，未逐行读其读取范围 |
| Hermes 外部记忆 provider 里各具体后端（mem0/honcho…）的行为 | `unknown` | 本副本 `plugins/memory/` 只见到 `supermemory` 等目录名，未拆具体实现 |
| Hermes `tool_executor.py` 并发执行的**精确并发度与串行例外** | `我推断` | 技能锚点给的是 `:1419`（线程池并发）/:1762（交互工具串行），未逐行读 |
| ScholarScout `NOTE_BUDGET_CHARS` 之外是否有 UI 层的记忆整理入口 | `unknown` | `rpc/learning.py` 有 `memory.remove` 等，但前端是否已接线未核（`ui/` 未读） |
| `docs/15-DeepTutor对照.md` | **不存在** | 已用 `docs/` 全目录 + 全盘 `find -iname '*对照*'` 核对，确无此文件；本文按 `docs/14` 风格写 |
| SS 改 `user_facts` 加列后 `memory.list` 前端消费是否需同步改 | `我推断` | 需看 `ui/app.js` 的消费代码（本次未读前端） |

---

## 附：一句话决策建议

1. **记忆**：先做 **P0-1 双账 + P0-2 合并引导**（零增量 token、零新依赖、1–2 天）；**P1-1 nudge** 与 **P1-2 主模型可写** 做成**成对开关**（默认关，保成本口径）；**P2 搬进系统提示词段**不做（与缓存冲突）。
2. **子 Agent**：先做 **P0 并行抓取（一个函数 + 线程池）**，验证收益后再考虑 **P1 顺序子任务**（总额度池兜成本）；**真·多智能体不做**（C1，`docs/14` 已给依据）。关键是**不要为好看堆一层框架**。
3. **其余八层**：SS 的**分层 / 工具注册 / 会话持久化**三块已与 Hermes 同构，**不用动**；**通道、技能市场、多租户、记忆 provider 框架**按 §4 不借鉴。
