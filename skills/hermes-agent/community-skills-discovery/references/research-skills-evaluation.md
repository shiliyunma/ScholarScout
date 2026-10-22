# Research-Focused Hermes Skills: Discovered & Evaluated

Evaluated: 2026-07-20 | Search sources: GitHub, skills.sh, awesome-hermes-agent

## Repos Found

### 1. gy910210/hermes-research-skills ⭐ #1 pick
**Stars**: 1 | **License**: MIT | **Status**: Installed (Core Pack + Monitoring Pack)
- **Strengths**: Most complete Chinese-first research workflow in the ecosystem. 18+ skills organized into 5 logical packs (Core / Monitoring / Concept / Methodology / Catalog). Excellent README (710 lines) with example repo, quick-start guides, minimal directory template. Supports `hermes skills tap add` for easy installation.
- **Weaknesses**: Single-author, low stars (new), still a monorepo (not physically split into packages yet).
- **Pack structure**:
  - Core Pack: research-orchestrator-zh, brief-ingester-zh, literature-scout-zh, paper-reader-zh, paper-critic-zh, research-synthesizer-zh, report-maintainer-zh
  - Monitoring Pack: scheduled-research-monitor-zh, knowledge-base-updater-zh
  - Concept & Review Pack: concept-boundary-analyzer-zh, concept-registry-maintainer-zh, critical-iteration-lab-zh, role-review-panel-zh
  - Methodology & Diagram Pack: methodology-sample-flow-extractor-zh, doc-diagram-orchestrator-zh
  - Catalog & Repo Maintenance: awesome-repo-maintainer-zh
- **Standard workflow**: research-orchestrator-zh → brief-ingester-zh → literature-scout-zh → paper-reader-zh → paper-critic-zh → research-synthesizer-zh → report-maintainer-zh
- **Install command**: `hermes skills tap add gy910210/hermes-research-skills` then `hermes skills install "gy910210/hermes-research-skills/skills/<name>-zh" -y`

### 2. NousResearch/hermes-agent — builtin research skills
**Stars**: 218k | **License**: MIT | **Status**: Already installed (bundled)
- arxiv — arXiv REST API search
- blogwatcher — RSS/Atom feed monitoring
- llm-wiki — Karpathy-style interlinked markdown knowledge base
- research-paper-writing — full paper lifecycle (experiment → submission), includes reviewer-guide references

### 3. Undermybelt/hermes-skills
**Stars**: 4 | **License**: - | **Status**: Not installed
- 1422 skills total, 17 in skills/research/
- Notable: auto-research, arxiv-research-synths, openalex-paper-search, paper-search-mcp, research-knowledge-ops, research-paper-write, research-innvtn-explorer, github-trending-spider
- Bulk export — validate before installing, individual docs are sparse

### 4. 0xNyk/awesome-hermes-agent
**Stars**: 4.8k | **Forks**: 340 | **Status**: Reference directory only
- Not a skill repo — a curated index/awesome-list of the Hermes ecosystem
- Best starting point for discovering new skills, plugins, tools
- README links to all major community resources

## Installation Commands Used

```bash
# Add a GitHub tap
hermes skills tap add gy910210/hermes-research-skills

# List taps
hermes skills tap list

# Install a specific skill from a tap
hermes skills install "gy910210/hermes-research-skills/skills/research-orchestrator-zh" -y

# Verify installation
hermes skills list | grep <skill-name>
```

## Key Observations

- `hermes skills install` runs a security scan on every install — verdict is logged (SAFE/blocked).
- Timed-out installs (exit 124) often succeed — always check with `hermes skills list` before retrying.
- Hub-installed skills are protected from `skill_manage` editing.
