# runtime/ —— 工具循环与工具注册表（一轮对话的执行引擎）

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。

## 1. 这个目录放什么

**一轮对话的完整生命周期**都在这里：落用户消息 → 组装上下文（系统提示 + 历史 + 台账/画像/记忆注入）
→ 调模型 → 执行工具 → 落助手消息与用量 → 上报事件。它把「模型说要做什么」翻译成对 `tools/` 的调用。

分层位置（单向，见 `docs/03-目录结构与规则文件.md` §2）：
`desktop/` `channels/` → `rpc/` → **`runtime/`** → `tools/` → `{providers, fetch, grading, library, state}`。

**它自己不做这些**（放别处）：不取数（`providers/` `fetch/`）、不判权（`grading/`）、
不碰 SQL（`state/`）、不实现工具本体（`tools/`）。runtime 只做调度。

## 2. 先看哪个文件

1. **`loop.py`** —— **先读它**。单轮循环 + `TurnContext`/`TurnResult`：轮次预算、取消、`stop_reason`、
   事件上报、用量记账都在这里，是理解本层的入口。
2. **`registry.py`** —— 工具注册表：`@tool` 注册、规范名↔线上名映射、`get_tools`/`invoke`。
3. **`toolctx.py`** —— 注入给工具 handler 的上下文（库/配置/出网/模型）与本轮预算账 `TurnBudget`。
4. **`context_budget.py`** —— 省钱三招（去重/截断/折叠）；**`compaction.py`** —— 跨阈值时的模型摘要压缩。
5. **`research_state.py`** —— 跨轮研究台账（子问题、已搜查询判重、缺口）。
6. 其余按需读：`subtasks.py`（顺序子任务与并行扇出）、`memory.py`、`learner_profile.py`、
   `research_view.py`、`practice.py`、`titles.py`、`prompts.py`。`__init__.py` 只是包标记。

## 3. 哪些是 API（外部可依赖的契约）

契约的权威副本是 `docs/01-接口契约.md`（§3.2 会话/对话、§5 工具契约、§6 内部边界、§5.17/§5.18 子任务）。
改下面任一签名 = 破坏契约，须同步调用方与测试。

- **注册表 `registry.py`**：`tool()`、`register()`、`ToolSpec`（`.to_openai()`）、`get_spec()`、
  `get_tools()`、`openai_tools()`、`invoke()`、`wire_name()`、`canonical_name()`、`registered_names()`、`TOOLSETS`。
- **循环 `loop.py`**：`run_turn()`、`build_messages()`、`mark_system_prompt()`、
  `TurnContext`（`.tools()` / `.tool_context()` / `.is_cancelled()`）、`TurnResult`（`.as_dict()`）。
- **上下文 `toolctx.py`**：`ToolContext`、`TurnBudget`（`.remaining` / `.can_spend()` / `.spend()`）。
- **上下文成本 `context_budget.py`**：`project_tool_result()`、`collapse_old_tool_results()`、
  `estimate_tokens()`、`messages_token_report()`。
- **子任务 `subtasks.py`**：`run_subtask()`、`run_subtasks()`、`fetch_many()`、`open_goals()`、
  `SubTaskResult`、`delegate_budget()`、`charge_budget()`。
- **台账/压缩/记忆/画像**：`research_state.load_state/save_state/similarity/find_repeat/compute_gaps/render_note`、
  `compaction.should_compact/compact/save_summary/render_summary_note`、
  `memory.render_facts_note/extract_facts/should_extract/mark_extracted`、
  `learner_profile.build_profile/render_profile_note`。
- **提示词 `prompts.py`**：`build_system_prompt()`、`system_prompt_hash()`。
- **看板 `research_view.py`**：`build_board()`、`build_overview()`、`build_evidence()`、`build_timeline()`。

调用方：`tools/*`（拿 `ToolContext`）、`rpc/session.py`（`chat.send` 调 `loop.run_turn`）、
`rpc/research.py` / `rpc/learning.py`（看板 / 画像）、`scripts/`（诊断脚本）、`eval/`。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 测试：`tests/runtime/`（单元层，如 `test_loop.py`、`test_registry.py`、`test_subtasks.py`、
  `test_context_budget.py`、`test_compaction.py`、`test_memory.py`、`test_learner_profile.py`）。
- 口径与设计依据（**权威副本，改口径先改它们**）：
  - `docs/01-接口契约.md` §3.2 / §5.0 / §5.17 / §5.18 / §6
  - `docs/03-目录结构与规则文件.md` §2（模块边界）、§7（真机坑清单）
  - `docs/16-Hermes架构对照与借鉴方案.md`（子任务与并行扇出的来历）
- 符号与行数：`docs/代码地图/00-符号清单.md`（生成物，勿手改）。
- 提示词骨架在 `runtime/prompts.py`，正文片段在 `prompts/`。

## 改这个目录之前先知道

- **工具名有两副面孔**：注册名是带点的**规范名**（`research.search`，唯一真相，事件/轨迹/论文都用它）；
  发给模型的 `tools[].function.name` 必须换成**线上名**（点→双下划线，`research__search`）。原因：
  OpenAI 兼容 API（DeepSeek 实测）只接受 `^[a-zA-Z0-9_-]+$`，带点直接 **400**，整个工具层当场不可用。
  映射在 `registry.wire_name()` / `canonical_name()`，`get_spec()` 两种名都认；注册名含 `__` 会被拒。
- **轮次预算 = 工具预算 + 1**：最后一轮**不给工具**（`tools=None`），专门留给模型把结果讲完。
  否则模型刚好用满工具预算就被判 `tool_limit`，最终答案丢失（`loop.py` 注释与 `docs/03 §7` 都记了这条）。
- **上下文成本三招**（`context_budget.py`）：去重（同来源只回锚点）+ 截断（`snippet`/整条设上限）+
  折叠（旧工具结果压成一行）。**去重只砍摘要与重复的 id 列表，`source_id` 一条都不能丢** ——
  丢了模型就会开始编 id，守卫拦下后重试，成本反而更高。
- **一本预算账**：`TurnBudget` 由主循环与子任务（`research.delegate`）**共用** —— 委派子任务实际消耗的
  工具调用数会 `spend` 回同一个账，主循环剩余预算因此变少（`docs/01 §5.18`）。
- **默认工具集是 `research`**：`get_tools()` 在 `toolsets` 为空/`None` 时只取 `research` 工具集。
  工具集白名单只有 `research` 一个 —— 曾经的 `kb` 工具集与 `features.enable_kb` 因为
  「开了什么都不发生」被整批删掉（docs/23，不留假功能）。见 `registry.py` 与
  `tests/runtime/test_registry.py`。
- **台账写者只有一个**：查询判重台账只由 `loop.py` 在**搜成功**后 `record_query` 并落库，工具只读不写
  （两个写者会丢更新）。无 `session_id` 时不做判重。
- **取消是步间检查**：`chat.cancel` 只在当前这一步（模型调用或工具调用）结束后生效，界面必须如实提示。
- **改 `runtime/prompts.py` 会改 `system_prompt_hash`**：哈希写进会话，历史回答就与新提示词不可比（`prompts.py` docstring）。
  **别与外置提示词文件的那枚哈希混**：`prompts/*.md` 各有自己的 `prompt_version`，改它们**不动** `system_prompt_hash`
  （两者的区分见 `prompts/README.md`，权威口径在 `docs/05`/`docs/04`）。
