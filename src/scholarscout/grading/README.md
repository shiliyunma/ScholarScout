# grading/ —— 来源判权层

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **行数、文件清单、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。
> 阈值表也不在这里复述（"Duplication is evil"），权威副本见 `docs/02 §3.1` 与代码本体。

## 1. 这个目录放什么

把"**这份资料算不算权威、是否与主题相关**"变成一个**可复现的运算**，而不是模型的自由心证。

它是**纯计算层**：不联网、不落盘业务状态，只依赖 `state/db.py` 读规则表与表结构。
判权结果由调用方（`tools/source_grade.py`）写库。

两条轴**刻意分开**，是本层最重要的设计事实：

| 轴 | 谁算 | 为什么 |
|---|---|---|
| 权威性 `authority` | **规则表**（确定性、可审阅、可 diff） | "判断权威性"是本课题的核心主张，必须能回答"这个判断是怎么来的" |
| 相关性 `relevance` | 本地打分（默认）或模型（给了主题时） | 语义相关性只有模型判得准；本地打分是零成本可复现的兜底 |

**相关性刻意不使用 tier**（照 AIHOT：分级只用来调门槛，不让"来源名气"污染相关性判断）。
论文与答辩里这是必答的一条，对照臂 `A_tier_in_relevance` 就是反着做来量化代价的。

## 2. 先看哪个文件

1. **`tiers.py`** —— 四级分层怎么查表命中（匹配优先级、子域、权重、兜底）。**先读它**，因为
   权威性是确定性的、最容易讲清的。
2. **`relevance.py`** —— 相关性怎么打分、四种结论（核心/相关/外围/排除）的阈值在哪。
3. **`support.py`** —— 补"覆盖率 ≠ 语义支持度"那个洞：这条资料**是否真的支持**这句断言。
   与 `tools/` 的守卫是两回事（守卫只校引用 id 真不真）。
4. **`__init__.py`** —— 只是个包标记，没有逻辑。

## 3. 哪些是 API（外部可依赖的契约）

调用方在 `tools/source_grade.py`、`tools/topic_map.py`、`tools/route_plan.py`、`eval/run_eval.py`
与 `tests/grading/`。改签名 = 破坏契约，必须同步这些地方。

- `tiers.load_rules(db)` / `tiers.classify(url=, domain=, source_type=, rules=)` /
  `tiers.classify_source(db, source, rules=None)` / `tiers.authority_for(tier, weight)`
- `relevance.local_relevance(...)` / `relevance.model_relevance(...)` /
  `relevance.verdict_for(authority, relevance)` / `relevance.topic_terms(topic)` /
  `relevance.term_coverage(term, haystack)` / `relevance.parse_model_output(text)`
- `support.build_pairs(...)` / `support.build_messages(...)`（判定提示词在 `prompts/verify-support.md`）

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 设计依据与口径（**权威副本，改口径先改它们**）：
  - `docs/02-数据模型.md` §3 / §3.1 —— 表结构、四级语义、权威性公式、判定阈值表
  - `docs/05-评测方案.md` §3.1（E1 判权指标）/ §3.7（引用语义支持度）
  - `docs/04-摘要提取方案.md` —— 判相关性为什么吃得到摘要
- 守这一层的测试与题集：`tests/grading/`（单元层）、`tests/eval/test_eval_sets.py`（题集护栏）、
  `eval/sets/e1_grade.jsonl`（判权题集）
- 相关提示词：`prompts/grade-relevance.md`、`prompts/verify-support.md`

## 改这个目录之前先知道

- **规则是数据，不是代码**：补分层规则走**新增迁移**（`state/migrations/0004+`），
  不改 `0002` / `0003` —— 那两份是"当时补了哪些缺口"的历史记录。
- **兜底的含义容易被误读**：`matched_rule_id IS NULL` 只在 `source_type` 不在已知集合时出现；
  普通网页会被 `source_type: web` 那条规则兜住（也有 id）。所以"有规则命中"≠"这个域名被规则表认识了"。
- **`domain` 规则同时认子域，`domain_suffix` 是纯后缀**：`.gov` 匹配不到 `gov.cn`，
  两者是两条独立规则（迁移 `0003` 补的）。
- **判权结果只追加不覆盖**（`source_grades` 表）：同一来源换主题重判会产生新行，
  "最新一条"靠 `created_at DESC, rowid DESC`。
- **本层不做网络与落盘**：任何"抓取/写入"的活都在 `fetch/`、`tools/`、`state/`，别在这里加。
