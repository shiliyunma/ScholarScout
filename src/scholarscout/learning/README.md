# learning/ —— 学习算法层（纯函数：零数据库、零模型调用）

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **行数、文件清单、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。

## 1. 这个目录放什么

只放**确定性的学习算法**：间隔重复（SM-2）、类型化掌握度、学习路线的增量补丁。
三件事的共同点是"**可复算**"——同样的输入必然算出同一个结果，**不碰数据库、不调模型、不联网**。

- 它是**领域逻辑层**（与 `grading/`、`library/` 同级）：可以用 `state/`，但**不许** import
  `runtime` / `rpc` / `desktop` / `tools`（规则表见 `tests/test_layering.py::RULES["learning"]`）。
- **落库与队列不在这里**：在 `state/learning.py`（对外门面）与
  `state/learning_cards.py` / `learning_routes.py` / `learning_topics.py`（实现）。
  本层的纯算法被那些仓储调用，算法与持久化分开。
- **时间一律靠传入的 `now`**，函数里不读钟 —— 否则测试没法钉住"下次是什么时候"这类口径。
- 本目录**没有 `__init__.py`**（对照：其余顶层包都有）；调用方按
  `from scholarscout.learning import srs` 这种方式导入单个模块。

## 2. 先看哪个文件

1. **`srs.py`** —— SM-2 间隔重复：三档评价 → q 值、EF 更新公式、答对/答错的间隔规则、
   `due_at` 为空算到期。**先读它**，它的取舍（为什么用 SM-2 不用 FSRS）写得最清楚。
2. **`mastery.py`** —— 类型化掌握度 +「门槛即游标」：近因加权正确率 × 置信度封顶 × 遗忘惩罚；
   推进 = 扫第一个没到门槛的知识点（不用 stage 计数器，天然支持跳级）。
3. **`route_patch.py`** —— 学习路线的增量补丁引擎：只认白名单字段、重排周号、
   `source_id` 必须是库里真实存在的 id（不做"尽力而为"的忽略）。

## 3. 哪些是 API（外部可依赖的契约）

调用方在 `state/learning_cards.py`（`from scholarscout.learning import srs`）、
`tools/route_revise.py`（`learning.route_patch`）、`runtime/learner_profile.py`、
`rpc/learning.py`。改签名 = 破坏契约。

- `srs.py`：`class Schedule` / `next_ease(ease, quality)` /
  `review(*, rating, ease, interval_days, streak, lapses, now)` /
  `describe_interval(interval_days)` / `is_due(due_at, *, now)` / 常量 `QUALITY`
- `mastery.py`：`normalize_type(raw)` / `type_from_tags(tags)` / `quality(rating)` /
  `recency_weight(age_days)` / `mastery_level(*, rating, reviews, age_days, lapses)` /
  `gate_threshold(knowledge_type)` / `is_mastered(level, knowledge_type)` /
  `next_objective(items)` / `summarize(rows)`
- `route_patch.py`：`class PatchError` / `apply_ops(plan, ops, *, allowed_source_ids)` /
  `parse_patch(content)`

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- **排期口径（SM-2 三档 → q、EF、间隔、`due_at` 为空算到期）**：`docs/01-接口契约.md` §5.11。
- **掌握度 / 学习者画像口径**：`docs/01-接口契约.md` §5.12（画像实现落在 `runtime/learner_profile.py`）。
- **算法出处与候选登记**：`docs/14-Agent能力提升方案.md` §2（SM-2 出处）与 §3（B6 抽背间隔重复）。
- **表结构**（`card_reviews` / `card_review_log` / `artifact_versions`）：`docs/02-数据模型.md`
  §8 / §8.1 / §8.3。
- 测试：`tests/learning/`（`test_srs` / `test_mastery` / `test_route_patch`）。
- 符号与文件清单：`docs/代码地图/00-符号清单.md`。

## 改这个目录之前先知道

- **纯函数**：不读钟（要"现在"就传 `now` 进来）、不连库、不调模型。落库是 `state/` 的事。
- **口径只定义一次**：`mastery.py` 的门槛表、`srs.py` 的 EF 公式与分界 —— 调用方不许自己
  拍一个阈值或权重，要调就改这里（改了这里，界面、画像、排期会一起变，因为它们共用同一套）。
- **`route_patch` 是"宁可报错也不静默改坏"**：操作名 / 字段名 / 条目类型 / `source_id` 写错就抛
  `PatchError`；应用完会重排周号，删周后编号仍连续（否则界面与统计会错位）。
- **本层可以读 `state`，但不能反过来依赖上层**；新增文件时别忘了它在哪一层 —— 门禁
  `tests/test_layering.py::test_every_top_level_package_has_a_rule` 会拦没有规则的顶层包。
