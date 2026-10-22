---
name: matt-engineering-skills
description: "Use when 做工程类工作：写改代码、评审、TDD、拆票、排障、调研。"
---

# mattpocock 工程技能目录（渐进式披露入口）

真源：`E:/Github/skills-main/skills-main/skills/`（git 仓库，`git pull` 即全局更新）。
本文件只是**目录**，不含任何方法。方法全在单篇 SKILL.md 里。要写文档、排错、做评审、拆票、调研、写 PR 文案、或要教我东西时，先在这里定位。

## 铁律

1. **先定位，后加载。** 按表找到 1 篇，`read_file <绝对路径>` 读全文，再照着做。
2. **一次只加载一篇。** 多篇叠加既爆上下文，又会让两套流程互相打架。
3. **加载后按它的流程走，但本机规则优先**：用户的 AGENTS.md 与既定偏好覆盖技能里的默认值。
4. 表里没有的名字不要猜路径去 read_file（会 404）。

路径模板：`E:/Github/skills-main/skills-main/skills/<分类>/<技能名>/SKILL.md`

## 定位表 — engineering（主战场，20 篇）

| 技能 | 一句话 | 何时用 |
|---|---|---|
| `ask-matt` | 仓库自带的路由器：问「我现在该用哪个技能」 | 你只描述处境、还没想好用哪篇时 |
| `code-review` | 双轴评审：Standards（合规）+ Spec（对得上需求），并行两路子代理 | 「审一下这个分支/PR」「review since X」 |
| `codebase-design` | 深模块（deep module）词汇：接口、seam、可测性 | 设计或改造模块接口时 |
| `domain-modeling` | 维护领域模型：术语表 GLOSSARY.md、ADR | 定命名/术语、写决策记录 |
| `diagnosing-bugs` | 疑难 bug 与性能回退的诊断循环（先证伪再改） | 「诊断/查这个 bug」「慢了/挂了/报错」 |
| `tdd` | 红-绿-重构，测试先行，含集成测试 | 要测试先行的新功能或修 bug |
| `to-spec` | 把当前对话合成一份 spec（不访谈） | 聊清了，要落成文档 |
| `to-tickets` | 把 plan/spec 拆成 tracer-bullet 小票，标阻塞边 | 准备开工、要分步交付 |
| `implement` | 按 spec 或票实现 | 有 spec/票，动手写 |
| `implement-spec` | implement 的变体：直接吃 to-spec/to-tickets 产物 | 习惯 spec→票→码流水线时 |
| `pr` | 写 PR 描述正文 | 提 PR 前要文案 |
| `prototype` | 一次性原型，回答一个设计问题 | 「这个状态模型/交互先试试」 |
| `research` | 对高可信一手来源调研，产出仓库内 markdown | 要查文档/API 事实、要人跑腿 |
| `improve-codebase-architecture` | 扫全库找「可加深模块」的机会，出 HTML 报告再逐个 grill | 存量项目架构体检 |
| `wayfinder` | 超过一个会话的大工程：摊成决策票地图，逐个解 | 「这事一周也做不完」 |
| `triage` | issue/外部 PR 的状态机流转 + 分诊简报 | 攒了一堆 issue 要清 |
| `grill-with-docs` | 逼问式打磨方案，同时顺手产出 ADR 与术语表 | 方案要定且要留档 |
| `wizard` | 生成交互式 bash 向导，带人走只有人能做的步骤 | 配 CI secret/第三方后台/迁移割接 |
| `retro` | 对一次编码会话做复盘 | 一段工作收尾 |
| `setup-matt-pocock-skills` | 仓库一次性初始化：issue tracker、triage 标签、领域文档布局 | 首次在这仓库用其它技能前；**Hermes 下多半不需要，要跑先问我** |

## 定位表 — productivity（7 篇）

| 技能 | 一句话 | 何时用 |
|---|---|---|
| `grill-me` | 逼问式访谈，磨掉方案里的含糊 | 想被追问着把计划想透 |
| `grilling` | 同上，更狠：直接压测你的判断 | 要压力测试一个决策 |
| `teach` | 在这个工作区里教我一个新概念/技能 | 要讲解 + 上手练习 |
| `handoff` | 把当前会话压成交接文档给下一个 agent | 换会话/换 agent 接手 |
| `to-questionnaire` | 把答不了的决策转成给别人填的问卷 | 需要外部信息才能定 |
| `wait-what` | 「打住，刚才那段没落地，重讲」 | 我讲糊涂了 |
| `writing-for-agents` | 写给 agent 看的文档：skill、AGENTS.md、CLAUDE.md | 改规则文件/写技能时 |

## 定位表 — in-progress / misc（未维护，按需仍可读）

| 技能 | 一句话 | 何时用 |
|---|---|---|
| `chief-of-staff` | 单会话内协调子代理推进长期目标 | 长任务要分工 |
| `claude-handoff` | 把会话交给一个立刻开工的后台 agent | 要与后台进程交接 |
| `loop-me` | 就你想做的工作流反复逼问需求 | 工作流还没想清 |
| `writing-fragments` / `writing-beats` / `writing-shape` | 写作三段：采料 → 排节拍 → 逐段成形 | 要写文章/长文 |
| `setup-ts-deep-modules` | 给 TS 仓库接 dependency-cruiser 强制深模块 | TS 多包项目 |
| `setup-pre-commit` | Husky + lint-staged + 类型检查 + 测试 | 仓库还没有提交门禁 |
| `git-guardrails-claude-code` | Claude Code hooks 拦危险 git 命令 | 仅 Claude Code 适用，Hermes 下跳过 |
| `migrate-to-shoehorn` | 测试里的 `as` 断言迁到 shoehorn | TS 测试数据构造 |
| `scaffold-exercises` | 生成练习目录（题/解/讲解） | 做课程材料 |

## 常用链路（按链路一次只取一篇，按顺序推进）

- **新需求 → 合并**：`to-spec` → `to-tickets` → `implement` → `code-review` → `pr`
- **需求还很糊**：`grill-me`（或 `grilling`）→ `to-spec`
- **有 bug**：`diagnosing-bugs` → `tdd`（先补红测试）
- **接口/模块要改**：`codebase-design` + `domain-modeling`
- **存量架构体检**：`improve-codebase-architecture`
- **超过一个会话的大活**：`wayfinder`
- **事实调研**：`research`
- **会话收尾 / 交接**：`retro` / `handoff`

## 适配层：Claude Code → Hermes（读技能正文前先记住）

- `CLAUDE.md` → 本机读 `AGENTS.md`（同一角色）。
- 「parallel sub-agents / Task tool」→ 用 `delegate_task`；单篇要求并行评审时照做。
- 正文里的 `/to-spec`、`/setup-matt-pocock-skills` 之类斜杠 → **不必存在同名命令**，我直接按该技能的流程执行。
- frontmatter 的 `disable-model-invocation` → Hermes 不识别，忽略（本路由已天然做到「手动触发」）。
- issue tracker（`triage`/`to-tickets`/`wayfinder`）：没配 tracker 就退化成本仓库 `docs/tickets/*.md`，**不要为了跑流程去接 GitHub/Linear**。
- 这些技能默认面对的是 git 仓库；在不带 git 的目录里用，把「提交/分支」步骤换成「写文件」。

## 想升级成真斜杠命令时（默认不做）

只有当你确实会手打 `/tdd`、`/code-review` 才值得：把这几篇的目录用 `mklink /J` 联到一个 curated 目录，再
`hermes config set skills.external_dirs '["<curated 目录>"]'`。代价：每条索引会常驻每轮提示词（约 150 字符/条）。
