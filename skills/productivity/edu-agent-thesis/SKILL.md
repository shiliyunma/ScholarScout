---
name: edu-agent-thesis
description: "Use when 推进大浩的毕设（教育智能体）—— 规划、代码、评测、论文。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [毕业设计, 教育智能体, agent, r, thesis, 大浩]
---

# 大浩的毕业设计（教育智能体）—— 项目全貌

加载本 skill 即可了解毕设全貌，不用扫盘。

## 基本事实

| 项 | 值 |
|---|---|
| 学生 | 张思浩，南京邮电大学 软件工程 2027 届，B23041923 |
| 课题名（暂定）| **面向课程知识库的教育智能体设计与实现** |
| 毕设期 | **2026-09-29 → 2027-01-02** |
| 🔴 硬截止 | **10-10 前**上报《课题汇总表》（姓名/班级/学号/课题/指导教师/地点/类型/类别） |
| 未定项 | 指导教师（10-05 前必须联系上）；09-26 的《申请学生汇总表》是否已交待核实 |
| 类型/类别 | 建议：类型=设计，类别=软件 |

## 门禁与渲染核对的坑（都是踩出来的）

- **门禁多了一项 `code-map`（用户已决定保留）**：它比对 `docs/代码地图/00-符号清单.md` 与当前代码结构。**任何新增/删除/改名的源文件都要先跑 `python scripts/make_code_map.py` 重生清单**，否则门禁红、无法提交。目前门禁共八项：lint / format / types / tests / rules-sync / deps-approval / ui-assets / code-map。**重生清单必须在 `ruff format` / `ruff check --fix` 之后再跑**（格式化会改文件、让刚重生好的清单立刻又过期，表现为：跑完 make_code_map 后 code-map 照样红）。
- **探针量几何前必须关掉 CSS 过渡**：Edge 无头下 `--virtual-time-budget` **不推进 CSS transition**，`getComputedStyle`/`getBoundingClientRect` 会一直停在动画起点 → 会误判“收起/折叠没生效”。量之前先设 `transition: none`，并用最小复现证明声明本身有效。
- **改布局要断言“邻居”**：只断言自己那块会漏掉真 bug。实例：左侧栏收起用 `display:none` 让分隔条退出 grid 流 → 中栏被挤成 **1px**、右栏掉进 6px 轨道，而探针只看侧栏宽度与 display，却报告“成功”。**凡改栅格/分栏，几何断言必须包含相邻栏位。**
- **CSS 权重**：id 选择器会把只写类的规则压掉（`#source-list li.source-row` 1,1,1 vs `.row.is-folded` 0,2,0）。折叠/收起态规则要么带头 id，要么用两类（`.layout.is-side-collapsed`），并**用 `getComputedStyle` 实证**，不能只看类名加没加上。
- **不得使用项目里不存在的设计令牌**：写 `--border/--muted-foreground` 这类本设计没有的令牌，会让**整条声明在计算值阶段静默失效**（边框一起消失）。写样式前先 grep 真实令牌（`--ui-*`）。
- **守卫测试是有价值的，不要把它改松**：`test_all_element_ids_used_by_js_exist_in_html` 真的拦住了“JS 用了 html 里不存在的 id”。守卫被改时（如新包要登记分层）**必须逐条审“是合法扩展还是放水”**。
- **`taskkill //F //PID` 在 git-bash 会被拒**（报无效参数）→ 用 `MSYS_NO_PATHCONV=1 taskkill /F /PID <pid>`。
- **提交信息写文件要在判断门禁之前**：把 `cat > msg.txt` 写在“门禁绿才提交”的分支里，一旦红就跑不到，下次 `git commit -F msg.txt` 会把不存在的文件当新文件、把你追加的一行当成**标题**。先写文件，再判断；必要时 `git commit --amend -F` + `--force-with-lease` 修正（单用户仓库安全）。

## 路径

- **f-string / 普通字符串里嵌中文引号 = SyntaxError，且已反复浪费整轮迭代**（`“…”`、`「…」` 放在 f-string 的花括号表达式或引号内部尤其容易炸）。写这类断言时：把中文引号提到**外层**普通字符串里，或直接改用 `「」`（它们不经 ASCII 引号解析）；写完立刻 `python -m py_compile <文件>` 自检，不要等到门禁。
- **判“元素在容器内部”要用几何包含关系**（`innerRect` ⊂ `outerRect`）而不是看类名或 DOM 结构；探针里“可见”也要按**可视区**（`onScreen`）判，否则会出现“DOM 里在、截图里看不到”的假绿。

- **测并发/时序不要用 `time.monotonic()` 直接比时间戳**：Windows 上 `monotonic()` 粒度约 15.6ms，同一批事件会拿到**完全相同**的值 → 断言恒成立（或恒失败），是**空断言**。改用汇合点（`threading.Barrier`）、事件时间戳或 `time.perf_counter()` + 足够长的等待窗口。
- **"取消只影响一个会话"这类用例，同步一律用 `Barrier(2)` 而不是 Event+硬超时**：让「对端还在模型调用里」成为**屏障给出的因果事实**（主线程在发完 `cancel()` 之后才抵达第二方），"对端还没收尾"用**结果信箱里查不到**（`_results` 只在 `_finish` 里写）来证。桩里写 `assert release.wait(timeout=10)` 等于把机器卡顿变成红：卡过 10s → 这一轮以 `error` 收尾并从池里摘掉 → `cancel` 回 `no_running_turn`，用例红在与产品无关的断言上（实测放大到 10.5s：旧写法 3/3 红，Barrier 写法 3/3 绿）。
- **`isinstance(x, dict)` 判不出标准库的 `WeakKeyDictionary`/`MappingProxyType`**（它们不是 `dict` 子类）→ 包着这个判断的守卫会**静默失效、从不触发**（实例：`detach_threads_from_exit_join` 因此永远不摘线程，关窗照卡）。要判映射用 `collections.abc.MutableMapping`，或干脆不用 isinstance 而直接操作并断言结果。
- **关窗/退出时先确认没有轮次在跑**：在 worker 还在写库时关 `Database`/HTTP 会直接**进程级崩溃**（实测 0xC0000005，worker 停在 `sessions.append_message`）。收尾路径要读 `runtime.busy`，忙时早退。

- **改守卫/检查的代码时，必须先跑“守卫自己的测试”，确认它没变成空判**。实例：把 `html.index()` 的先后判据换成正则按 `<script src>` 判序，正则经 heredoc 转义后写歪 → 对任何输入都返回“无问题”——**假绿比红危险得多**（它静默关闭了一道真检查）。抓住它的是守卫自己的单测，所以：**动守卫前先看它有没有测试、跑一遍基线**。
- **守卫误报时，先分清是“数据/注释”的问题还是“判据太粗”**。优先级：改数据（如注释里别写文件名）< 改判据；真要改判据，必须保证它的测试在改后仍能测出真故障（合成样本要用**真实形态**，例如真的 `<script src>` 标签，不能是裸文件名）。
- **`python - <<'PY'` 里写正则要格外小心**：`\b`/`\\` 经 heredoc 后层数不对（写歪不报错，只是不再匹配）。**改文本一律用 `patch` 工具**；必须在 heredoc 里写正则时，写完立刻回读文件并断言它长什么样。

| 路径 | 内容 |
|---|---|
| `D:\AI+教育毕设毕设\毕设规划\` | 规划目录（入口 `00-总索引.md`）|
| `毕设规划\脚手架\` | **可运行的 Agent 运行时骨架**，1680 行 Python |
| `毕设规划\01-选题与导师.md` | 课题名候选 + 导师邮件/微信模板 + 要问导师的 12 问 |
| `毕设规划\02-需求与技术方案.md` | 五层架构 / 模块 / SQLite DDL / 技术选型 —— 论文 2、3 章底稿 |
| `毕设规划\03-评测与实验方案.md` | 60 题评测集 + M1–M6 指标 + 三档对照 + 消融（论文第 5 章）|
| `毕设规划\04-十四周里程碑.md` | W1–W14 逐周目标/交付/验收 |
| `毕设规划\05-第一周开工清单.md` | 逐日 checklist + 可直接跑的命令 |
| `毕设规划\06-论文结构与写作.md` | 章节/字数/图表清单/参考文献策略/查重避坑 |
| `毕设规划\07-风险与应对.md` | 23 条风险 + 答辩问答 |
| `毕设规划\08-研究方向调研与学习规划.md` | **第三个能力**：开放世界调研（来源分层 / 抓取验证 / 主题地图 / 学习路线）+ B0/B1 对照评测 |
| `毕设规划\模板\` | 周报模板、评测集 CSV 模板 |
| `毕设规划\图\` | 论文共用图（命名固定，见 README）|

## 仓库（已建好，别重复 git init）

- 本地：`D:\AI+教育毕设毕设\毕设规划\脚手架`（仓库根就在脚手架目录，**不是**其上层）
- 远程：`git@github.com:shiliyunma/eduagent.git`（SSH，本机已配好，push 无需额外授权）
- 已推送：初始提交 + README 修正；远程与本地 HEAD 一致已验证
- 日常三步：`git add -A && git commit -m "feat: …" && git push`
- `.gitignore` 挡住 `run/` / `.env` / `__pycache__` / `*.db`；`.gitattributes` 统一存 LF
- 验证手法：`git clone` 到临时目录里跑 `python check.py`（确认仓库自包含，没漏提交文件）

## 技术方案（五层，均自研）

入口（CLI / Streamlit）→ 运行时（loop / registry / context / memory / ctx）
→ 能力层（**两条通道**）→ 状态层（SQLite + FTS5 + MEMORY.md + research 工作区）
→ 底座（RAGLearn / arXiv API / 网页抓取 / DeepSeek）

### 两条检索通道（答辩第一问）

- **core（课程问答）**：回答"这个知识点是什么"——封闭世界，自建库
- **research（研究方向调研）**：回答"这个方向我该怎么入门"——开放世界，外网

工具共 **11 个**：core 5 个（`search_course_kb` / `search_exercise` / `get_student_profile` /
`update_weakness` / `plan_learning_path`）+ research 6 个（`research_search` / `fetch_source` /
`grade_sources` / `build_topic_map` / `plan_learning_route` / `save_research_note`）。

用 `--toolsets core,research` 切换；`config.ENABLE_RESEARCH=0` 整批关掉（也是消融开关）。

> ⚠️ **上面这 11 个工具与 `--toolsets core,research` 都是 v1 eduagent 的事实。**
> **v2 ScholarScout 是 8 个工具**（`research.search` / `source.fetch` / `source.grade` / `topic.build_map` /
> `route.plan` / `route.revise` / `note.save` / `research.delegate`），`registry.TOOLSETS = ("research", "kb")`，
> 且截至 2026-10-11 **没有任何模块以 `toolset="kb"` 注册**（8 个全在 `research`）——
> `docs/03` 目录树里那个 `tools/kb_search.py` **并不存在**。`docs/01 §5.7` 还写着"只注册 7 个工具"，也落后了。
> 工作区当前无 `tools/` 以外的 v1 模块，**引用工具数时要先数 `tools/__init__.py` 的 `register()` 调用**。

**参照对象是 Hermes Agent，但不 fork、不复制它的代码**（读架构，自己实现）。
学习工作台在 `D:\找工作找工作\hermes-learn\`（加载 skill `hermes-source-study`）。

## 评测（已落地，`脚手架/eval/`）

**目的**：证明系统有效，每个数字都能回答“分子/分母/谁判的”。文档在 `毕设规划\09-自动化评测流程与指标标准.md`。

| 文件 | 作用 |
|---|---|
| `eval/questions.jsonl` | 43 条输入，7 组（A 单跳 10 / B 多跳 8 / C 多轮 5 / D 不可答 5 / E 改写 9 / F 调研 3 / G 对抗 3），每行带金标准 chunk + 判分规则 |
| `eval/run_eval.py` | 5 条臂：a0 纯LLM / a1 纯RAG / a2 完整 / b0 调研单轮 / b1 完整调研 |
| `eval/metrics.py` | 三层指标 + Wilson 区间 + McNemar 精确检验 + pass^k |
| `eval/report.py` | 自包含 HTML 仪表盘（内联 SVG 4 张图）+ Markdown 表 |
| `eval/judge.py` / `manual_sheet.py` / `rescore.py` | LLM 判官+kappa / 人工判分表 / 重算分数 |

```bash
cd "D:/AI+教育毕设毕设/毕设规划/脚手架"
python eval/run_eval.py --arms a0,a1,a2 --tag v1 && python eval/report.py --tag v1 --open
```

**本机可以免费跑真模型**（Ollama 已装 Qwen2.5-3B）：
`LLM_BASE_URL=http://localhost:11434/v1 LLM_API_KEY=ollama LLM_MODEL="modelscope.cn/Qwen/Qwen2.5-3B-Instruct-GGUF:latest"`
—— 但 3B 的分数只能说明链路通，写论文要换目标模型。

