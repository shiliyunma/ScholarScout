---
name: file-bridge-agent-orchestrator
description: "Orchestrate work with another AI agent (e.g. OpenClaw) via a shared filesystem bridge. Hermes acts as architect/PM, decomposing tasks, writing code templates, reviewing results — one task at a time through status.json + markdown files."
version: 1.0.0
author: Hermes Agent
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [multi-agent, orchestration, bridge, collaboration, code-generation]
    related_skills: [kanban-orchestrator, kanban-worker]
---

# File-Bridge Agent Orchestrator

Orchestrate work with another AI agent (OpenClaw, etc.) through a shared filesystem bridge. Hermes acts as the **architect/PM**: decompose tasks, write code templates, review results. The other agent implements. Communication is asynchronous via bridge files.

## When to use this

- You need two AI agents collaborating on a project
- One agent handles architecture/design/review, the other implements
- The agents are on the same machine and can share a filesystem
- You want sequential tasking (one task at a time) with code review gates

## Bridge Directory Structure

```
<shared-bridge-dir>/
├── from-Hermes.md    # Hermes writes tasks/replies here
├── from-蛋.md         # The other agent writes results here (name varies)
└── status.json        # State machine: idle → waiting → ready → idle
```

### status.json state machine

```json
{
  "version": 1,
  "last_from_egg": "summary of last completion",
  "last_from_hermes": "summary of last task sent",
  "task_id": "project-taskN",
  "status": "waiting"    // one of: idle, waiting, ready, processing
}
```

States:
- `idle` — no task pending
- `waiting` — Hermes sent a task, waiting for peer to finish
- `ready` — peer finished, ready for Hermes to review
- `processing` — peer is actively working

## Workflow

### Step 1 — Architecture design
Before any tasking, design the full architecture:
- Module breakdown
- Interface definitions (function signatures, class methods)
- Data flow between modules
- Write this to `from-Hermes.md` with `[HERMES]` prefix

### Step 2 — Task decomposition
Break the project into 3-8 atomic tasks. Each task should:
1. Be self-contained (one file or one logical unit)
2. Include a `[TASK]` header and `[CODE_TEMPLATE]` section
3. Have the code template with function/class stubs and `# TODO: 实现` markers
4. Specify exactly which file to write to

### Step 3 — Sequential tasking
Send ONE task at a time:
1. Write task + code template to `from-Hermes.md`
2. Set `status.json` → `status: "waiting"`
3. Wait for peer to set `status: "ready"` in `status.json`
4. Read `from-蛋.md` for completion message
5. **Review the code** — check:
   - All required functions/classes exist
   - Correct imports and use of shared constants
   - Boundary conditions handled
   - Comments in required language
6. If issues found → max 2 retries, point to specific lines
7. If OK → write next task
8. Repeat until all tasks done

### Step 4 — Integration + testing
After all modules are complete:
1. Create any integration/entry-point files (e.g., `run_game.py`)
2. Run the full project to verify it works
3. Fix any issues found during testing

## Monitoring Setup

Set up a background watcher for faster response than manual polling:

### Option A: 10s background watcher (recommended)
Create a Python script at `~/AppData/Local/hermes/scripts/bridge_watcher.py`:
```python
import json, time, os
BRIDGE = r"<bridge-path>"
STATUS_FILE = os.path.join(BRIDGE, "status.json")

def check():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("status") == "ready":
            data["status"] = "idle"
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[ALERT] Peer replied! task_id={data.get('task_id')}")
            return True
    except: pass
    return False

if __name__ == "__main__":
    print("[WATCHER] Running...")
    while True:
        if check(): break
        time.sleep(10)
```

Run as background process:
```python
terminal(command="python -u \"<path>/bridge_watcher.py\"", background=True, notify_on_complete=True)
```

### Option B: Cron job (fallback, min 1 minute)
```python
cronjob(action="create", name="monitor bridge", schedule="* * * * *", prompt="Read status.json. If status='ready', read from-蛋.md, notify user, reset status to idle.")
```

### Option C: Manual polling
```python
read_file("<bridge>/status.json")  # check status manually
```

## Communication Protocol

All messages use this format:

```
[HERMES]
[HERMES_TASK TaskN/M]
[CODE_TEMPLATE]
```python
# template code with TODOs
```
```

