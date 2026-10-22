# tools/ —— 模型可见的工具实现层

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。
> 各工具的入参/出参/默认值也不在这里复述（"Duplication is evil"），权威副本见 `docs/01-接口契约.md` §5。

## 1. 这个目录放什么

把「模型能主动做的一件事」实现成一个**工具**：一个模块一个工具，模块里声明入参 schema 与 handler，
注册进 `runtime/registry.py` 的工具注册表。这里是**模型可见层**的落点。

分层位置（单向，见 `docs/03-目录结构与规则文件.md` §2）：
`rpc/` → `runtime/` → **`tools/`** → `{providers, fetch, grading, library, state}`。

**它自己不做这些**（放别处）：取数（`providers/`、`fetch/`）、判权（`grading/`）、
落库（`state/`）、循环与预算（`runtime/`）。工具只**编排**这些下层能力并把结果包成统一包络。
`tools/*` **不许 import `rpc/*`** —— 工具不知道自己被谁调。

**工具清单（规范名）**：`research.search`、`source.fetch`、`source.grade`、`topic.build_map`、
`route.plan`、`route.revise`、`note.save`、`research.delegate`、`py.exec`、`skill.list`、
`skill.load`（这 11 个始终注册）；`kb.search` **只受 `features.enable_kb` 控制、默认关**
（开着才注册，注册集变成 12 个）。`__init__.py` 的 `register_tools(enable_kb=...)` 是注册总入口
（显式、幂等，默认值与产品默认一致＝关），由 `app.build_context()` 调用。

**注册纪律**：每个工具模块导出 `SPEC`（`ToolSpec`）与 `register()`，**不在 import 时自动注册**
（测试清空注册表后能再注册一次，行为可预期）。`source_context.py` 是**共用件不是工具**（无 `SPEC`）。

## 2. 先看哪个文件

1. **`__init__.py`** —— `register_tools()` 就是本版工具清单，**先读它**知道有哪几个工具。
2. **`source_context.py`** —— 生成类工具（`topic.build_map` / `route.plan` / `route.revise`）共用的
   候选来源与模型上下文；**候选来源的三级取法在这里**，读它才看得懂生成类工具"凭什么资料排内容"。
3. **`research_search.py`** —— 主线工具，也是**失败语义与来源解析的样板**：
   `resolve_wanted()` 把 `auto` 展开、`_all_providers_failed()` 决定什么时候必须回 `ok=false`。
4. **`source_fetch.py`** —— 体量最大的一支：抓取 → 原件落盘 → 抽正文 → 全落入 `data/text/` → 摘要入库。
5. 其余按主题读：`source_grade.py`（判权）、`topic_map.py`（知识地图）、`route_plan.py`（路线）、
   `route_revise.py`（路线增量补丁）、`note_save.py`（笔记/抽背卡）、`delegate.py`（委派子任务）。

## 3. 哪些是 API（外部可依赖的契约）

契约的权威副本是 `docs/01-接口契约.md`（**§5 工具契约**：§5.0 通用约定 +
§5.1 `research.search` / §5.2 `source.fetch` / §5.3 `source.grade` / §5.4 `topic.build_map` /
§5.5 `route.plan` / §5.6 `note.save` / §5.9 `route.revise` / §5.17–§5.18 子任务与委派）。
改下面任一签名 = 破坏契约，须同步 `docs/01` 与调用方（`runtime/registry.invoke`、`loop`）。

- **每个工具模块**：导出 `SPEC`（`ToolSpec`）与 `register()`。
- **handler 签名固定**：`handler(ctx: ToolContext, **arguments)` —— `runtime/registry.invoke()` 强制。
- **返回统一包络**：`{ok, data, error, refs}`；**任何进入回答的 id 必须回填 `refs`**。
- `research_search.py`：`research_search`、`resolve_wanted`、`available_providers`、`unavailable_providers`
- `source_fetch.py`：`source_fetch`、`guess_source_type`
- `source_grade.py`：`source_grade`
- `topic_map.py`：`topic_build_map`、`parse_map`、`apply_guards`、`build_flat_map`
- `route_plan.py`：`route_plan`、`parse_plan`、`apply_guards`、`build_skeleton`
- `route_revise.py`：`route_revise`、`render_route`
- `note_save.py`：`note_save`、`render_cards`、`QUALITY_RULES`
- `delegate.py`：`research_delegate`
- `source_context.py`：`candidate_sources`、`attach_text`、`render_sources`、`valid_ids`、`degrade_reason`

