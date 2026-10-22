---
name: competition-code-research
description: Systematically research open-source competition solutions on GitHub — find official info, discover participant repos, evaluate quality, download efficiently, and organize results.
---

# Competition Code Research

Systematically discover, evaluate, download, and document open-source code from AI/ML competitions (KDD Cup, Kaggle, etc.).

## When to use

- User asks to "find competition solutions" or "download winning code"
- User names a specific competition and wants open-source repos
- User wants to benchmark or learn from top solutions

## Workflow

### Phase 1: Gather competition info

1. **Search official website**: `web_search(query="<competition name> <year>")` → find official page
2. **Extract key facts**: prize pool, tracks, timeline, scoring metric, submission format
3. **Get leaderboard**: search the official site's leaderboard page for team names and scores
4. **Save to file**: write competition overview as a markdown file in the target directory

### Phase 2: Discover participant repos

1. **Web search patterns** (iterate these):
   - `site:github.com <competition> <track> solution`
   - `site:github.com <competition> <track>`
   - `<competition> <year> <track> winner solution code github`
   - `<competition> <year> champion open source`
   - Chinese: `<比赛名称> 方案 开源 GitHub`
   
2. **Check GitHub Topics**: `https://github.com/topics/<competition-tag>` (e.g. kddcup2026)
3. **Evaluate each candidate**: check stars, description, README quality, whether it's a meaningful implementation vs a clone of the starter kit

### Phase 3: Download repos

**In China (GitHub port 443 blocked):**
```bash
# Use codeload.github.com — fast and reliable
curl -L -o "<dir>/<name>.zip" "https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
```

**Branch discovery**: try `main` first (most common), fall back to `master`.

**Individual files** (for READMEs or small configs):
```bash
curl -L -o "<dir>/README.md" "https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
```

**Where direct clone works** (good network):
```bash
git clone --depth 1 "https://github.com/{owner}/{repo}.git" "<dir>/<name>"
```

### Phase 4: Organize and document

1. **Extract ZIPs**: `unzip -qo <file>.zip -d <dir> && mv <dir>/<top-folder>/* <target-name>/`
2. **Create index**: write a `对话记录.md` or `README-总览.md` with:
   - Competition overview
   - Leaderboard (top 10+)
   - Table of downloaded repos (name, size, source, description)
   - Key technical highlights (best architectures, notable improvements)
3. **Update conversation record**: append to the user's persistent conversation log if one exists

### Phase 5: Setup and run the starter kit

After downloading, set up the environment and run the baseline before diving into code.

#### Step 1 — Environment setup

```bash
cd <phase_dir>
uv sync                   # Install deps (generates .venv/)
```

Verify the config file exists and has valid YAML:

```bash
ls configs/
# Look for *.local.yaml or *.example.yaml
```

#### Step 2 — Debug YAML config

**Common pitfall: indentation mismatch** — `api_key` gets extra indentation (6 spaces) while sibling keys use 2 spaces, causing:

```
ScannerError: mapping values are not allowed here
  in "<unicode string>", line 7, column 14:
          api_key: sk-xxx...
                 ^
```

**Fix**: ensure all keys at the same level have **identical** indentation:

```yaml
agent:
  model: deepseek-v4-flash     # 2 spaces ← use valid model name
  api_base: https://...        # 2 spaces
  api_key: sk-xxx              # 2 spaces ← must match above
  max_steps: 16
```

Other YAML gotchas:
- Trailing comments after `api_key:` (e.g. `api_key: sk-xxx # my key`) leak the secret text into the parsed value
- The `...` placeholder pattern (`sk-xxx...yyy`) is NOT valid YAML — replace with the real key
- Mixing tabs and spaces: always use spaces

#### Step 3 — Configure API key

1. Copy `*.example.yaml` to `*.local.yaml` (gitignored)
2. Fill in `api_key` and `api_base` for your LLM provider
3. Set `model` to the actual model name (e.g. `deepseek-v4-flash`, `gpt-4o`)

