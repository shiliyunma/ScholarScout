# summary/ —— 摘要流水线

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的四问组织，只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、哪些是 API、去哪看更多。
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。

## 1. 这个目录放什么

把"取到的正文"变成"入库的摘要"：**一次模型调用 + 确定性守卫**，不分块、不做 map-reduce、不做第二次"修复"调用（照 AIHOT；`docs/04 §1`）。这是 `source.fetch` 流水线的核心一段。

四件事：

- `pipeline.py` —— 七步流水线（取正文 → 清洗 → 截断+拼提示词 → 调模型 → 解析 → 守卫 → 落库）。**落库不在这里**，由 `state/ingest.save_summary` 做。
- `prompts.py` —— 提示词装载（`{{> 片段}}` 引规则片段、`{{变量}}` 填值）；**版本 = 拼装后（未替变量）文本的 `sha256[:12]`**。
- `guards.py` —— 确定性守卫：清洗、截断、长度合规与**确定性压缩**、数字可溯源。
- `settings.py` —— 流水线参数（与 `config.example.yaml` 的 `summary:` 段对应）。

关键边界：**正文不入库、不进 FTS，只入摘要**（`docs/04 §1`、§10）；正文落 `data/text/`。摘要里出现、原文里找不到的数字会被 `guards.ungrounded_numbers` 挑出来 —— 这是反幻觉规则的**可执行版本**。

## 2. 先看哪个文件

1. **`pipeline.py`** —— 先读 `summarize()`：唯一对外入口，也定义了体裁选择、降级与守卫的编排。
2. **`guards.py`** —— 确定性部分（清洗/截断/长度/数字可溯源），最容易被论文验证，也最该先锁死。
3. **`prompts.py`** —— 提示词怎么装载、版本怎么算。
4. **`settings.py`** —— 参数（截断、长度上下限、要点上限、是否查数字）。
5. `__init__.py` —— 包标记。

## 3. 哪些是 API（外部可依赖的契约）

调用方在 `tools/source_fetch.py`（`SummaryInput` / `summarize`）、`state/ingest.save_summary`（消费 `SummaryDraft`）；另有 `grading/relevance.py`、`runtime/compaction.py`、`runtime/titles.py` 复用 `summary.prompts`。改签名 = 破坏契约。

- `pipeline.SummaryInput`（输入：`title` / `body` / `source_type` / `extract_status` / ...）、`pipeline.SummaryDraft`（输出，含 `degraded` / `guard_failed` / `ungrounded`）、`pipeline.ParsedSummary`
- `pipeline.summarize(llm, item, *, settings, prompts=None)`、`pipeline.choose_kind(...)` / `pipeline.prompt_name_for(kind)` / `pipeline.variables_for(kind, item)` / `pipeline.parse_output(text)` / `pipeline.prompts_dir_of(settings)`
- `guards.clean_text` / `guards.clamp_text` / `guards.length_ok` / `guards.compact_summary` / `guards.count_sentences` / `guards.numbers_in` / `guards.ungrounded_numbers`
- `prompts.PromptPack`（`raw` / `compose` / `version_of` / `available`）、`prompts.PromptError`
- `settings.SummarySettings`

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 设计依据与口径（**权威副本，改口径先改它们**）：
  - `docs/04-摘要提取方案.md` §2（AIHOT 实测锚点）、§4（七步）、§5（提示词文件）、§6（三档字段）、§8（怎么证明摘要好）、§10（与检索的边界）
- 提示词（外置，改标准不用改代码）：`prompts/summarize-paper.md`、`prompts/summarize-doc.md`、`prompts/summarize-repo.md`，规则片段 `prompts/rules-answer-first-summary.md`、`prompts/rules-academic-facts.md`、`prompts/rules-anti-hallucination.md`
- 参数：`config.example.yaml` 的 `summary:` 段
- 守这一层的测试：`tests/summary/`（`test_pipeline` / `test_guards` / `test_prompts`）；端到端在 `tests/tools/`；题集与指标见 `docs/05-评测方案.md` 与 `eval/sets/e2_guard.jsonl`

## 改这个目录之前先知道

- **一次调用，不做分块**：正文先 `clamp` 到阈值再送模型（`docs/04 §5` 拼装顺序固定：体裁提示词 → 答案前置 → 学术数字清单 → 反幻觉）。长度不合规用**确定性裁剪**（`guards.compact_summary`），**不发起第二次修复调用**。
- **三档字段各有用**（`docs/04 §6`）：`title_zh`（列表展示 + FTS 检索）、`summary_short`（答案前置，FTS 检索 + 卡片）、要点（学习路线原料 + 抽背卡）。注意文档 §6 里第三档写作 `key_points`，**代码字段叫 `points`**（落库为 `points_json`）；§6 还定义了第四档 `why_relevant`（喂 `source.grade` 的可解释性），`SummaryDraft` 有该字段，但当前 `summarize()` 返回 `None`。
- **正文只落盘、不入库、不进 FTS**：`data/text/<doc_id>.txt` 放正文；库里只有元数据、摘要与路径（`docs/04 §10`）。想搜正文要等将来的向量检索。
- **解析是逐行标签 + 兜底**：`parse_output` 认 `title_zh:` / `summary_short:` / `points:` 标签；丢标签时按行兜底（首行当标题、其余当摘要）。
- **降级不是失败**：模型挂了或没正文时不抛异常，退化成"用元数据出的摘要"并标 `degraded=1`；守卫没修好标 `guard_failed`（`docs/04 §4`）。
- **`prompt_version` 只按模板算**：`PromptPack.version_of` 对**含片段的模板文本**取哈希，**不含变量值** —— 改任意片段或主体，版本必变；变量值不同而模板相同则版本相同（变量属于输入，不属于版本）。