**工具集（toolset）**：`runtime/registry.py` 的 `TOOLSETS = ("research",)`；`research` 是默认
（`get_tools()` 在 `toolsets` 为空/`None` 时只取它）。曾经的 `kb` 工具集与 `features.enable_kb`
已在「不留假功能」下整批删除（docs/23）。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 契约与口径（**权威副本，改口径先改它们**）：
  - `docs/01-接口契约.md` §5（各工具的入参/出参/失败语义）
  - `docs/03-目录结构与规则文件.md` §2（模块边界）、§7（真机坑清单）
  - `docs/19-资料命名与放置方案.md`（资料落盘布局与命名，`source.fetch` 用到）
  - `docs/16-Hermes架构对照与借鉴方案.md` §3.3（并行扇出与顺序子任务的来历）
- 提示词：`prompts/plan-route.md`、`prompts/build-topic-map.md`、`prompts/revise-route.md`、`prompts/grade-relevance.md`
- 测试：`tests/tools/`（如 `test_research_search.py`、`test_source_fetch.py`、`test_route_plan.py`、`test_delegate.py`）
- 符号与文件清单：`docs/代码地图/00-符号清单.md`（生成物，勿手改）。

## 改这个目录之前先知道

- **工具名有两副面孔**：注册名是带点的**规范名**（`research.search`，唯一真相）；发给模型的
  `function.name` 必须换成**线上名**（点→双下划线，`research__search`）。原因：OpenAI 兼容 API
  只接受 `^[a-zA-Z0-9_-]+$`，带点直接 **400**，整个工具层当场不可用。映射在 `registry.wire_name()` /
  `canonical_name()`；**注册名禁止含双下划线**（注册即报错）。
- **全部来源失败必须回 `ok=false` + 原因**，不能回 `ok=true + 空列表`：只回空列表会让模型以为
  "这个方向没资料"，反复重试同一查询直到烧光预算并开始编造（真机实测，见 `docs/03 §7`）。
  `research.search` 的判据在 `_all_providers_failed()`：有来源成功过 → 空结果是真实结论，不算失败。
- **生成类工具的候选来源是三级取法**（`source_context.candidate_sources`）：
  ① 给了 `source_ids` 就用它；② 否则取该主题下**判权可用**的来源；③ 再没有就取最近入库的来源。
  模型**不许凭空规划** —— 路线/地图上每条都要能指回库里真实的 `source_id`。
- **守卫 `apply_guards`（`route_plan.py` / `topic_map.py` 各有一份）**：引用 id 必须在候选白名单里，
  否则**剔除**（并计入 `stats` 的 `ungrounded*` / `dropped_edges`）；同一来源的重复条目丢弃。
  **资料类**条目（paper/doc/video/code）挂不上出处才计 `ungrounded`；**行动类**（exercise）本身不声称
  出处，单独计 `exercise_items`（把它算作无出处会反向逼模型给练习硬挂来源 = 真幻觉）。
- **`research.delegate` 与主循环共用一本预算账**：子任务实际消耗的工具调用数会 `spend` 回
  `ToolContext.budget`（`runtime/toolctx.py:TurnBudget`），主循环剩余预算因此变少 —— 不是另起一个池。
  委派失败（开关关 / 额度尽 / 参数非法 / 内部异常）一律 `ok=false` + 原因，**绝不把异常抛回主循环**。
- **参数类错误（如空 `topic`、查询判重）由工具 `raise ValueError`**，交 `runtime/loop.py` 收敛成
  契约错误；**来源/网络类失败**则按上面的包络回 `ok=false`。两种路径别混。