## Pitfalls

- **Status mismatch**: The peer agent might use `"processing"` instead of `"ready"`. Specify clearly in tasks to use `"ready"`.
- **Path confusion**: Windows needs `C:/path` or `/c/path` format. Python doesn't understand `/d/path` — use `D:/path` or `os.path.join`.
- **Watcher one-shot exit**: The background watcher exits after one detection. Restart it after each new task.
- **Overlapping file writes**: Ensure write_file completes before the peer reads it. Write status.json last (atomic commit).
- **Code review scope**: Don't just check syntax — verify interface compatibility between modules. The architect defines interfaces; enforce them.
- **Task granularity**: Too small = excessive round trips. Too large = peer loses context. Aim for 1 file per task.

## Delegating to built-in subagents (`delegate_task`)

Same class as the file bridge — you orchestrate, another agent implements — but the transport is in-process.
These rules come from repeatedly hitting the failure modes:

- **One work item per child (two at most, and only when they are adjacent).** A child that is handed 2–4 items
  runs into its iteration cap with the features written but **nothing committed**, leaving an unrunnable worktree.
  Put the escape clause in the brief: *“if you run low on iterations, finish and commit the FIRST item, then
  report honestly on the rest.”*
- **Every brief carries the pass criterion.** Not “run the tests” but the exact command, the exact success line
  to look for, and the rule that the commit is chained to it (commit only when that line appears); commit
  messages go through a file (`git commit -F`), never a quoted `-m` that the shell will split.
  **Read the counts in that line, not just the verdict:** a gate that reports “0 failed, N skipped” while the
  linters and type-checker were skipped for lack of a PATH entry is a **false green**. The passing form is the
  one with **zero skips**, and it usually needs the project's venv on `PATH` first — put that setup command in
  the brief too, or the child will report success off a skipped run.
- **Demand real output, not conclusions.** “It works” is not evidence — require the actual test count, the
  command echo, the field names parsed from a real response. Also require a “did not do / could not verify”
  section with `unknown` markers, and forbid inventing internals. Children comply well when told explicitly.
- **Verify the workspace before trusting the handoff.** A child that was interrupted may have left a
  **half-finished, non-running** change set. First step on receipt: `git status` + run the gate — then decide
  between finishing it and `git checkout -- <file>` to roll back (keep a patch of the uncommitted state first).
- **Inspect any guard/test file the child modified.** Confirm the diff **extends** the rule (a new layer added
  with its forbidden dependencies) rather than **loosens** it to buy a green run.
- **Re-run the key tests yourself** after receiving work; self-reports are claims. This is what actually catches
  the real problems.
- **Tell the child to edit text with the `patch` tool**, never a script/heredoc — those eat one escaping layer
  (CRLF files, `\n`, anchor mismatch) and can turn a file that only needed one type fix into a syntax error.
- **The child’s own findings are often worth more than the task.** When it reports a bug it stumbled on
  (a date-dependent test, a regex that swallows underscores, duplicated text-box copies), record it as a
  deliverable — not just as “did not finish”.
- **Stopping a child takes effect at its next iteration boundary**, so it may still touch files after you ask it
  to stop. Re-capture the state (patch + `git status`) *after* stopping, and reconcile the two.

## Parking work between sessions

When the user says “stop, pick it up tomorrow”, saving the *working state* is part of the orchestration, not an
afterthought:

1. **Patch the uncommitted changes to a file** (`git diff > <dir>/<date>-uncommitted.patch`). Uncommitted work is
   often a half-finished change set — it must not be lost, and it must not be committed just to save it.
2. **Write a handoff note next to it**: what shipped today, which files are dirty and what is known broken, the
   **first command to run tomorrow**, the ordered todo list, the measured facts (endpoints, field names, response
   shapes), and the prerequisites only the user can satisfy.
3. **Stop the children first, then re-capture** — a stop lands at an iteration boundary, so files may change
   after you asked. Reconcile the patch against `git status` before declaring the state saved.
4. Keep this directory out of version control (add it to `.gitignore`): it is a handoff, not a deliverable.

## Related

- [kanban-orchestrator] — SQLite-kanban based agent orchestration (different transport, same decomposition pattern)
- [delegate_task tool] — built-in Hermes subagent spawn (same-process, no file bridge needed)
