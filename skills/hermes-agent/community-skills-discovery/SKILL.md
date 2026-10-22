---
name: community-skills-discovery
version: 1
description: >
  Discover, evaluate, and install third-party skills for Hermes Agent from GitHub
  and other community registries. Covers searching for skill repos, evaluating quality
  (docs, structure, maintenance), installing via taps, and verifying the result.
category: hermes-agent
triggers:
  - "find skills for"
  - "search for skills on GitHub"
  - "install community skill"
  - "install from GitHub"
  - "third-party Hermes skill"
  - "skill repo"
  - "skills pack"
---

# Community Skills Discovery & Installation

## 1. Discovery Channels

### GitHub Search
Search for Hermes Agent skill repositories:
```
site:github.com hermes agent skill <topic>
site:github.com hermes-research-skills
site:github.com hermes-skills
```

### Curated Directories
- **awesome-hermes-agent** (0xNyk/awesome-hermes-agent) — 4.8k stars, curated index of skills, plugins, tools, and integrations. The canonical starting point for discovering the ecosystem.
- **Undermybelt/hermes-skills** — 1422 published SKILL.md files in a single repo, covering dev, security, research, productivity, creative, MCP. Bulk export of a live runtime skill collection (sanitized). Use `scripts/validate_skills.py` before installing.
- **Skills Hub** (`hermes-agent.nousresearch.com/docs/skills/`) — official registry with 79,949+ skills across 12 registries, auto-rebuilt twice daily.

### Hermes CLI Browsing
```bash
hermes skills browse                    # paginated browse of all registries
hermes skills browse --source skills-sh # filter by registry
hermes skills search <topic>            # search across registries
hermes skills inspect <identifier>      # preview name/description/SKILL.md WITHOUT installing
```

**Order the work registry-first.** `search` + `inspect` cost nothing, cover the same topic space as a GitHub search,
and anything found there installs through the mandatory scan. Falls back to GitHub repos only when the registry has
no real candidate — GitHub hits for a nesh topic are typically 0–2 stars with no LICENSE, i.e. no validation AND
a manual install that skips the scan. Use `inspect` before recommending anything: shallow "skill" entries are often
just a persona paragraph with no corpus, no scoring, and no record system, and that is only visible in the preview.

## 2. Evaluation Criteria

When evaluating a community skill repo:

| Criterion | What to check |
|-----------|---------------|
| **Stars** | Higher = more community validation |
| **README quality** | Long README with examples, workflow diagrams, quick-start = well-maintained |
| **Structure** | Each skill should have `SKILL.md` at minimum; `references/`, `templates/`, `scripts/` indicate maturity |
| **Maintenance** | Recent commits, CHANGELOG, CONTRIBUTING guide |
| **Packaging** | Skills organized into logical packs/umbrellas, not a flat list |
| **License** | MIT or Apache preferred. **A repo with no `LICENSE` file at all is "all rights reserved" by default** — fine for local personal use, but tell the user that, never present it as open source. |
| **Security** | Hermes CLI runs a security scan (SAFE/blocked verdict) on every `hermes skills install` — **but only on the registry install path. A repo that is not behind a registry can only be installed by manual copy, and the scan never runs. Audit it yourself (§3.5).** |

## 3. Installation

### Via Tap (recommended for multi-skill repos)

```bash
# 1. Add the GitHub repo as a skill source
hermes skills tap add owner/repo

# 2. Verify it was added
hermes skills tap list
# Expected: owner/repo → skills/

# 3. List what's available
hermes skills browse           # check the general listings
# Or install directly by ID

# 4. Install a specific skill (use full path: owner/repo/skills/skill-name)
hermes skills install "owner/repo/skills/skill-name" -y
# The -y flag skips the confirmation prompt
```

### Manual Copy (when you want to review first)

⚠️ **This path bypasses the security scan entirely — nothing scans a hand-copied skill.** Low risk for a doc-only
skill, but a bulk export bundles hundreds of files, so **audit the scripts first (§3.5)** before you copy anything.