## 评测里踩过的坑（改代码前先看）

- **小模型不会真的调工具，而是把调用“写”出来**：本机 Qwen2.5-3B 在 40 题里有 **16 题一次工具都没调** ——
  它输出 `【出处】搜索课程知识库得到：{...}` 这种文本，而不是 OpenAI 格式的 `tool_calls`。
  后果：a2（完整智能体）**反而输给 a1（纯 RAG）**（均分 0.350 vs 0.650，McNemar p=0.013 显著）。
  这是**模型能力问题，不是架构问题** —— 所以 3B 的分数只能当“链路通了”，写论文必须换目标模型。
  诊断入口：`records[x]['tools_called']`（空 = 没调工具）与 `stop_reason`。
- **引用有两种写法，只认一种就会系统性低估**：模型会写 `根据资料[1]`（序号）而
  不是 `kb004`（id）。`metrics.cited_ids(text, retrieved_ids)` 必须把序号映射回 chunk id，
  否则 E1/E2 会把答对的题计成半分。修改判分规则后跑 `eval/rescore.py` 重算，**不要重跑模型**。
- **“写了【出处】但说不出是哪一条”要算假引用**：只扫 `kb\\d+` / `[n]` 会把“假装引用了”
  这种最危险的行为漏掉（a2 的 15 次引用里只有 2 次可溯），所以 `cited_ids` 里加了 `<未标id>` 哨兵。
- **（⚠️ 仅 v1 eduagent 脚手架，已废弃；v2 见「ScholarScout」节）`registry.get_tools()` 永远包含 core 工具集**（过滤条件写了 `and spec.toolset != "core"`），
  所以做不了“零工具”的臂 —— 纯 LLM 臂要绕过 EduAgent，直接调 `llm.chat(msgs, [])`。
- **拒答指标必须成对报**（E3 拒答召回 + E4 误拒率），否则“一律拒答”能刷分。
- **不可答题的检索指标要返回“不适用”而不是 0 分**，否则不可答题会拉低 recall@k。
- **mock 的结果不能当结论**：报告顶部会自动打警示条；本机没 key 时 `run_eval.py` 会自动用 mock。
- `report.py` 里写死字符串中的 `%%` 不会被转义（只在 % 格式化时才转义）—— 写了两个百分号会原样显示。
- **shell 里写含中文引号/反斜杠的 python -c 很容易崩**：分析脚本写成临时 .py 文件再跑，别用 `-c`。

## 怎么跑（每次开工先跑这三个）

```bash
cd "D:/AI+教育毕设毕设/毕设规划/脚手架"
python check.py                              # 6 项自检（免 key，不联网）
python run_demo.py                           # 课程问答端到端（免 key）
python run_research_demo.py "教育智能体"     # 调研流水线（真实 arXiv + 真实网页抓取）
python run_research_demo.py "Agent 工具调用" --offline   # 无网络时用离线样例
```

真模型：`.env` 里填 `LLM_API_KEY`；接 RAGLearn：`USE_RAGLEARN=1 RAGLEARN_HOME=E:/RAGLearn`。

## 已踩过的坑（别重踩）

- **arXiv API 必须显式带 `Accept` 头**，否则一律 406（curl 默认发 `Accept: */*` 所以能通，urllib 默认不发）。
- **arXiv 会限速**：连续请求后整段时间都 406。必须 ≥3 秒/次 + 指数退避 + 6 小时缓存；
  `data/fixtures_arxiv.jsonl` 是离线兜底（人工整理的真实链接，**不要用 `scripts/make_arxiv_fixture.py`
  的离线回退结果覆盖它** —— 脚本已经加了保护）。
- **路线生成器里别写 `tier <= 2` 当"综述"条件**，会把核心论文全吃进第一段，导致"论文精读"空空。用 `_is_overview()`。
- **工具 schema 取参数要取 `tools[i]["function"]["parameters"]`**，写成 `tools[i]["parameters"]` 会拿不到参数、
  报 `missing required argument`。
- **中文检索要切二元组**，按空格直接匹配一条都命不中。

## 铁律（回答涉及毕设问题时必须遵守）

