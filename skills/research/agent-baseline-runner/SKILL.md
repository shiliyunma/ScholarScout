---
name: agent-baseline-runner
category: research
description: Run agent-based competition starter kits (KDD Cup, Kaggle agent comps) on Windows — API config verification, model name resolution, direct Python invocation workaround for multiprocessing bugs, and baseline speed analysis.
tags: [competition, baseline, kdd-cup, agent, windows, api-setup]
trigger: User wants to run an agent-based competition baseline that uses an LLM agent + tools + API on Windows.
version: "1.0"
---

# Agent Baseline Runner

Run agent-based competition starter kits (ReAct/CodeAct agents) on Windows.

## Setup Verification Checklist

Before running any task, verify these in order.

### 1. Config YAML — Indentation matters

All keys under `agent:` must have the **same** indentation:

```yaml
agent:
  model: deepseek-v4-flash        # 2 spaces
  api_base: https://api.deepseek.com   # 2 spaces
  api_key: sk-xxx                 # 2 spaces — NOT 6 spaces!
```

**Pitfall**: YAML silently fails on inconsistent indentation. `api_key` with more spaces than `api_base` causes `ScannerError: mapping values are not allowed here`.

### 2. Model name — Verify against the API

The model name in `agent.model` must match what the provider actually serves. **Never assume** a name like `deepseek-chat` works without checking.

```bash
# List available models
curl -s https://api.deepseek.com/v1/models -H "Authorization: Bearer *** | python -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

**Known mappings** (DeepSeek, 2026):

| Config value | Reality |
|---|---|
| `deepseek-chat` ❌ | Does NOT exist — request hangs |
| `deepseek-v4-flash` ✅ | Fast inference, ~1-3s per call |
| `deepseek-v4-pro` ✅ | Slower, higher quality |

### 3. Test API connectivity

Always test a real chat completion before trusting the agent:

```bash
curl -s -X POST https://api.deepseek.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer *** \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"say hello"}],"temperature":0.0}' \
  --connect-timeout 15 --max-time 30
```

Expect HTTP 200 and non-empty response in < 3s.

## Running the Baseline

### ❌ What NOT to do (Windows)

The starter kit's CLI (`dabench run-task`, `dabench run-benchmark`) uses `multiprocessing.Process` internally. On Windows the default spawn mechanism re-imports the calling script, breaking with heredoc scripts, relative paths, and Chinese-character paths.

**Do not use** `uv run dabench run-task task_11 --config ...`

### ✅ What to do — Direct Python invocation

Write a thin script that imports the starter kit modules directly, creates model + tools explicitly, calls `agent.run(task)`, and saves outputs manually.

```python
import sys, json, csv
sys.path.insert(0, 'src')
from pathlib import Path
from data_agent_baseline.config import load_app_config
from data_agent_baseline.agents.model import OpenAIModelAdapter
from data_agent_baseline.agents.react import ReActAgent, ReActAgentConfig
from data_agent_baseline.benchmark.dataset import DABenchPublicDataset
from data_agent_baseline.tools.registry import create_default_tool_registry

config = load_app_config(Path('configs/react_baseline.local.yaml'))
dataset = DABenchPublicDataset(config.dataset.root_path)
task = dataset.get_task('task_11')
model = OpenAIModelAdapter(
    model=config.agent.model, api_base=config.agent.api_base,
    api_key=config.agent.api_key, temperature=0.0,
)
agent = ReActAgent(
    model=model, tools=create_default_tool_registry(),
    config=ReActAgentConfig(max_steps=16),
)
result = agent.run(task)

# Save outputs
out_dir = Path('artifacts/runs/my_run/task_11')
out_dir.mkdir(parents=True, exist_ok=True)
trace = result.to_dict()
(out_dir / 'trace.json').write_text(json.dumps(trace, ensure_ascii=False, indent=2))

if result.answer:
    csv_path = out_dir / 'prediction.csv'
    with open(csv_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(result.answer.columns)
        for row in result.answer.rows:
            w.writerow(list(row))
    print(f'Answer: {result.answer.columns} — {len(result.answer.rows)} rows')
else:
    print(f'No answer: {result.failure_reason}')
print(f'Steps: {len(result.steps)}')
```

## Baseline Speed Profile

The ReAct baseline is **inherently serial** — each step is one API round trip with growing context.

| Factor | Typical |
|--------|---------|
| DeepSeek v4 Flash per call | 1-3s |
| Steps per easy task | 6-10 |
| Steps per hard task | 12-16 |
| Context growth effect | +20-50% on later steps |
| **Total per task** | **~20s - 2min** |

BrightLiao's L2 (context preload) cuts steps by 30-50%.

## Pitfalls

| Problem | Symptom | Fix |
|---------|---------|-----|
| Invalid model name | First API call hangs until timeout | Verify via `/v1/models` endpoint |
| YAML indentation | `ScannerError` | Align all sibling keys |
| Windows subprocess | Task timed out with 0 steps | Use direct Python invocation |
| Expired/short API key | `401 Authentication Fails` | Update `api_key` |
| Flash vs Pro cost | Pro 3-5x slower/more expensive | Use `-v4-flash` for dev, Pro for final |

## Verification

After running, confirm:
1. `trace.json` exists with non-empty `steps` array
2. `prediction.csv` exists with expected columns (successful runs)
3. `e2e_elapsed_seconds` < 3 min per task
