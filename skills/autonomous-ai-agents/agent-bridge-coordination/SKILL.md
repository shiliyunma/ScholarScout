---
name: agent-bridge-coordination
description: "Coordinate with another AI agent (OpenClaw/Claude Code/Codex) via a shared filesystem bridge — Hermes acts as architect/PM, the other agent as implementer."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [multi-agent, filesystem-bridge, coordination, architect-pm, cross-agent]
    related_skills: [kanban-orchestrator, claude-code, codex, opencode]
---

# Agent Bridge Coordination (Filesystem)

Coordinate with another AI agent (typically OpenClaw, but the pattern works with any agent that reads/writes files) through a shared directory. Hermes acts as **architect/PM** — designing interfaces, breaking down tasks, reviewing code — while the other agent acts as **implementer** — writing code, fixing bugs, handling details.

## When to Use This

- A user has two agents (Hermes + another) and wants them to collaborate
- The other agent cannot be called via CLI (no `terminal()` integration)
- The user wants a structured architect/implementer split
- You need to delegate multi-step coding work with review gates

## Do NOT Use When

- The agent has a CLI — use `delegate_task` or terminal spawning (see `claude-code`, `codex`, `opencode` skills)
- The task is simple enough for a single `delegate_task` call
- You can use the kanban board for multi-agent coordination (see `kanban-orchestrator` skill)

## Bridge Directory Protocol

### Directory Layout

```
<shared-path>/bridge/
├── status.json          # JSON status file — the "handshake"
├── from-Hermes.md       # Hermes writes tasks/replies here
└── from-OtherAgent.md   # The other agent writes responses here
```

### status.json Format

```json
{
  "version": 1,
  "last_from_hermes": "description of last task sent",
  "last_from_egg": "description of last response received",
  "task_id": "game-task1",
  "status": "waiting"
}
```

Valid status values:
- `"idle"` — no work in progress, bridge is dormant
- `"waiting"` — Hermes has sent a task, waiting for the other agent to complete
- `"ready"` — the other agent has completed the task, Hermes should read the response
- `"error"` — something went wrong

### Message Convention

**from-Hermes.md** (Hermes writes):
```
[HERMES]

## Architecture Overview
...

## Task N/M：Task Title

[TASK] Description of what to do
[CODE_TEMPLATE]
```python
# Framework code with blanks for the other agent to fill
```
```

**from-OtherAgent.md** (the other agent writes):
```
Description of what was done, any issues encountered, questions for Hermes.
```

## Protocol Rules

1. Hermes sends ONE task at a time — never batch multiple tasks
2. All replies from Hermes start with `[HERMES]`
3. Tasks use `[TASK]` + `[CODE_TEMPLATE]` format
4. Each task has a max of 2 review-retry cycles before Hermes escalates
5. Hermes must review the code before sending the next task
6. The other agent updates status.json to "ready" when done
7. Hermes updates status.json to "waiting" after sending a task

## Monitoring

Set up a cron job to automatically detect when the other agent responds:

```bash
hermes cron create --schedule "*/2 * * * *" --name "monitor-bridge" --prompt "
Read <bridge-path>/status.json
If status == 'ready':
  1. Read from-OtherAgent.md
  2. Output notification with task_id and response summary
  3. Set status back to 'idle' to avoid duplicate alerts
"
```

## Architecture/PM Workflow

When acting as architect/PM for a coding project:

1. **Read requirements** — read the goal/project specification file
2. **Design architecture** — determine module split, class interfaces, method signatures
3. **Break into atomic tasks** — 3-8 tasks, each focused on one deliverable:
   - Task 1: Config/constants module
   - Task 2: Entity/data classes
   - Task 3: Main loop / application entry
   - Task 4: Integration and testing
4. **Send Task 1** — with full [CODE_TEMPLATE] (function signatures, class stubs, blanks)
5. **Review response** — check interface compliance, naming consistency, edge cases
6. **Send next task or request fixes** — max 2 retry cycles per task
7. **Final integration** — write the entrypoint, test end-to-end
8. **Report to user** — what was built, what works, what needs attention

### CODE_TEMPLATE Best Practices

- Provide complete **function signatures** (params + return types)
- Provide **class stubs** with all methods listed
- Leave only **implementation bodies** empty for the other agent to fill
- Include **blank annotations** like `# TODO: implement` or `# 请补充：`
- Specify **naming conventions** explicitly in the template

## 另一个写者直接改同一个仓库（提交纪律）

经 bridge 交件时不会撞车；但对方**直接写同一个 git 仓库**时，提交自己的产物前要先看几步。
**提交纪律的完整清单以 skill `ai-collab-project-gates` 为准**（只 add 自己独占的路径、别用 hunk 过滤、
生成物必须在 HEAD 上重生成、双端比对推送、别人弄红的门禁只报告不修）—— 这里不重复，只补 bridge 场景特有的两条：

1. **先读 `git status --short` 的第一列**：第一列有字母 = 已被暂存；若 index 与工作区完全一致
   （全是 `M `/`A `、第二列空）= **对方正在准备提交** → 此时**不要 `git reset`**（会清掉人家备好的暂存），
   要么按现状提交，要么只按自己的路径暂存。
2. **看对方的写入节奏再动手**：对方文件在最近几分钟内还在变时，先做不依赖它的部分；
   要改同一文件就用最小改动，别整文件重写 —— 否则对方下一次保存会覆盖你的改动。

## Pitfalls

1. **Never send multiple tasks at once** — the other agent will do one and the rest pile up causing confusion
2. **Always review before proceeding** — a bad implementation compounds on the next task
3. **File paths must be absolute** — the other agent may not share your working directory
4. **Use the same bridge for both projects** or create separate subdirectories — don't mix contexts in one bridge directory
5. **Cron monitoring can alert on stale "ready"** — always reset status to "idle" or "waiting" after processing
6. **The other agent may have different capabilities** — don't assume it can do everything Hermes can (browser, web search, etc.)
7. **Don't change status.json format mid-stream** — break the other agent's parser