1. **代码是自己写的**。Hermes 只提供架构参照；绝不说“我开发/改进了 Hermes”。
2. **每个数字要有出处**。论文指标必须来自 `run/eduagent.db` 的 trace 表或评测集结果表，不充许“效果明显”类表述。
3. **不与现有事实相矛盾**：课题名/方向若变，必须同步改 `D:\找工作找工作\2-简历\` 下 B02 简历第 1 条与 `附-Hermes毕设条目-面试口径.md`（三处一致）。
4. **不改用户已有文件前先告知**；学 RAGLearn 的做法：改代码先列计划。

## 2026-10-05 路线决议（覆盖此前"双通道平权"的主线设定）

大浩确认的 5 条，此后一切设计以此为准：

| # | 决议 |
|---|---|
| 1 | core 知识库通道**保留但降级**为可插拔次要通道，只作论文消融对照（用来回答答辩必问的"为什么开放世界方案不需要知识库"）；**主线 = 开放世界调研 + 学习辅助** |
| 2 | AIhot **只作架构参照**（读它的多源抓取→分析→归组→成刊流水线），不部署、不跑。注意：AIhot 是新闻聚合/成刊项目，**没有 query→结果集 的检索能力**，不能当检索引擎 |
| 3 | 资料来源**全覆盖 + 按来源分层打分**：学术源（arXiv/S2/OpenAlex）+ 官方文档 + 通用搜索 + 社区（GitHub/知乎/Reddit） |
| 4 | **复用 Hermes 的技术栈，现有 1680 行脚手架直接废弃**（复用技术选型 ≠ 复用 Hermes 代码，铁律 1 不变） |
| 5 | 原件（PDF/HTML）存本地目录；抽取的纯文本进 SQLite FTS；元数据/简介/出处/会话关联只放表里 |

他明确要求**先不管**的：导师状态、10-10《课题汇总表》与课题名、每周投入时长、交付物清单。
**RAG 库相关的评测（A 组单跳题等）按他要求删除。**

## ScholarScout（v2 主线，2026-10-05 起）

| 项 | 值 |
|---|---|
| 名称 | **ScholarScout**（开放世界学术调研 + 学习辅助智能体，桌面应用，全本地） |
| 本地 | `D:\AI+教育毕设毕设\ScholarScout`（**仓库根就是它**，不再挂在毕设规划目录下） |
| 远程 | `git@github.com:shiliyunma/ScholarScout.git`（SSH 可用，首次推送已成功） |
| 旧方案 | v1 eduagent + 1680 行脚手架=**丢弃**；教训继承见 `docs/03 §7` |
| 技术栈 | Python 3.11 + uv；JSON-RPC 2.0 over stdio；pywebview 桌面壳；原生 HTML/JS **无构建**；SQLite(WAL)+FTS5；OpenAI 兼容模型 |
| 门槛 | `bash scripts/check.sh`（ruff/mypy/pytest/规则同步/依赖审批）；白名单 `deps-allowlist.txt` |
| 设计文档 | `docs/00-总索引.md` → `01-接口契约` / `02-数据模型` / `03-目录结构与规则` / `项目规则.md` |

**关键设计决策（写入论文/答辩要能讲）：** 中文检索用 `tokenize='trigram'` 索引**摘要与会话**，查询短于 3 字自动走 LIKE 兜底（**正文不入库，所以搜不到正文** —— 这在答辩上会被问）；FTS 索引由 **DB 触发器**维护；authority 由 `source_tier_rules` 规则表判定（可复现），relevance 才交给模型；**相关性打分不使用 tier**（不让来源名气污染打分）。

**进度（2026-10-05 第二轮收尾）：远程 `1082955`，503 测试 + `check.sh` 零失败零跳过。**
M1–M8 全部完成，另补齐审计出的六处真缺口（见下「审计式补课」）。
**E3 第四轮全量（单变量：只改提示词）**：**产物完整率 100%（6/6 任务两件产物齐出，上一轮有两题只出路线 = 0/1）**、
出处覆盖率 **100%（153/153）**、ungrounded **0%（0/153）**、行动类 29 条、每题不同来源 **11.7**、**降级率 0%**、
工具成功率 100%、P50/P95 15326/46251ms、token 331687/29079、单任务 146–228s。

**M6 评测（离线部分已完成）**：`docs/05-评测方案.md` 是口径唯一权威；`eval/sets/` 三套题集（E1 判权 24 / E2 守卫 12 / E3 端到端 6）；`eval/metrics.py`（一致率 / Cohen's κ / 最近秩分位数，**分母为 0 返回 None**）；`eval/run_eval.py`（`--set e1|e2|e3|all`、`--ablation`、`--live`）；报告落 `eval/reports/`（不入库）。
**当前数字**：E1 tier 一致率 100%（κ 1.000）、verdict 58.3%（κ 0.444，10 条分歧保留列入报告）；E2 守卫 12/12，关守卫时出处覆盖率虚高 96%→79%；对照臂 A_no_rules 只剩 16.7%。**E3 未跑：本机无 .env / 模型密钥。**

**真机跑 E3 暴露的三个致命缺陷（离线测试全绿也照样发现不了，2026-10-05）**：
① OpenAI 兼容 API（DeepSeek 实测）只接受工具名 `^[a-zA-Z0-9_-]+$`，**带点直接 400** → 注册名（`research.search`）不变，发给模型/模型回传的换成**线上名**（`research__search`），`registry.wire_name/canonical_name` 双向映射，事件与 trace 仍用规范名。
② **robots.txt 拦住了官方 API**：arXiv 的 robots 禁 `/api/query` → 检索恒 0 条 → 新增 `robots_exempt_hosts` 白名单（默认 `export.arxiv.org`）。
③ arXiv 查询被整句加引号（`all:"..."` 精确短语）→ 真检索几乎恒 0 条 → 改成按空格切词 `all:a AND all:b`。
④ 附带：全部来源失败时工具原本回 `ok=true + items=[]`，模型以为“没资料”而反复重试直到烧光预算并输出 DSML 伪工具调用 → 现改为 `ok=false` + 原因。
**一句教训：离线打桩测试只能证明逻辑自洽，证明不了“能跟真实 API 对上话”。任何里程碑收尾至少跑一次真接口。**
**另：arXiv 只索引英文**，中文关键词恒 0 条（事实，不是 bug）——检索用英文，中文留给主题与摘要。

**评测第一次跑就抓到三个真缺陷（写论文/迭代日志的现成素材）**：① 域名规则等值匹配 → `www.nature.com`、`zhuanlan.zhihu.com` 全漏判 tier 4（现改为域名规则同时认子域）；② `.gov` 匹配不到 `gov.cn`（迁移 0003 补 .gov.cn 等）；③ 本地相关性只认整体字面命中 → 中文单主题词几乎全灭（现引入中文二元组部分 credit）。

**M7 桌面壳配色**：整套照搬 Hermes 桌面端令牌体系（`--theme-*` 种子 + `color-mix` 派生、文字透明度阶梯、hairline 描边、圆角系数 0.2），浅色默认 / `.dark` 深色（中性近黑，**不是深蓝**），顶栏有主题切换；截图在 `docs/screenshots/`。
**无头截图工具链（本机可用）**：`msedge.exe --headless=new --virtual-time-budget=6000 --screenshot=out.png <file:// 页面>`；**注意**页面若在启动时调 `applyTheme('auto')`，注入的 `dark` class 会被覆盖 —— 截图要调真实入口函数。

链路已闭环：**模型要工具 → 真的抓 → 摘要入库 → 判权 → 结果带 source_id 喂回模型**。已落地：`fetch/http.py`（限速/退避/缓存/robots）、`providers/arxiv.py`、`summary/`（AIHOT 式七步流水线）、`tools/{research_search,source_fetch,source.grade}`、`grading/{tiers,relevance}.py`（规则表驱动判权）、迁移 0002（`source_grades` + 40 条分层规则种子）。

**判权设计要点（答辩会问）**：authority = tier 基准分 {1:5.0, 2:4.0, 3:2.5, 4:1.0} × 规则权重，卡 0-5；**相关性刻意不用 tier**（不让来源名气带偏打分）；无主题不编分；主题词一个都没中则压到 ≤1.5；阈值集中在 `grading/relevance.py:verdict_for`；判权结果只追加（`source_grades`）。

> **不要跨版本搬运事实（2026-10-11 被咬）**：把本 skill 里的"core 工具集"当成 v2 的事实，写进子 Agent 的任务说明；
> 子 Agent 去 `runtime/registry.py` 核，发现 `TOOLSETS = ("research", "kb")`、**根本没有 core**，拒绝照抄并回报。
> v2 的实际行为：`get_tools()` 在 toolsets 为空时只给 `research`；`kb` 受 `features.enable_kb` 门控（默认关）。
> 教训：**交给子 Agent 的每条事实，都要标注它属于哪一代架构，并让它自己去代码里核。**

**M5 学习产物（已完成）**：`topic.build_map` / `route.plan` / `note.save`，外加 `state/learning.py` 与 `tools/source_context.py`
（生成类工具共用的候选来源三级取法：`source_ids` → 该主题判权为「核心/相关」的来源 → 最近入库 25 条）。
**反幻觉守卫（论文卖点，带 stats 可计量）**：地图节点挂不上出处 → `[无出处]` + `ungrounded_nodes`；
路线条目无对应资料 → `source_id=null` + `ungrounded_items`；模型输出不可解析 → 退化成 `flat`/`skeleton` 兜底而非硬编。

**端到端主线已验收**（`tests/tools/test_end_to_end.py::test_full_chain_from_search_to_route_and_cards`）：
检索 → 取原文出摘要 → 判权 → 主题地图 → 学习路线 → 抽背卡 → 导出，每件产物都挂在真实 source_id 上。

**Loop 硬规则（踩过的坑）**：轮次预算 = 工具预算 + 1，**最后一轮 `tools=None` 逼模型收口**；
否则模型刚好用满 `max_tool_calls_per_turn` 会被误判 `tool_limit`、最终答案直接丢掉。

**M7 桌面壳（已完成）**：`desktop/rpc_client.py`（子进程 + 一行一 JSON，按 id 配对/通知转发/超时）、`bridge.py`（JS 只看得见 info/rpc/reveal/shutdown）、`app.py`（建窗 + 拉起 `--rpc` 子进程）、`ui/`（原生 HTML/CSS/JS，无构建、离线，五个面板）。
**壳里不 import 业务模块** —— 分发只有一份（CLI/桌面/测试同路径），后端崩了窗口还在，中文路径编码坑挡在子进程（强制 PYTHONUTF8/PYTHONIOENCODING）。启动：`python scripts/scout.py --desktop`。
`scout.py --check-ui` 做 UI 资源校验（文件齐全 + **只调真实存在的 RPC 方法** + 无外链），已进门槛。
另：`archive.open_raw` / `archive.reveal` 已实现（路径经 `paths.resolve_rel` 白名单，只允许 data/ 与 run/）；`chat.send` 结果增加 `refs`（本轮各工具报出的出处归并），界面靠它显示"这一次生成了什么"。

**M3 已补齐（2026-10-05 第二轮）**：openalex（免 key，1.6s 出真综述）/ semantic_scholar（**无 key 恒 429 → 按"需要 key"处理**）/ github 三源全接；MinerU 兜底也接上了。

## 评测口径：指标压力会反噬成编引用（2026-10-05 踩出来，论文能直接用）

E3 报 `ungrounded 17.5%`，看着像模型在编。跑 `scripts/diagnose_grounding.py` 把产物原文 dump 出来逐条看：
**全部 11 条"无出处"都在路线里，其中 7 条是 `exercise`** —— "跑通一次训练""构造新学生冷启动评估划分""写一份端到端复现报告"，
这些是**行动/产出**，本来就没有、也不该有出处。度量把"安排了练习"算成了"没有出处"。

两档口径（此后路线/地图都按这个算）：

| 类别 | kind | 声称有出处？ | 进 ungrounded/覆盖率分母？ |
|---|---|---|---|
| 资料类 | paper / doc / video / code | 是 | **进** |
| 行动类 | exercise | 否 | **不进**，单独报 `exercise_items` |

**为什么必须分开**：把练习算作"无出处"不只是误报 —— 指标压力会逼模型给练习硬挂一个来源，
**那才是真幻觉（为了指标好看而编引用）**。修完 e3-04 的同一题：路线无出处 11 → 0，
资料类全部挂上真实 id，练习正确写成 `exercise`。
**通用教训**：任何"反幻觉指标"都要先分清"声称有出处"与"本来就没有出处"，否则指标越硬，编得越像真的。
**配套纪律**：指标不好看时，先写诊断脚本把**产物原文 + 被丢弃的引用**落盘，再动手 —— 别凭猜改。

## 通用检索的真实边界（2026-10-05 实测，答辩会问）

**四个免 key 来源各自的处境**（`auto` 默认 = arxiv + github + 已配实例的 searxng）：

| 来源 | 状态 | 要点 |
|---|---|---|
| arxiv | ✅ 可用 | ≥3s 限速（否则 406）；**只索引英文** |
| github | ✅ 可用 | 官方 Search API，**免 token 就能用**（0.9s 返回真仓库）；未认证 10 次/分钟 → 间隔必须 6s；`robots.txt` 返回 404 即无限制，不需要豁免；判权落 tier 3 |
| searxng | ✅ 可用（自建） | 实测可用引擎 360search + sogou + bing；挂了一键恢复见下 |
| bing / duckduckgo（抓结果页） | ❌ 默认关 | Bing robots 禁 `/search`；匿名请求只返回词典/百科垃圾（**真浏览器也测了，一样**）；DDG 国内不通 |

