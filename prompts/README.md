# prompts/ —— 模型提示词正文（外置资产，非 Python 包）

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这个目录是干什么的、先看哪个文件、
> 哪些是入口契约（版本 / 片段 / 变量）、去哪看更多。
>
> **文件清单与符号不在这里** —— `docs/代码地图/00-符号清单.md` 由 `scripts/make_code_map.py`
> 生成，而该生成器默认只扫 `src`、`scripts`、`eval`，**不含本目录**；本目录的清单看磁盘即可。

## 1. 这个目录放什么

提示词**外置成 Markdown**：**提示词即配置，版本 = 内容哈希**（照 AIHOT，见 `docs/04 §2`）。
分两类：

- **任务主体**（能被独立调用）：`summarize-paper.md` / `summarize-doc.md` / `summarize-repo.md`
  （摘要三体裁）、`build-topic-map.md` / `plan-route.md`（产物）、`grade-relevance.md`（相关性打分）、
  `revise-route.md`（路线增量修订）、`research-decompose.md`（子问题分解）、
  `memory-extract.md`（长期记忆抽取）、`compact-history.md`（历史压缩）、`suggest-title.md`（会话标题）、
  `verify-support.md`（引用语义支持度）。
- **公共规则片段**（被别的提示词以 `{{> 名字}}` 引入，自身不独立调用）：
  `rules-anti-hallucination.md`、`rules-answer-first-summary.md`、`rules-academic-facts.md`。

目录里**只有 `.md`**；装载器是 `src/scholarscout/summary/prompts.py` 的 `PromptPack`。

## 2. 先看哪个文件

1. **`rules-anti-hallucination.md`** —— 被引用最多的公共片段。
2. **`summarize-paper.md`** —— 一眼看到"任务主体 + 三个 `{{>}}` 片段 + `{{变量}}`"的完整组合。
3. **`build-topic-map.md` / `plan-route.md`** —— 产物类；**`verify-support.md`** —— 支持度判定的
   严格 JSON 输出契约。

## 3. 入口契约（版本 / 片段 compose / 变量 / 哈希）

- **文件名 = 代码里的提示词名**：`PromptPack(directory).compose("plan-route", ...)` 对应
  `plan-route.md`。调用方在 `runtime/titles.py`、`runtime/memory.py`、`runtime/compaction.py`、
  `runtime/research_state.py`、`grading/relevance.py`、`grading/support.py`、`summary/pipeline.py`、
  `tools/{topic_map,route_plan,route_revise}.py`。**改名 / 移动 = 改调用方**（文件不存在即抛
  `PromptError`）。
- **`{{> 片段名}}` 引入片段**（`_INCLUDE` 正则；最多嵌套 5 层，循环引用报错）；
  **`{{变量}}` 填运行时值** —— 模板用到哪个变量，调用方就必须给哪个，缺则早失败（`PromptError`）。
- **版本 = 拼接后（变量未替换）文本的 `sha256[:12]`**（`PromptPack.version_of`）——
  改任一主体或片段，版本必变；变量值不同而模板相同则版本相同（正确：变量属于输入，不属于版本）。
- **两枚哈希别混**：本目录每个提示词各有自己的 **`prompt_version`**（内容哈希，随行落库，如
  `summaries`、判权结果、`support_checks`）。**`system_prompt_hash`** 记的是**系统提示词**，
  由 `src/scholarscout/runtime/prompts.py` 的 `build_system_prompt` 组装、**不在本目录** ——
  改本目录的文件只改各自的 `prompt_version`，改 `runtime/prompts.py` 才会改 `system_prompt_hash`
  （E3 报告据此追溯"哪组数字来自哪版提示词"，见 `docs/05 §3.4`）。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 设计依据与口径：`docs/04-摘要提取方案.md`（§2 提示词外置 / 版本=内容哈希；§7 MinerU）；
  `docs/05-评测方案.md` §3.7（`verify-support` 口径）；`docs/03-目录结构与规则文件.md` §1。
- 代码侧装载与调用：`summary/prompts.py`（`PromptPack`）、`summary/pipeline.py`（体裁→文件名映射）、
  `grading/relevance.py`、`grading/support.py`、`runtime/*`、`tools/{topic_map,route_plan,route_revise}.py`。

## 改这个目录之前先知道

- **公共片段被多处引用**：`rules-anti-hallucination.md` 被 6 处、`rules-answer-first-summary.md`
  与 `rules-academic-facts.md` 各被 3 处 `{{>}}` 引用 —— **改一条片段即改所有引用它的提示词版本**。
- **改提示词不影响历史结果**：版本随行落库，改提示词只影响新资料 / 新会话，历史行不重算
  （`docs/04 §2`）。
- **`verify-support.md` 是严格 JSON 契约**：判定按 `prompt_version` 落 `support_checks`；
  提示词里写死两条 —— ① 证据不足**不许猜**；② 理由必须说明依据的是**标题还是摘要**
  （`docs/05 §3.7`）。
- **别在注释里写真实 URL 之类的多余内容**：片段会被整段拼进调用文本，多一分噪音模型就多一分走偏。
