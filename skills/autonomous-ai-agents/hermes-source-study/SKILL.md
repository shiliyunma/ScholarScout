---
name: hermes-source-study
description: "Use when 带大浩读 Hermes 源码、抽背或继续 hermes-learn 路线。含锚点、铁律、格式。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [hermes, source-reading, teaching, 抽背, 面试, agent-architecture]
---

# Hermes 源码带读（大浩的学习工作台）

大浩在 `D:\找工作找工作\hermes-learn\` 学 Hermes Agent 的组成原理，目标 AI 应用开发 / AI Agent 岗位（2027 届秋招）。

## 工作台（先读这些再答话）

| 文件 | 作用 |
|---|---|
| `00-总索引.md` | 五层架构 + 三层阅读法 + 材料可靠性分级 |
| `01-学习路线.md` | 6 阶段 + 2 专题，每讲：读物 / 源码锚点 / 动手 / 自测 |
| `02-源码地图.md` | 目录职责 + 关键文件实测行数 |
| `03-核心机制速查.md` | 8 大机制 + 面试讲法 + 博客数字校准表 |
| `04-自测题库.md` | 每阶段 1 原题 + 2 延伸题（含答案与锚点） |
| `学习进度.md` | 打卡表 + 实证笔记；每天结束时帮他记一行 |

源码本体：`D:\找工作找工作\hermes-learn\hermes-agent-main`（**v0.21.3**，6842 个 .py / 199 万行 / 204 MB）。

## 铁律

1. **数字与结论必须现查本副本。** 社区博客的"父 90 子 50""conversation_loop 4900 行"在 v0.21.3 里都不成立（实测 `delegation.max_iterations=250`、`max_spawn_depth=1`、`conversation_loop.py=1745 行`）。文档口径（70+ tools / 28 toolsets）也与本副本实测（63 次 `registry.register`、36 个 toolset）有差。
2. **不整文件通读。** 只读被调用的函数。5299 行的 `context_compressor.py` 只需要入口 + 它调用的三个函数。
3. **笔记必须带 `文件:行号` 锚点**，否则不算数。
4. **大浩说"不会"就停。** 不许他自己猜，也不许替他猜；猜对也算错。
5. 带读很费上下文，**一次只带读一个函数**。

## 常查锚点（本副本实测，可直接引用）

- 循环：`agent/conversation_loop.py:1594 run_conversation`；门面 `run_agent.py`（1599 行）；turn 阶段 `agent/turn_*.py`（31 个）
- 工具：`model_tools.py:867 handle_function_call`；`tools/registry.py:654 register` / `:95 discover_builtin_tools` / `:222 _CHECK_FN_TTL_SECONDS=30`
- 并发：`agent/tool_executor.py:1419`（线程池并发）/ `:1762`（交互工具串行）
- 提示词：`agent/system_prompt.py:659 build_system_prompt_parts` / `:722 build_system_prompt`；缓存断点在 `agent/prompt_caching.py:3-5`（系统前缀 + 最后 3 条 = 4 个断点）
- 压缩：`agent/context_compressor.py:1911 class ContextCompressor` / `:4979 compress`；抽象 `agent/context_engine.py`；默认值 `compression.threshold=0.50`、`protect_last_n=20`（`hermes_cli/config_defaults.py:556/569`）
- 预算/委派：`agent/iteration_budget.py:25`；`delegation.max_iterations=250`、`max_spawn_depth=1`、`max_concurrent_children=10`（`config_defaults.py:1322/1341/1336`）
- 入口：`hermes_cli/main.py`（3597 行）→ `cli.py`（4835 行）→ `run_agent.py` → 循环
- 网关：`gateway/run.py`（5676 行）；平台适配器 `gateway/platforms/`（内置）+ `plugins/platforms/`（22 个）
- 持久化：`hermes_state.py` + `hermes_state_*.py` 家族（schema/messages/sessions/search/fts/wal）

## 抽背格式（他的硬性偏好）

**1 道原题 → 他答 → 判分 → 再给 2 道延伸题 → 逐题判分 → 讲透。** 不要一次丢一堆。
每题 3 分：结论对 / 机制说得清 / 源码锚点对。

可直接用的模板：
```
抽背我，阶段 N，按 1 原题 + 2 延伸题的格式来
带读源码：从 <文件>:<行号> 开始讲，每 50 行停顿先问我这段在干嘛
验证笔记：我说"X"，去源码查证，对就确认，错就纠正
```

## 结束后

把当天"grep 出来的实证"追加一行到 `学习进度.md` 的"实证笔记"表（日期 / 阶段 / 源码锚点 / 搞懂了什么）。