**为什么 github 路对毕设特别重要**：E3 路线里“无出处”的**资料类**条目几乎全是代码与教程
（`公开 DKT 实现（含数据加载与训练脚本）`、`框架官方入门教程`）—— arXiv 只给论文，这一路正好补上。

**Docker 挂了不会自愈（真实故障）**：通用检索跑在 Docker 里，Docker Desktop 关掉后容器不会自己回来
（`restart: unless-stopped` 只在守护进程活着时生效）→ 应用只看到一条 network 报错。
解法：`scripts/searxng_up.py` 一条命令（启 Docker Desktop → compose up → 等到引擎加载完 → **用真 provider 自检**）；
provider 把 network/timeout 翻译成“该跑哪条命令”。
**注意**：脚本里“自动启动 Docker Desktop”那段容易成为**未实测**的盲区 —— 要验就真的把 Docker 退掉再跑一次。

**限速表会覆盖 provider 自己的值（真 bug）**：`research_search` 是“配置表优先于 provider”，
表里留一个 `“github”: 1.0` 就会把 provider 算好的 6s 覆盖成 1s → 间歇 403。
动态限速（随 token 有无而变）的来源**不要写进全局限速表**。

- **抓搜索引擎结果页不可行**：Bing robots 明确 `Disallow /search`；DuckDuckGo 与公共 SearxNG 在国内超时；
  且 Bing 对匿名请求返回**词典/百科类垃圾结果**（用真浏览器会话测也一样 → 不是“缺个浏览器”的问题）。
- **真浏览器也不万能**：Edge 无头真会话抓百度照样吃“安全验证”（按 IP+指纹判机器人）。
- **免 key 的可行解 = 自建 SearxNG**：`deploy/searxng/` 一条 `docker compose up -d`。三个坑：
  ① Docker Hub 境内不可达 → `docker pull docker.m.daocloud.io/searxng/searxng:latest` 再打回官方 tag；
  ② 默认引擎表境内全 timeout → `keep_only` 收窄到 360search/sogou；
  ③ **`keep_only` 只“保留”不“启用”** → 还得写 `engines: [{name: x, disabled: false}]`，用 `/config` 核对“启用引擎数”。
  容器 restart 后要等十几秒（引擎加载），太早查会得到空结果。
- **商用 API**：tavily / serper / brave / google_cse（后两个有免费额度），填 `.env` 即自动生效。
- **检索结果别吃 HTTP 缓存**：检索是时点查询，缓存会把一次瞬时空响应钉成 6 小时“事实”
  （真机上就让 provider 恒 0 条而 curl 明明有 16 条）→ 检索一律 `cache=False`，只缓存“200 且有内容”的内容抓取。
- **Hermes 的做法可作对照**：它是可插拔检索后端注册表（付费优先、keyless 兜底），`searxng` 是一等公民
  （`SEARXNG_URL` 指向自建实例，走 `/search?format=json`）；AIHot 干脆不做通用搜索，用“信源表 + 6 类适配器”，
  JS 渲染外包给 Jina Reader（按次计费）。两家都不拿浏览器当检索手段。
**已知待办（已闭环）**：前端历史产物与笔记已补 —— `topic.list` / `route.list`（「显示全部历史产物」）+「笔记」页（note/summary）。
真正还没做的只有三件：①「引用是否真的支持该说法」未校验（守卫只校验 id 存在性）；② 论文第 5 章；
③ MinerU 免 token 的 Agent 服务当下返回 `model service is temporarily unavailable`。

**中文路径硬坑（已入 memory）**：项目在 `D:\AI+教育毕设毕设\` 下，**禁用 `uv pip install -e .`** —— site.py 用 GBK 读 editable 写的 UTF-8 `.pth`，解释器启动即崩。装依赖用 `python scripts/install_deps.py`，跑 CLI 用 `python scripts/scout.py`（详见 `docs/03 §9`）。

**中文项目 lint 心得**：ruff 需把 RUF001/002/003（全宽标点）加进 ignore；写未启用规则的 `# noqa` 会触发 RUF100。mypy 在这个项目里已抳到三个真 bug（漏 import、`Row|None` 污染变量、秒级时间戳导致排序不确定）—— 不是摆设。

链路已闭环：**模型要工具 → 真的抓 → 摘要入库 → 结果带 source_id 喂回模型**（`tests/tools/test_end_to_end.py` 守着）。M3 已落地：`fetch/http.py`（限速/退避/缓存/robots）、`providers/arxiv.py`（Accept 头 + 3 秒限速）、`summary/`（AIHOT 式七步流水线）、`tools/research_search.py` + `tools/source_fetch.py`。

**M3 与桌面壳的尾巴都收干净了**（pywebview 桌面壳 + ui/ 仍需在本机开窗口人工确认）。

**中文路径硬坑（已入 memory）**：项目在 `D:\AI+教育毕设毕设\` 下，**禁用 `uv pip install -e .`** —— site.py 用 GBK 读 editable 写的 UTF-8 `.pth`，解释器启动即崩。装依赖用 `python scripts/install_deps.py`，跑 CLI 用 `python scripts/scout.py`（详见 `docs/03 §9`）。

**中文项目 Python 的两个 lint 心得**：ruff 需把 RUF001/002/003（全宽标点）加进 ignore；写了未启用规则的 `# noqa` 会触发 RUF100。mypy 在这个项目里已经抓到两个真 bug（漏 import、变量被 Row|None 污染），不是摆设。

**下一步**：论文第 5 章；E3 已不再只靠 arXiv（接上 SearxNG 后需重跑一遍拿新数字）。

## 审计式补课："完善所有未完善的地方"该查什么（2026-10-05 第二轮，可直接复用这套动作）

用户说"检查程序、完善所有未完善的地方"时，**不要凭记忆列待办**，按这五步盘查（每步都有真收获）：

```bash
# 1) 占位与死代码
grep -rn "TODO\|FIXME\|XXX\|待补\|placeholder" --include=*.py src scripts eval
# 2) 契约承诺了、代码里却没有的状态
grep -rn "not_implemented\|未实现" src
# 3) 文档里记着的待办
grep -n "已知待办" -A 12 docs/00-总索引.md
grep -n "剩下的\|未做\|待写" docs/*.md
# 4) 界面能不能读到库里的东西（最容易漏的一类：数据在库、入口没有）
grep -oE "\"[a-z_]+\.[a-z_]+\"" src/scholarscout/desktop/ui/app.js | sort -u
# 5) 配置里有没有"写了但没人读"的键
grep -rn "enable_mineru\|MINERU_API_TOKEN\|openalex_mailto" src/ | grep -v config.py
```

**这轮真查出来的六类（都是"看起来完成、其实没接上"）：**

| 缺口 | 表现 | 教训 |
|---|---|---|
| 死配置 | `features.enable_mineru` / `MINERU_API_TOKEN` **没有任何代码在用**，契约里承诺的 `mineru_failed` 永不出现 | 配置项要有测试或调用点，否则就是安慰剂 |
| 名存实亡的来源 | `openalex` / `semantic_scholar` 允许写进配置，代码恒回 `not_implemented` | `KNOWN` 集合必须等于已实现集合 |
| 数据在库、界面无入口 | `note.list` 前端只传了 `kind=card` → 学习笔记与摘要**写进库读不到** | 每个 kind/状态都要有查看入口 |
| 只能看本会话 | 产物只从 `session.messages` 的 refs 里取 → 进程一退就点不开 | 列表接口（轻量）+ get 接口（正文）分开 |
| 自救脚本自己的洞 | `searxng_up.py` 首次在新机器跑会卡在 Docker Hub 不可达 | 自救入口也要按"最坏环境"设计 |
| 半成品残留 | `note_save.py` 里 `{...} and {...}` 靠"前者真值返回后者"侥幸正确 | 审计要读可疑表达式，不只看 TODO |

**MinerU 官方接口真机契约（写代码别猜，docs/04 §7 有全表）**：

- 免 token Agent 接口：`POST /api/v1/agent/parse/url` → 轮询 **`GET /api/v1/agent/parse/{task_id}`**
  （试错过的 `…/result?task_id=`、`…/status?task_id=` 都回 `-10002 invalid task_id`）
- `state` ∈ waiting-file/uploading/pending/running/converting/done/failed；done 给 `markdown_url`，failed 给 `err_code/err_msg`
- 需 token 的精准接口（≤200 页）：`full_zip_url` → Zip 里取 `full.md`（stdlib zipfile 就够）
- **401 是抛异常不是返回空** → fallback 要 catch；**退回 Agent 后轮询 URL 也必须换**
  （拿 Agent 的 task_id 查 `/api/v4/...` 永远查不到 —— 单元测试抓到的真 bug）
- ⚠️ 2026-10-05 现状：免 token 的 Agent 服务**不可用**（arXiv / w3.org 多源多次都 `model service is temporarily unavailable`）
  → 代码与契约就绪，但"扫描版能出正文"尚未端到端成功 → 论文里不能当成已验证能力写

**OpenAlex 两个要点**：① `abstract_inverted_index` 是**倒排索引**（词 → 位置数组），必须按位置还原成摘要；
② `robots.txt` 返回 `Allow: /` → **不需要豁免**；③ `year_from` 口径 = 能判定的筛掉、判定不了的留着。
**Semantic Scholar**：无 key 打 `/graph/v1/paper/search` 恒 **429** → 按"需要 key"处理（别塞进 auto 白等）。

**通用引擎一键恢复实测数字（兑现了之前欠的"停机路径未实测"）**：容器全删 → 脚本 **11 秒**恢复；
**连 Docker 守护进程一起停 → 29 秒**恢复（自动起 Docker Desktop → compose up → 等引擎 → 真 provider 查询命中 5 条）。
结论："Docker 关了通用检索就废了"现在是一条命令的事。