**⚠️ Critical: DeepSeek model naming** — The old alias `deepseek-chat` no longer works on DeepSeek's current API. Available models (verify via `curl https://api.deepseek.com/v1/models`):

| Model | Type | Use case |
|-------|------|----------|
| `deepseek-v4-flash` | Fast/cheap | Development, debugging, easy tasks |
| `deepseek-v4-pro` | Stronger | Hard tasks, final submissions |

Using a non-existent model name (like `deepseek-chat`) causes the first API call to hang indefinitely — **no error reported, zero output for 10+ minutes**. Always verify with curl before running the agent.

#### Step 4 — Verify environment

```bash
uv run dabench status --config configs/<your-config>.yaml
```

Expected output shows: project_root (ready), data_dir (present), config_path (present), and task counts by difficulty.

#### Step 5 — Inspect and run a single task

```bash
# Inspect first — see the question, context files, difficulty
uv run dabench inspect-task task_<id> --config configs/<your-config>.yaml

# Run one task — produces artifacts/runs/<run_id>/<task_id>/prediction.csv
uv run dabench run-task task_<id> --config configs/<your-config>.yaml
```

Start with an `easy` task for the first run.

#### Step 6 — Troubleshoot common failures

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ScannerError: mapping values are not allowed here` | YAML indentation | Check all sibling keys share the same indent level |
| `Authentication Fails, Your api key: ****xxx is invalid` | Bad/stale API key | Replace the key in `.local.yaml` |
| `401 - invalid_request_error` | Wrong provider endpoint | Check `api_base` matches the provider |
| `dabench: command not found` | uv environment not active | Run via `uv run dabench` not bare `dabench` |
| `No module named ...` | deps not installed | Re-run `uv sync` |
| `Prediction CSV: not generated` | Task failed mid-run | Check `trace.json` in the task output dir for the error |
| `run_id: [WinError 183] 当文件已存在时` | Fixed `run_id` collides with prior run | Remove `run_id` from YAML config, or delete `artifacts/runs/<run_id>/` |
| Zero output for 5+ minutes, process hangs | API key invalid (silent auth failure) | Test with `curl` first — see `references/baseline-execution-guide.md` |
| Zero output, no 401 error, process hangs for 10 min | **Wrong model name** (e.g. `deepseek-chat` instead of `deepseek-v4-flash`) | Check available models via `curl https://api.deepseek.com/v1/models -H "Authorization: Bearer $KEY"` |

**Performance expectations**: the ReAct baseline is inherently serial (1 LLM call per step, max 16 steps). Expect 30s–2min per task on DeepSeek. See `references/baseline-execution-guide.md` for the full performance breakdown and troubleshooting guide.

## Scoring repos for value

| Signal | What it means |
|--------|---------------|
| Has docs/ folder with Chinese documentation | High — shows structured thinking |
| Shows incremental improvements (v1 → v2 → v3) | High — real iterative development |
| Has Docker packaging | High — competition-ready |
| Has evaluation reports with metrics | High — reproducible results |
| Pure starter-kit fork with no changes | Low — skip |
| Single commit, no README | Low — unpublished experiment |

## GitHub download tips (China network)

- `codeload.github.com` ZIP: 8MB/s+ ✅ (preferred)
- `raw.githubusercontent.com`: works for individual files ✅
- `git clone` to `github.com:443`: almost always times out ❌
- Proxy mirrors (`ghproxy.com`, `hub.fastgit.xyz`): unreliable ❌
- Browser (Hermes built-in): works but slow ❌

## Troubleshooting

**Problem**: ZIP download gets 14 bytes (HTML instead of ZIP)
→ **Fix**: wrong branch name. The repo might use `master` not `main`.

**Problem**: ZIP download times out before completing
→ **Fix**: use `--max-time 600` for large repos (>10MB) and run in background.

**Problem**: "Failed to connect" on all approaches
→ **Fix**: try the browser approach (Hermes desktop browser can reach GitHub when the terminal cannot).