```bash
# Download (codeload ZIP is far faster + more reliable than git clone on constrained networks)
curl -L -o /tmp/x.zip "https://codeload.github.com/<owner>/<repo>/zip/refs/heads/<branch>"

# Install one skill into the profile's skills dir
mkdir -p <hermes>/skills/<category>
cp -r <extracted>/<skill-dir> <hermes>/skills/<category>/<skill-name>/
```

### 3.5 Auditing a non-registry skill repo

Since no scan will run, do this yourself and **report exactly what you audited and what you did not**:

1. **Enumerate the executable surface, then filter it.** `GET /repos/<owner>/<repo>/git/trees/<branch>?recursive=1`
   and keep only `.py/.sh/.ps1/.js/.bat`. A large hit list is usually doc-site scaffolding (search indexes, minified
   bundles) — the real skill scripts are the handful sitting next to a `SKILL.md`.
2. **Grep every script for the four risk classes** before reading any of it line by line —
   exfiltration (`requests|urllib|socket|base64|curl |wget `), credentials (`api_key|token|password|os.environ` on
   secret-looking names), execution (`subprocess|os.system|Popen|exec\(|eval\(`), destruction
   (`rmtree|os.remove|os.unlink|shutil.move|chmod`).
   **Zero network imports across every script is the strongest single signal** — with no network library there is no
   exfiltration path, whatever else the code does.
3. **Read the context around every destructive and `subprocess` call.** Confirm the target is bounded to the skill's
   own workspace and that command arguments are a fixed list — a string-built command or `shell=True` is disqualifying.
   A `rmtree` that explicitly preserves the user-editable subdirectories before rebuilding is legitimate; say so.
4. **Check the workspace-resolution chain, then set the env var.** A script resolving its root as
   `env var → a hardcoded user dir → os.path.dirname(__file__)` will happily operate **inside its own install
   directory** when neither of the first two exists — creating and deleting files among your skills. Export the
   documented env var before running any of its scripts, and put that instruction in your report.
5. **Say it in the right words.** "I read all N scripts and grepped them for the four risk classes" is a supported
   claim; "this skill is scanned/safe" is not. When you could not cover something, name it.

### Verification

```bash
# Check it's listed
hermes skills list | grep <skill-name>

# The skill should show as "enabled"
```

## 4. Pitfalls

- **Hub-installed skills are protected** — you cannot edit or delete them with `skill_manage`. They can only be removed via `hermes skills uninstall`.
- **Timed-out installs may still succeed** — if `hermes skills install` times out (exit 124), check `hermes skills list` before retrying. The skill was likely installed before the HTTP response finished.
- **Full path is required** — use `"owner/repo/skills/skill-name"`, not just the skill name.
- **Security scan is mandatory — but only on the registry install path.** Every `hermes skills install` goes through
  quarantine + scan (NATIVE to Hermes, not optional; a blocked verdict needs `--force`). **A manual copy is never
  scanned**, so the §3.5 audit is your only control. Never describe a hand-copied skill as "scanned".
- **Repository layout decides whether the tap path can work at all.** `hermes skills tap add` expects
  `<path>/skills/<name>/SKILL.md`. Repos with skill folders at the root, or in a singular `skill/`, won't index —
  check the tree before promising a tap install; those are manual-copy only.
- **`git clone` is often the wrong downloader.** On constrained networks prefer
  `curl -L -o x.zip https://codeload.github.com/<owner>/<repo>/zip/refs/heads/<branch>`; `raw.githubusercontent.com`
  handles single-file inspection fine. Reserve `git clone` for repos you actually need history from.
- **A pack can require all of its members installed side by side** (one entry-point skill orchestrating N dependencies,
  with relative paths between them). Spitting the pack or installing one folder silently degrades it — check the README
  for an "install as a whole" instruction before copying a subset.
- **Taps persist** — once added, a tap stays in your config until explicitly removed with `hermes skills tap remove owner/repo`.
- Not all skills in a collection are relevant to your task — evaluate individually per Section 2 criteria.