**验证手段受限时怎么办（本轮踩到）**：`browser_exec` 在会话里两次超时（云端到不了 `localhost`，本机模式也没起来）、
Node 侧无 jsdom → 界面渲染没能肉眼验证。**可复用的替代法**：用真后端导出真数据再喂给前端假桥 ——
① Python 里 `dispatch()` 把界面要调的 13 个方法各导一份 JSON 到 `run/ui_probe/payloads.json`；
② 复制 ui 到 `run/ui_probe/`，加一个 `mock-bridge.js` 定义 `window.pywebview.api.rpc` 读该 JSON；
③ 副本 index.html 里把 bridge 插在 app.js 之前；④ `python -m http.server 8099` 起靶场。
（本轮到此为止：没有浏览器可驱动，所以只到"RPC 真数据 + `--check-ui` + `node --check`"，**渲染未经肉眼确认**，须如实对大浩说明。）

## 桌面端体验三件套（2026-10-05 第三轮，用户点名的反馈）

### ① 会话标题：根因是前端把标题钉死了

现象：“所有会话都叫新调研”。诊断靠一句 grep：`grep -rn "新调研" src/` → 只有 `app.js` 里
`session.create({title: "新调研"})`。后端 `sessions.py` **本来就有**首条消息自动命名的代码，
但条件是 `title IS NULL OR title = ''` —— 前端一传占位标题，自动命名就永远轮不到。
**教训："功能不生效"先查调用方是不是先把值写死了，再疑后端没实现。**

修法（三件都要，缺一不可）：
- 前端不传 title；后端把“截断首条消息”升级为**总结式**（剥掉 帮我/请/我想/麻烦… 前缀 + 首尾标点 + 压空白）；
- **占位标题也算未命名**（`PLACEHOLDER_TITLES`）—— 否则已经叫"新调研"的老会话永远救不回来；
- 手动改过的名字不能被覆盖（`session.rename` + 列表双击改名，用内联输入不用 `window.prompt`，pywebview 不保证它可用）。

### ② 三栏可拖动 + 硬最小宽度

- 列宽改成 CSS 变量 `--col-left/--col-right`，两条 `.splitter` 拖动改它们；中间栏必须 `minmax(0, 1fr)`
  （不加 `minmax(0,…)`，长内容会把列撑破而不是出滚动条）
- 最小值 180/360/280，JS 里 clamp（**拖动时和窗口 resize 时都要 clapm**），存 localStorage，双击复位
- 拖动用 pointer events + `setPointerCapture`（鼠标按住飞出元素也不丢事件）

### ③ 双击即用的 .exe：关键是“不要 freeze”

用户原话“最好是.exe，且不会因版本更替失效”。
**正确解法：exe 只是个启动器**（找项目根 → 找 .venv 解释器 → 拉起 `scout.py --desktop`），
业务代码全在项目里 → **改代码永远不用重编 exe**。把应用 freeze 进 exe（PyInstaller）每改一次都要重打包，
那正是用户说的“会因版本更替失效”。

