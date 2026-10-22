# Baseline Execution Guide

> **When to use**: User is running the competition starter kit's baseline agent and encounters slowness, hangs, or run_id conflicts.

## Why the ReAct baseline is slow

The official ReAct baseline is **inherently serial** — not slow because of your environment. Expect **30 seconds to 2+ minutes** per single task.

### 4 root causes

| # | Cause | Why it matters |
|---|-------|---------------|
| 1 | **Serial loop** — each step requires a full LLM API call before the next can start (`max_steps` defaults to 16) | 16 steps × several seconds each = you wait linearly, can't parallelize |
| 2 | **Growing context** — every step appends the assistant response + observation to the messages array. Step 1 has ~3 messages; step 16 has ~35 | Later steps process ever-larger prompts, getting slower each time |
| 3 | **API latency** — DeepSeek API has ~2-5s first-token latency from China; OpenAI is faster (~0.5-1s) | Each step wastes 2-5s just waiting for the first token |
| 4 | **JSON action parsing** — agent outputs `{"action":...,"action_input":...}` in JSON. When code contains quotes/triple-quotes, JSON parsing fails, triggering a retry step | Each retry adds another wasted LLM call |

### Rough time estimate per task difficulty

| Difficulty | Typical steps | Estimated time (DeepSeek) | Estimated time (OpenAI) |
|-----------|--------------|--------------------------|------------------------|
| Easy | 4-8 | 20-60s | 10-30s |
| Medium | 6-12 | 30-90s | 15-45s |
| Hard | 10-16 | 60-120s | 30-60s |
| Extreme | 12-16+ | 90-180s | 45-90s |

### How BrightLiao's L2 (context preloading) helps

Preloading data files (`head`, `dtypes`, `knowledge.md`) at agent startup eliminates 4-6 steps of `read_csv` / `list_context` tool calls, cutting runtime by 30-50%.

## `run_id` collision

### Symptom
```
Error: Invalid value for run.run_id: [WinError 183]
当文件已存在时，无法创建该文件。: '...\\artifacts\\runs\\first_run'
```

### Root cause
The config has a fixed `run_id:` (e.g. `run_id: first_run`). The runner calls `mkdir(exist_ok=False)` and fails if that directory already exists from a previous run.

### Fix — two options

**Option A** (recommended): Remove `run_id` from config — auto-generates a UTC timestamp:
```yaml
run:
  output_dir: artifacts/runs
  # run_id removed — auto-generated timestamp
```

**Option B**: Delete old run directory before re-running:
```bash
rm -rf artifacts/runs/first_run
```

### Why it happens
The first run fails (bad API key, timeout, etc.) but creates the output directory. On retry with the same `run_id`, the existing directory blocks creation.

## Silent hangs (zero output, process runs indefinitely)

### Symptom
```
process poll → status=running, output_preview=""
# After 5+ minutes → still no output
```

### Root cause #1 — API key invalid
When the API key is invalid (401), the OpenAI SDK's `client.chat.completions.create()` raises an `APIError` that gets caught and re-raised as `RuntimeError` in `model.py`. But with some configurations, the HTTP call can hang waiting for the auth response rather than erroring quickly.

### Root cause #2 — Wrong model name
DeepSeek's current API only accepts `deepseek-v4-flash` and `deepseek-v4-pro`. The old alias `deepseek-chat` no longer exists.

When the model name doesn't match any available model, the API call **hangs silently** — no 401 error, no timeout for a very long time (the default 600s process timeout expires first). The trace.json shows `steps: []` (zero steps completed) and `failure_reason: "Task timed out after 600 seconds."`

### Diagnostic — use the same curl for both causes
```bash
# This curl reveals BOTH auth issues AND available model names
curl -s https://api.deepseek.com/v1/models \
  -H "Authorization: Bearer YOUR_KEY" --connect-timeout 10 --max-time 15
```
- **200 OK + model list**: auth works. Check if your `model:` name is in the list.
- **401**: bad API key.

### Prevention
1. Always verify API key AND model name with curl before running the agent
2. Set a modest `task_timeout_seconds` (default 600 is too long for debugging — use 60s)
3. Run single-task first before full benchmark

## Quick diagnostics checklist

| Symptom | First thing to check |
|---------|---------------------|
| Zero output for minutes | API key validity OR wrong model name (curl test) |
| Zero output + steps=[] in trace after timeout | Model name doesn't exist on provider |
| `run_id` error | Remove fixed run_id from YAML |
| Task takes >> 2 minutes | Check API provider latency |
| `ScannerError` on config load | YAML indentation (sibling keys must match) |
| `Authentication Fails` | Replace with valid key from provider dashboard |
