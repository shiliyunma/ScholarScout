# 业界一手出处（原文摘录，避免每次重新检索）

抓取日期：2026-10-11。摘录逐字来自原文页面。

## 1. Chromium《Documentation Best Practices》

URL: https://chromium.googlesource.com/chromium/src/+/main/docs/documentation_best_practices.md

> "A small set of fresh and accurate docs is better than a large assembly of "documentation"
in various states of disrepair."
→ **一小批新鲜准确的文档，胜过一大堆各种失修状态的"文档"**。反对"每个目录都写文档"。

> "Docs work best when they are alive but frequently trimmed, like a bonsai tree."

> **Update docs with code**: "Change your documentation in the same CL as the code change."
→ 文档与代码**同一个提交**改，否则必然腐烂。这一条决定了要不要把文档检查放进门禁。

> **Delete dead documentation**: "Dead docs are bad. They misinform, they slow down ...
Stragglers can always be recovered." / "Take it slow ... First delete what you're certain is wrong."
→ 处理存量烂文档的次序：先删确定错的，拿不准的先不动。

> **Implementation state determines document repository**: "If the doc is about implemented code,
put it in README.md. If it's pre-implementation discussion, including Design docs, PRDs, and
presentations, keep it in shared Drive folders."
→ **关于已实现代码的文档，家是 `README.md`**；设计文档属于"实现前讨论"，实现完成后应转为
**归档**，不该继续被当作持续维护的数字源。

> **README 该回答的四件事**: "A good README.md orients the new user to the directory and points
to more detailed explanation and user guides: What is this directory intended to hold?
Which files should the developer look at first? Are some files an API? Who maintains this
directory and where can I learn more?"
→ 直接当模板用，不要自己发明结构。

> **Duplication is evil**: "Do not write your own guide to a common technology or process.
Link to it instead."
→ 同一个术语/流程不要写第二份，链接过去。这条否掉"每个目录一份术语表"。

> 文档层次（自上而下）：inline comments（**只写 why**）→ method/class API comments（**what/how 的契约**，
"any behavior documented here should have a test verifying it"）→ README.md（导航）→
Design docs/PRDs（实现前提案，之后转归档）。
→ 推论："这个文件是干什么的"属于 **module docstring**，不是另开一份 md。

## 2. Diátaxis

URL: https://diataxis.fr/ （Ubuntu 官方文档体系采用；官网列有 Gatsby / Cloudflare / Vonage 的证言）

> "Diátaxis identifies four distinct needs, and four corresponding forms of documentation —
tutorials, how-to guides, technical reference and explanation. It places them in a systematic
relationship, and proposes that documentation should itself be organised around the structures
of those needs."

> "Diátaxis solves problems related to documentation content (what to write), style (how to write it)
and architecture (how to organise it)."
→ 核心用法：**按读者需求分类，不按"内容主题"分类**；四类混写会四不像。
reference（清单、参数）与 explanation（为什么）不该塞进同一个文件。

## 3. C4 模型（Simon Brown）

URL: https://c4model.com/diagrams/code/

> "Ideally this diagram would be automatically generated using tooling (e.g. an IDE or UML
modelling tool), and you should consider showing only those attributes and methods that allow
you to tell the story that you want to tell."
→ 代码级的图/清单**应该自动生成**，且**只展示讲得通故事的那部分**（不是全量倾倒）。

## 4. ADR（架构决策记录）

URL: https://adr.github.io/ ；源头：Michael Nygard, "Documenting architecture decisions" (2011)，
Red Hat 博客有转述：https://www.redhat.com/en/blog/architecture-decision-records

> "An Architectural Decision Record (ADR) captures a single AD and its rationale."
→ 记录"当时为什么这么决定"的文档**不随代码变化而失效**，是唯一完全不会腐烂的手写文档类型。

## 5. 陈旧文档的代价（二手但一致，用于说服）

- https://dev.to/tacoda/when-the-docs-lie-27m4 — "Stale documentation is not a small problem.
It is worse, in specific and measurable ways, than no documentation at all." 同文建议
"A README per significant directory"。
- https://understandingdata.com/posts/doc-drift-detection-ci/ — 工程做法：把文档漂移检测放进 CI。