落地要点（`packaging/launcher/ScholarScoutLauncher.cs` + `scripts/build_exe.py`）：
- 编译器用 **Windows 自带的 `csc.exe`**（`C:\Windows\Microsoft.NET\Framework64\v4.0.30319\`）
  → 零新增依赖，不用 PyInstaller/MSVC。传 `/target:winexe`（双击不闪黑框）、`/codepage:65001`（源码 UTF-8）
- 项目根解析优先级：`--project` > 同目录 ini > `SCHOLARSCOUT_HOME` > 从 exe 往上找 `scripts/scout.py` > 内置默认；
  显式给了错路径就**明确失败**，不要偷偷换目录
- 出错要**弹窗 + 告诉下一步跑什么**（缺 pywebview → 给出 `install_deps.py` 命令）；
  再给一个 `--diagnose`（打印解析结果，**不弹窗**）+ `SCHOLARSCOUT_LAUNCHER_SILENT=1`（错误走 stderr），
  否则弹窗会阻塞自动化验证

**两个真坑（都自己踩了）**：
1. **GUI 子系统里 `Console.OutputEncoding = UTF8` 不生效** → 中文按 GBK 写进管道，UTF-8 终端全是乱码。
   正确做法：自己 `new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false))`。
2. **`import pywebview` 是错的，模块名是 `webview`**（包名 `pywebview`）。
   我因此误判“pywebview 未安装、桌面壳起不来”，其实一直装着；`--diagnose` 的探针探的是 `import webview`。

### ④ 图标：不引 Pillow 也能做 .ico

`scripts/make_icon.py`：把 PNG 当“只有一页的文档”用已批准的 **pymupdf** 渲染成 16/24/32/48/64/128/256，
再用 stdlib `struct` 拼 ICO 容器（**PNG-in-ICO**，Vista+ 支持，不用写 BMP/AND 掩码）。
注意：宽高各一个字节，**256 要写 0**（写 256 会溢出，Windows 认不出那一档）。
exe 图标用 csc `/win32icon:`；窗口/任务栏图标用 `webview.start(icon=...)`（pywebview 支持，用同一份 .ico）。

### 验证手法（当图形界面没法自动截图时）

- `dist/ScholarScout.exe --diagnose` → 真实校验解析与依赖状态
- `SCHOLARSCOUT_LAUNCHER_SILENT=1 … --project C:/nope` → 验失败路径与退出码，**不会弹窗卡住**
- 启动后用 `Get-CimInstance Win32_Process` 看链路（`scout.py --desktop` + `-m scholarscout --rpc` 双双在）
- 窗口真否起来：`Get-Process | ? MainWindowTitle -ne ''` → 看得到 `ScholarScout —— …`
- exe 里图标真嵌了吗：读字节数 `data.count(b'\x89PNG\r\n\x1a\n')`（本机为 7 档）
- **不要**用 `browser_exec` 验本地页面：云端后端到不了 `localhost`，会卡满 420s 超时

### ⑤ 窗口/任务栏图标：`webview.start(icon=...)` 在 Windows 上**无效**（真踩过）

用户说“图标应该包括左上角以及任务栏”。先看源码再改，避免白干：

- pywebview 的 `start(icon=...)` 文档明写 **"Supported only on GTK/QT"** —— Windows 上直接忽略；
- 它的 winforms 后端（`platforms/winforms.py`）是这么干的：
  `ExtractIconW(GetModuleHandleW(None), sys.executable, 0)` → **从 python.exe 取图标**，
  所以双击启动器打开的窗口，标题栏与任务栏都是 **Python 图标**。

正确修法（`src/scholarscout/desktop/icon.py`，纯 stdlib ctypes）：
1. 等窗口出现（`webview.start(func=...)` 在异步线程里跑，轮询 `FindWindowW(None, title)`，超时就放弃）；
2. 对 HWND 发 `WM_SETICON`：`ICON_BIG`(32) → 任务栏，`ICON_SMALL`(16) → 标题栏左上角；
3. `SetCurrentProcessExplicitAppUserModelID("shiliyunma.ScholarScout")` —— 不设的话任务栏按钮
   按 python.exe 归组，图标又变回 Python（**这一步最容易被漏，漏了 1、2 也白搭**）。

**64 位必坑**：`LoadImageW` 不显式 `restype = c_void_p`，ctypes 把返回值当 `c_int`，
HICON 被截断成 32 位 → `SendMessage` **静默失败**（代码看着跑了，图标没变）。`argtypes` 也要写全。

验证手法（无需真看任务栏）：`SendMessageW(hwnd, WM_GETICON, ICON_BIG/ICON_SMALL, 0)` 读回非 0 即已设上；
再把 `PrintWindow` 截的标题栏与 `assets/icon.ico` 的 16px 档放大 8 倍并排比形状。

### ⑥ 无头环境下自己截图验证 UI（本次踩的四个坑）

- **任务栏是自动隐藏时，屏幕底部截到的是最上层窗口**（本机就截到了资源管理器的状态栏）。
  可靠做法：把鼠标停到屏幕底边 → `FindWindowW("Shell_TrayWnd")` → `GetWindowRect` → 截**那个矩形**，完后还原鼠标位置。
- **`browser_exec` 不要用来验本地页面**：云端后端到不了 `localhost`，会卡满 420s。
- `.ps1` 里写中文注释，PowerShell 5.1 按 GBK 读**无 BOM** 文件 → 直接语法报错。脚本用纯 ASCII。
- `Bitmap.Save` 到**含中文的绝对路径**会抛 GDI+ `ExternalException` → 用相对路径并在项目目录下跑；
  且先 `SetProcessDPIAware()`，否则 `Screen.Bounds` 报逻辑像素（1707×1067 而非 2560×1600）。
- 验证“某个按钮在不在”可以用 **A/B**：开着截一张、关掉再截一张、两张堆叠比差异（比猜图标形状可靠）。

### ⑦ 启动器放哪：问用户期望，不是问惯例

按惯例建产物进 `dist/`，但用户直接反问“exe 不应该放在最外面吗?”—— 他说得对：
启动器是**入口**不是中间产物。默认输出到项目根，同时用 `--out-dir` 保留灵活性；
.exe / .ini / launcher-diagnose.txt 都被 gitignore（构建产物 + 本机绝对路径）。
**教训：约定俗成不等于用户想要的；这类“放哪”的事直接问或按直觉舒服的位置放。**

## 回答正文渲染 Markdown（2026-10-05 第四轮，用户要求"照搬 Hermes"）

Hermes 桌面端用 `streamdown + shiki + katex + dompurify`（React + 打包）。我们这套 UI 是
**无构建原生 JS + 离线（`--check-ui` 禁外链）**，库搬不过来 —— **搬它的设计决策**：

| 搬什么 | 为什么 |
|---|---|
| 围栏代码内不做行内转换 | Hermes 的 `markdown-preprocess.ts` 也是先按围栏切分再分段处理 |
| 裸 URL 自动链接、不吃掉紧邻的 `**` | Hermes 的 `RAW_URL_RE` 特意把 `*` 排除在字符类外 |
| 表格 / `> [!NOTE]` 提示块 / 代码块带语言标签与复制 | `markdown-table.tsx` / `extractAlert` / `RichCodeBlock` |
| 先剥 `<think>`/`<reasoning>` | 跑的是推理模型；Hermes 也有一条 AI 推理块正则 |
| 流式渲染 | streamdown 的核心卖点；我们自己做 120ms 节流（未次结果直接落） |

**不搬的**：dompurify（我们用"模型文本只经 textContent 落地、绝不拼 innerHTML"从构造上免疫）、
shiki（无构建引不进）、katex（我们的输出没有公式）。

### 三个真 bug（都是自写的，被 node 用例抓出来）

1. **有序列表画成圆点**：同层从 `-` 换成 `1.` 是**另一个列表**，原实现并成一个块且根节点恒 `<ul>`
   → 标记类型在同层变化就断开，根节点按首项决定 `ul`/`ol`
2. **嵌套列表没嵌进去**：嵌套判据用了根哨兵的 `-1` 缩进，导致第一项就被当成"更深一层"
   → 改用**首项缩进**当第 0 层
3. **中文句号被吞进链接**（`…/y。`）：裸 URL 的尾部标点排除表只有 ASCII
   → 补 `。，、；：！？）】」』》`（**中文项目做行内解析都要想到这条**）

### 硬约束

- **点链接会把整个界面顶掉**：pywebview 窗口本身就是浏览器，`<a>` 默认当前窗口导航。
  必须 `preventDefault` 后调桥接方法（Python 侧 `webbrowser.open`）；只放行 http/https。
- **`--check-ui` 的正则分不清注释与代码**：在 JS 注释里写一个真实 URL 就会被判"引用了外网资源"。
- 门槛要加三条（否则都是**静默降级**）：markdown.js 在不在、是否在 app.js 之前引入、
  app.js 是否真的用了渲染器。

### 测试手法（UI 代码也能真测）

渲染器写成"纯函数（parseBlocks/parseInline）+ DOM 构建"两层，**纯函数在文件末尾 `module.exports`**，
于是 `node markdown_probe.js <产品文件路径>` 就能真跑产品代码断言解析结果（22 条用例，
没装 node 时 pytest skip）。比在 Python 里复刻一遍解析器可靠得多。
**验证渲染效果**：向库里 seed 一条富 markdown 的助手消息 → 启应用 → `PrintWindow` 抓窗口
（不抢焦点）→ 自己看。比截图任务栏可靠得多（那个受自动隐藏/遮挡干扰）。

## 上下文成本：钱到底花在哪（2026-10-05 实测 + 第一轮优化）

用户反馈“缓存好像没效果，API 账单很高”。**先分清缓存边界再谈优化**：

| 层 | 缓存 | 说明 |
|---|---|---|
| 网页抓取 | ✅ 6 小时 | `fetch/http.py` 落 `data/cache`，**只覆盖内容抓取** |
| 检索请求 | ❌ 故意不缓存 | 时点查询；缓存会把瞬时空响应钉 6 小时 |
| **模型调用** | ❌ 零缓存 | **费用主体就在这里** —— 出网缓存对它一点用没有 |

**实测解剖（工具直接量，不靠估）**：系统提示词 393 tokens + 工具声明 1562 tokens ≈ 1956 tokens **每轮固定重发**；
一轮对话模型调用 ~10 次，**每次都把前面所有工具结果重发一遍** → 同一段 JSON 被计费 5～9 次；
单条工具结果最长 9583 字符 ≈ 3194 tokens；多次检索常捞回同一批来源 → **摘要被反复计费**（最大一笔浪费）。

**三条策略（`runtime/context_budget.py`，纯函数）**：去重（已发过的 `source_id` 只回锚点，并砍掉重复的 `refs.id` 列表）、
截断（snippet 200 字符 / 单条 4000 字符）、折叠（只留最近 3 条工具结果全文）。
**铁律：`source_id` 一个都不能丢** —— 它是引用锚点，丢了模型就编 id，守卫拦下重试反而更贵。

**实测效果**（同题 e3-01、同模型、同提示，对照报告 20261005-163320.json）：
输入 **39,808 → 33,808（−15.0%）**，工具调用 9 次与 stop_reason=completed 均未变。

**把“贵不贵”变成可查的数字**：`chat.done` 的 `usage` 已经在返回里，界面直接显示
`token 输入 12.3k ／ 输出 0.4k` —— 用户抱怨账单时，先给得出数字，再谈优化。

**下次还能省的**（按收益/成本排）：① 未传任何推理强度参数，若模型支持 `reasoning_effort` 可大幅压 completion；
② 历史窗口 40 条 user/assistant 全量重发，可做摘要压缩；③ 工具声明 1562 tokens 可精简描述；
④ LLM 响应缓存（对评测重跑价值大，对交互几乎无感）；⑤ `run_eval` 缺 `--only/--limit`，单题调参要等 15 分钟全量。

## 省 token：量过的杠杆与它们的真实适用面（2026-10-05/06）

先量再改。四个杠杆的实测结论（探针脚本 `run/cost_probe2.py`）：

| 杠杆 | 实测 | 怎么办 |
|---|---|---|
| 推理强度 `reasoning_effort` | **这个模型不认**：low/minimal 与不带参数 completion 162/154/160，无差别 | 别浪费时间在这个参数上 |
| `thinking={"type":"disabled"}` | **真开关**：completion 162 → 36（−78%），`reasoning_content` 归零，答案长度不变 | 做成配置项，默认开（质量优先） |
| 工具声明精简 | 4,675 字符/轮里**绝大部分是 JSON-Schema 必需结构**（每参数 type/required/enum），描述文字只 ~500 字符 | **不值得改**（省<3%，还会伤工具选择）；先量再说 |
| LLM 响应缓存 | 交互每轮 messages 都新 → 请求体哈希必不同，**命中率≈0** | 默认关；只对评测复跑有价值 |
| 历史重发 | 真实会话只有 12 条 ≈ 1,000 tokens，根本碰不到上限 | 加**保头保尾**字符预算是为了防长会话线性膨胀，不是日常手段 |

两条经验：
- **不要假设“历史才是大头”**：查库实测后，重发成本的大头是每轮的工具结果与固定项（系统提示 + 工具声明）；
  “上下文瘦身”的收益要用同题同模型的 A/B 量出来（实测输入 −15%）。
- **善意的优化也要量**：工具声明看着一大块，拆开一看全是 schema 结构；改它反而有风险。

## 对照实验的纪律（提示词分档时最容易错的地方）

把“自动生成产物”改成“按需生成”并把旧行为当对照臂时：

- **对照臂必须与旧版提示词逐字相同**。我第一次重写时只差了一个**空行**（三引号后的换行），
  哈希就从 `af6f1b33e12c` 变成了另一个值 —— “单变量对照”当场不成立，旧报告的数字不再可比。
  做法：把旧版从 git 取出来比字符串（`git show HEAD:path > old.py` 再动态 import 比），
  并用**黄金哈希**写进测试钉住；两档应该只在流程那一段不同（把该段摘掉后两边相等）。
- **换档就是换实验条件**：`system_prompt_hash` 会写进会话，报告里要带上 `arm.system_prompt_hash`，
  否则事后分不清哪组数字来自哪版提示词。
- **口径要能区分“真做到”与“看上去做到”**：能确定性查库的（首轮是否真出产物、点单后是否真出产物）
  优先；措辞匹配（“要不要我生成…”）只能当辅助指标，必须在报告里标注是启发式、不是满意度。
- **部分跑要留痕**：`--only/--limit` 跑出来的报告要写 `results["selection"]`，
  否则单题结果会被当成全套结论引用；题号写错时必须返回空并提示，不能静默跑全套。
- **改默认行为后要回头修旧测试**：旧用例断言“每轮必出两件产物 / 10 次工具调用”
  那是在测**对照臂**的行为，应显式传 `auto_artifacts=True`，别直接删掉它们。

## 界面加不上按钮时，先查“接口能不能被调用”（以取消为例）

后端有 `chat.cancel` 但界面两年没入口——根因不是忘加按钮，是它要 `turn_id` 而**整轮结束前前端拿不到**
（`turn_id` 随 `chat.done` 才回来）。修法：后端维护 `session_id -> turn_id` 注册表（`ctx.running`，
run_turn 里登记、finally 里摘），RPC 改成可按 `session_id` 取消。
铁律：真要取消时**没有在跑的轮次必须回 `{ok:false, reason}`**，不能静默回 ok；
取消是**步间检查**，界面必须写“当前这一步结束后生效”，别让人以为点了就停。

## 用量/成本读数：宁可报 null 也不折算

会话级/全局成本都用 `usage_records` 一张表取数。**单价未配置时 `est_cost` 回 null**——
报 0 会让人以为免费。每轮显示真实 token，累计值随会话切换刷新。

## “点不动”优先怀疑传输层（真机 bug 的排查顺序）

一次真实故障：生成过程中点资料库条目毫无反应。根因不在界面，而在**两层传输串行**：
① 服务端 `serve()` 是“读一行→分发→写回”的单线程循环，一轮 `chat.send` 阻塞 3–12 分钟，
后面的请求根本读不到；② 桌面桥 `call()` 把锁握到整个等待结束，且 `_await` 会丢弃别的 `id` 的响应。
同一个根因还让**「停止」从来没生效过**（它排在队尾）——上一轮只做了离线测试（直调 dispatch），
没走 stdio，所以没发现。

**铁律**：凡是“长任务 + 界面还能操作”的功能（预览/取消/设置），必须**走真实传输层**验证一次，
只调 `dispatch` 不算验证。JSON-RPC 有 `id`，并发与乱序本来就是安全的：
服务端 daemon 工作线程 + 带锁写回；客户端 per-id 信箱（锁只管“分配 id + 写请求”）；
DB 侧 `check_same_thread=False` + WAL（一写多读）。

## 在“轮次前/入库前”写字段，先防顺序陷阱

标题提前生成踩到的坑：用户消息一落库，本地截断就先占位起名（看起来像真标题），
**模型总结的标题再也写不进去** → 只能“轮次前先总结标题、再跑轮次”，并且“写回只在未命名时”与
“判定未命名”两处条件必须一致（否则出现“判定说没名字、写回又不给写”）。

## 新引入的额外模型调用：测试配置里默认关掉

标题功能给 `chat.send` 加了前置调用，5 个既有测试的桩回复被吃掉。修法**不是**给每个测试补一条桩，
而是**在测试配置里默认关掉该开关**、由专门测试文件显式打开（产品默认值保持开）；
否则以后每个 round 测试都要知道这个隐性多一次调用。

## 界面显示与实际行为要有“口径不变量”测试

新增来源状态表时加了参数化测试：`usable == (name in auto_names())`。
这类不变量防的是“界面说可用、实际检索不带它”——用户最难排查的一类错觉。
同理：“每个密钥字段都必须能在面板里填”（防“后端能改、界面没入口”）。

## 长任务能停 + 会话用量可见

- 取消必须**可按 `session_id`**（整轮结束前前端拿不到 `turn_id`），且“没有在跑的轮次”要回
`{ok:false, reason}` 而不是假装成功；界面要如实写“当前这一步结束后生效（步间检查）”。
- 用量读 `usage_records`；**单价未配置就别折算金额**（报 0 会被当成免费）。
- 已知缺口：用量在**整轮结束时**才落库 → 被停止/中断的轮次不记 token，会话累计会少算。

## 论文第 2 章（文献综述）：文献靠检索拿，不能手写

- **用本系统自己的链路检索文献**（`scripts/collect_thesis_refs*.py` 走 arXiv provider）——
  既拿到可核元数据，又顺便证明检索层真能用。
- **参考文献表由脚本生成**（`scripts/make_thesis_bib.py`）：从接口返回直接生成条目
  （作者名缩成“姓 首字母 + et al”），**绝不手写作者名/编号**；**先定编号再写正文**
  （编号顺序 = 引用出现顺序），否则会出现“引了 [12] 但 [12] 是别的文章”。
- **OpenAlex 在本机当期硬 429**（单发 + 冷却 65s 仍 429）→ 文献全用 arXiv，
  并在论文里**如实写明“这不构成穷尽性综述”**；别为了好看去编期刊名。
- 每篇只写标题/摘要支撑得起的话；对照表要落到“别人怎么做 → 本文怎么做 → 差别”。

## 论文成稿与 docx 成品链路

- **内容以 `docs/*.md` 为准，docx 是产物**：`scripts/build_thesis_docx.py` 一次生成
  （封面 + 中英文摘要 + 目录域 + 六章 + 参考文献 + 致谢；A4、宋体小四 1.5 倍行距首行缩进 0.74cm、
  黑体标题、三线表、页脚页码）。改内容不要改脚本。
- 验收必须**转 PDF 逐页看**（LibreOffice 无头 + pymupdf 渲染 PNG 再自己看），
  并程序化检查：无空白页、章起始页、元笔记未漏、参考文献条数。
- 踩过的坑（元笔记漏进 docx、插分页符产生空白页、标题被染蓝色、`uv run` 弄坏 venv）
  已写进 skill `docx-report-generation` 的 pitfall 13–18，出成品前先读。

## 论文第 3/4 章的写法（已验证过的结构）

- **第 3 章（设计）**：需求→约束（每条需求写它带出的约束）→四层架构（强调**单向依赖**）→
  数据模型取舍→检索/判权分工→反幻觉两级证据→成本解剖表→界面约定→工程规范→**决策记录表
  （决策/理由/代价）**。每条设计都必须写出**被放弃的替代方案**，否则像拍脑袋。
- **第 4 章（实现）**：先给**真实规模**（文件数/行数/测试数/提交数，现场 `wc`+`git rev-list` 取数），
  然后按层写“**撞墙才写对**”的点（来源方限速与字段怪癖、GUI 框架“看起来生效其实没生效”、
  中文路径下解释器与 GDI+），每个坑都写：现象→原因→处理。
- **第 3/4 章严禁与第 5 章重数字**：第 5 章只讲结果与判读，口径统一在 `docs/05`。

## 引用语义支持度校验（补“覆盖率≠语义支持度”那个洞）

现有守卫只校“引用 id 真不真”，所以“出处覆盖率 100%”只能读作**可归属率**。补法（`grading/support.py` + `eval/verify_support.py`）：

- **证据分两层，必须分开报**：拓过原文的来源有摘要+要点（强），没拓过的只有标题/年份/域名（弱）。
  混在一起算“支持率”会被**标题字面重合**污染 —— 实测 121 条来源里只有 11 条有摘要。
- **解析不出来就是 unknown，不计入分子分母**：宁可样本变少，也不把解析失败当通过。
- **从长到短匹配关键词**：`"supported"` 是 `"unsupported"` 的子串 —— 按声明顺序匹配会把
  “不支持”读成“支持”，直接让指标虚高（写用例钉住）。
- 结果落库（`support_checks`，UNIQUE(artifact_id, item_id) 做 upsert）：可复算、可累积，
  重复审同一批产物不重复计费；弱支持清单直接交人工复核。
- 产出数据结构：`topic_maps.graph_json.nodes[].{nid,label,source_ids, dropped_source_ids}`；
  `learning_routes.plan_json.weeks[].idx`（**周号是 `idx` 不是 `week`**）+ `items[].{kind,title,why,source_id}`；
  只审“资料类”条目（exercise 不声称出处），与 ungrounded 口径一致。

## 桌面壳（pywebview）踩过的坑

- **窗口几何：单位是两套**。`window.width/height/x/y` 报**物理像素**，而
  `create_window(width=…)` / `webview.screens` 用**逻辑像素**。不换算就出这种事：存下 1700x1050，
  下次开出 2539x1547（150% 缩放屏）。取缩放用 `ctypes.windll.user32.GetDpiForSystem()/96`。
- **别用“窗口尺寸≈屏幕尺寸”猜最大化**：screens 与窗口尺寸单位不一致时会误判，
  而误判的代价是“干脆不记尺寸”（用户要的功能直接失效）—— 比“最大化的窗口下次以同尺寸打开”严重得多。
- **窗口尺寸恢复要在关闭前存**：`window.events.closing += save`（销毁前属性还能读），
  再加 `webview.start()` 返回后的兜底（不同后端事件可用性不一致）。
- **窗口最小尺寸也是逻辑像素**：min_size=(960,640) 在 150% 屏上实测锁在 1429x953 物理像素。
- 验证“界面跟随窗口”：用 `SetWindowPos` 拉两个差距大的尺寸各截一张（注意会被 min_size 卡住，
  所以“小”那一张实际是最小尺寸），再靠 vision 对比。**PowerShell 脚本写成纯 ASCII**：
  PS 5.1 按 GBK 解 UTF-8 中文注释，会跑出续行符把脚本搞坏。
- **URL 上带 `#fragment` 会让窗口全白**：`create_window(url="...index.html#settings")` 实测页面加载失败。
  要“启动后直接打开某个面板”，用 `window.evaluate_js("openSettings()")`（子线程 sleep 2s 再调即可，
  evaluate_js 自己会排到 UI 线程）；前端函数声明在全局就能被调到。
- **窗口/任务栏图标**：`webview.start(icon=...)` 在 Windows 被忽略，要自己发 `WM_SETICON`（见 desktop/icon.py）。
- 核对界面不要靠抢焦点截图：用 `PrintWindow`（后台渲染）+ 相对输出路径（GDI+ 在中文路径下 `Bitmap.Save` 会抛
  `ExternalException`）。给应用留一个 `SCHOLARSCOUT_DEBUG_JS` 环境变量口子，就能在启动后跑一段 JS
  （点开菜单/弹层）再截图，比手点可靠。

## 设置写入的坑（settings_io）

- **`.env` 里的 `LLM_MODEL` / `LLM_BASE_URL` 会盖掉 `config.yaml`**：用户在设置栏改模型会静默不生效。
  改了这两个字段就顺手把 `.env` 里的同名覆盖摘掉，并在 `notes` 里告诉用户。
- 写配置必须**原子**（先 `.tmp` 再 `os.replace`）且**在原始 YAML 上深合并**，否则会抹掉用户自己加的键。
- 白名单之外的键**直接报错**：静默忽略拼错的键 = 用户以为改了、其实没改。
- 布尔字段默认值要显式给：界面用 `值 !== false` 判勾选时，值缺失会让“默认关”的开关看起来是开的。
- 枚举磁盘占用别用单一类型注解（`dict[str, dict[str,int]]` 里塞 path 字符串会被 mypy 拦）；
  同名的循环变量在多个 for 里复用也会让 mypy 把 `Row` 当成 `dict`。

## 导出与分享

- 导出 Markdown 时**跳过 `role=tool` 的消息**：一条就是几千字符的原始 JSON，会把导出件撑到不可读；
  工具调用用“工具轨迹”一节做摘要即可。本机绝对路径要打码。

## Hermes 技术栈实测（2026-10-05，量本机 git 安装副本）

Python `os.walk` 累加，已排 venv/node_modules/.git/dist。**量级数字是用来压期望的，别想着复刻：**

- 非测试代码 ≈ **149 万行**（Python 75.8 万 = 总 1,854,671 − tests 1,097,022；TS/TSX 73.4 万）
- 前端/桌面：`apps/` 57.4 万行 TS（= Electron 桌面端）、`web/` 5.5 万、`ui-tui/` 9.9 万
- 后端：`hermes_cli/` 20.9 万、`agent/` 11.3 万、`tools/` 10.4 万、`plugins/` 8.8 万、`gateway/` 8.5 万、`tui_gateway/` 2.7 万
- 栈：Python 后端 + Electron(TS/React/nanostores) + JSON-RPC(`tui_gateway/`) + SQLite/FTS5(`~/.hermes/state.db`) + 技能即 Markdown + 设置进 config.yaml/密钥只进 .env

**毕设状态层该照抄的两条（比他自研切词强）：**

1. `state.db` 是**双 FTS5 索引**：`messages_fts` + **`messages_fts_trigram`**。trigram tokenizer 就是"中文检索要切二元组"的工程解法 —— 直接 `tokenize='trigram'` 支持中文子串检索，不用在应用层切词。
2. **索引由 DB 触发器维护**（`messages_fts_insert/update/delete` 等），不在应用层手动同步，少一类 bug。

会话存储共 60 个对象（24 索引 / 26 表 / 10 触发器），核心三表 `sessions` / `messages` / `session_model_usage`。

## 开发协作铁律（2026-10-05 大浩定，本项目所有 AI 协作适用）

1. **先定接口、数据模型、目录结构，再让 AI 填实现** —— 禁止"帮我写个系统"式开局。
2. **一次只改一个文件或一个函数**，禁止顺手重构整个项目。
3. **规则文件写清楚**：技术栈 / 命名 / 错误处理，放进项目 CLAUDE.md（Hermes 侧等价物是 AGENTS.md，两个都读）。
4. **lint、类型检查、测试、构建，一个不过就不合。**
5. **关键路径人工逐行 review**：支付、权限、安全、数据迁移。
6. **新增依赖要审批** —— AI 最爱随手 import 不存在的包。
7. **小 PR、勤提交、能回滚**，出问题直接撤。
8. **定期重构** —— AI 写得快，技术债也快。

## 当前状态与下一步（2026-09-27，历史记录）

已完成：规划 9 份（含 `08` 调研模块）+ 脚手架跑通（1680+ 行 Python，check 6/6 绿；
`run_demo.py` 与 `run_research_demo.py` 均可复现；调研流水线真实抓取过 Anthropic 官方页）。
未完成：导师未联系、课题名未上报、`eval/` 脚本未写、Web 界面未做、调研模块的两个消融开关未加。
**下一步 = 跑 `05-第一周开工清单.md` 的 D1–D7。**

## 未同步项（需要时提醒大浩）

简历 `张思浩-简历-v10-B02-软件开发.docx` 第 1 条与 `附-Hermes毕设条目-面试口径.md`
目前**没提**第三个能力（研究方向调研）。若要同步，建议在该条正文末尾加：
「并实现面向开放世界的研究方向调研与学习路线生成（来源分层 / 抓取验证 / 路线规划）」，
并向面试官解释"两条检索通道"的区别。**改简历前必须先征得他同意。**

## 收尾批次的坑（2026-10-08：拆分、前端接线、多格式抓取）

### 门禁与提交
- **提交前必须看到 `check.sh` 的"失败 0 项"**。本轮我在红灯门禁上提交过一次（lint 段 `RUF059`）：
  只看各子项 `[OK]` 会漏掉它。线索很隐蔽 —— `ruff check` 打 "N hidden fix available" 时退出码就是 **1**，
  而我用 `| tail -1` 把真正的报错行吃掉了。自查命令：
  `bash scripts/check.sh 2>&1 | grep -E "失败|\[FAIL\]"`；要定位失败段落用 `awk '/\[FAIL\]/{flag=1} flag'`。

### 空 dict 的 mypy 陷阱 / README 规模的写法
- **空 `{}` 会被循环体把类型推窄**：`artifact = {}` 后接 `artifact[k] = int(...)`，mypy 会把变量推成
  `dict[str, int]`，下一行再写 `artifact[k] = None` 就报 `assignment`。修法是**显式标注**
  `dict[str, int | None]`（并在注释里说明 None 与 0 的语义不同），**不是 `# type: ignore`**。
- **README 写"N 个 X × M 个 Y"会被读成"N 组、每组 M"**：验收时必须**逐行 json 载入数真值**
  （`len(obj['turns'])`），本次 e4 实际是 6 个对话**合计 15 轮**（3/3/3/2/2/2），
  而 README 的"6 个对话 × 15 轮"会被读成 90 轮。数字口径有歧义就改成能逐条对账的写法。

### 前端接线
- **白名单映射器是字段黑洞**：`sources.js normalize()` 只复制已知键 → 后端新加的 `session_id`/`session_title`
  到不了前端 → 34 条资料全挤进"（未记录会话）"一组，**分组功能等于没做，而测试全绿**。
  加/改后端字段时必须同步改映射（或改成透传）。
- **渲染核对（无头截图 + 自己看图）几乎每次都抓到真 bug**（本轮 3 个界面问题里 1 个是它抓的）——
  界面改完必须真机看图，不能只跑门禁。
- **探针桩的锚点要容忍 `?v=`**：`index.html` 里实际是 `<script src="markdown.js?v=2">`，
  写死 `<script src="markdown.js"></script>` 会让桩根本插不进去 → 页面空白、白跑一轮。
  做法：按前缀 `<script src="markdown.js` 找那一行，插完 `assert "window.pywebview" in html`。
- **探针自己要留可读标记**：在页面上挂 `data-probe="groups=10 rows=34"`、
  `data-detail="md=1865 pre=0"`，再用 Edge `--dump-dom` 读回来 —— 比只看截图硬。

### 拆分（B5/B6 同一套，顺序不能反）
- **先在单体状态把契约测试改成读"全部 js"的统一读取口**（`tests/desktop/_ui.py`）**并跑绿，再拆**；
  否则拆完失败项反复漂移（第一次就是这么回滚的：4 个测试各自读 `app.js` 文本）。
- **拆的是职责不是行数**：一张 CSS 不拆（拆开只会改变层叠顺序，纯风险）；`run_turn` 这种单一流程也不拆。
- **用 AST 取顶层定义的行范围机械搬运**（`lineno`/`end_lineno`），不手抄；
  新模块**先带原文件的完整 import 头，再让 ruff 自动删用不到的**（本轮清掉 8 处），
  比人工判断该 import 什么稳得多。
- **原模块 docstring 会被一起搬走** → 新文件里 `from __future__` 不在最前 → SyntaxError；
  修法：只保留新写的说明，再接 `__future__` 行。
- **保留再导出门面**：`rpc/methods.py`、`state/learning.py` 都留成门面，外部 import 一字不改，改动不扩散。

### 用临时脚本改文件（本项目多为 CRLF）
- **每个替换必须断言命中**；未命中照样打印"已改"会让人以为改好了（本轮踩过）。
- 多行字面量在 CRLF 文件里对不上 → 用**单行锚点**，或直接用 `patch` 工具（它自己处理 CRLF）。
- `read_text()` 的结果已带 `\r`，再用 `"\r\n".join(...)` 拼会写出 `\r\r\n`；
  自查 `raw.count(b"\r\r\n")` 必须为 0。
- heredoc 用**带引号的**定界符（`<<'PY'`）才不会被 shell 吃掉一层转义。

### 前端与门禁（2026-10-08 又踩）
- **门禁绿 ≠ 前端语法对**：`check.sh` 里的 UI 检查只验"资源齐全 + 只调真实存在的 RPC"，
  **不解析 JS**。改完 js 必须单独 `node --check <file>; echo $?` 并看**退出码 0**。
  本轮因此两次在语法错的 `learning.js` 上提交并推送（`6bf4c49` → 修于 `12184b3`）。
- **不要在循环里用 `cmd >/dev/null 2>&1 && echo OK || echo FAIL` 下结论**：本机 MSYS bash 下
  该写法会对**全部**文件（含没动过的）报 FAIL —— 又一个"命令骗我"的实例。
  逐个跑并显式打印 `$?`。
- **写 JS 的换行转义别数层数**：heredoc（shell 一层）+ Python 字面量（一层）极易算错，
  结果是先写成字面量反斜杠 n、改一次又变成"字符串里夹真换行"（SyntaxError）。
  用 `String.fromCharCode(10)` —— 零转义。拼接反斜杠一律用 `chr(92)`（旧教训的同一条）。

### 证据与数据纪律（论文里能当"设计取舍"写）
- **判断 JS 语法只认一种信号：`node --check <file>` 自己打不打印错误**。
  在本机 MSYS bash 里，把 `node --check` 接在 `export PATH=...` / `cd` / heredoc 后面再取 `$?`，
  以及写成 `for f in *.js; do cmd && echo OK || echo FAIL; done`，**都会得到假的非 0/FAIL**
  （本轮因此误判过四次，其中两次还在"坏文件"结论下提交了）。要复核就用单独一条
  `node --check 文件` 并看看有没有输出；要批量就用 `node --check f1 && node --check f2` 直接串。
- **JS 里"收集所有匹配"一律要 `g` 标志**：`String.replace(re, fn)` 不带 `g` 只替第一处、回调只跑一次 ——
  抽背选项按钮只出「A」就是这个（先怀疑转义、查了字节码才发现正则本身是对的）。
  这类 bug **门禁与单测都看不见，只有渲染核对能抓** —— 所以界面改完必须看图。
- **替换后必须回读校验**："脚本打印成功"不等于"文件真的改了" —— 本轮有个脚本先切了块、
  后面又重新 `read_text` 读盘再写回，把裁剪覆盖掉了。每个写操作后回读并断言关键串还在。

- **派生值不进证据**：复习日志只存 `rating + reviewed_at`，间隔由 fold 重算
  → 才有"重算 == 汇总行"这条可对账的测试。
- **老数据没有留痕就如实暴露**：`replay_card` 对迁移前的卡返回 `logged=0` + 空值，不编一个看着像的数。
- **投影不下发谜底**：抽背的 `note.due` 只给 `front + card_id`，答案走 `note.reveal` 单独取；
  测试断言的是"payload 里搜不到答案原文"，而不是"界面没显示"。
- **抽不到就说抽不到**：Office 抽取器区分"文档确实没字"（ok=True / text 为空）与"打不开"（ok=False + 原因），
  调用方据此记 `empty` 或 `extract_failed`。

### 多格式抓取（.py/.md/.docx/.xlsx/.pptx/.pdf）
- **扩展名判定先看 URL 后缀、再看 media type**：很多站点把 `.md`/`.py` 用 `text/plain` 甚至
  `application/octet-stream` 发出来，只看 media type 会一律认成 txt。
- **Office 三件套用标准库读**：`zipfile` + `ElementTree` 抽 `word/document.xml` /
  `xl/sharedStrings.xml` + `xl/worksheets/sheet*.xml` / `ppt/slides/slide*.xml` ——
  不引 python-docx/openpyxl/python-pptx，**运行期依赖白名单守住 4 项**。
- **音视频明确标 `skipped_media`**，别让它和"提取失败"混成一个桶（用户明确要求排除）。
- 文本类（md/py/json/csv/…）**原样解码**，不要再过 HTML 提取器（洗了会没缩进与代码结构）。
