# KDD Cup 2026 Data Agent — Research Findings

> Session: 2026-07-20 | Used by: competition-code-research skill

## Competition basics

| Field | Value |
|-------|-------|
| Full name | KDD Cup 2026: Data Agents for Complex Data Analysis |
| Host | HKUST(GZ) DIAL Lab (Prof. Lei Chen) |
| Website | https://dataagent.top |
| Prize | ~$30,000 |
| Timeline | Mar 15 – Aug 9, 2026 |
| Submission | Docker image |
| Scoring | Score = Recall − λ · (Extra Cols / Predicted Cols), λ=0.5 |

## Leaderboard Top 10

| Rank | Team | Score | Award |
|------|------|-------|-------|
| 1 | TorchTensor | 0.6496 | Champion |
| 2 | NV Data Explorer | 0.6109 | Runner-up |
| 3 | afanti | 0.5900 | 2nd Runner-up |
| 4 | Team KOBUSHI | 0.5880 | Merit |
| 5 | jinfengxiyulou | 0.5822 | Merit |
| 6 | Cogitating | 0.5707 | Merit |
| 7 | idmg123 | 0.5695 | Merit |
| 8 | Degas | 0.5656 | Merit |
| 9 | Mamba Never Out | 0.5608 | Merit |
| 10 | PulsePause | 0.5586 | Merit |

## Key repos discovered

### Must-read: BrightLiao Chinese solution
- **Repo**: github.com/BrightLiao/KDDCupDataAgent
- **Value**: Detailed Chinese docs, 4-layer improvement architecture, full Docker pipeline
- **4 layers**: CodeAct → Context Preloading → Shape Spec Extraction → Plan-Executor

### Official
- Starter kit: github.com/HKUSTDial/kddcup2026-data-agents-starter-kit (master branch)
- Paper list: github.com/HKUSTDial/awesome-data-agents (647⭐)
- Benchmark: github.com/ucbepic/DataAgentBench

### Participant solutions
- Team 1166 (Gold Miner, rank 32): github.com/stulyf/kdd2026-team1166-solution
- Eiomm ReAct baseline: github.com/Eiomm/kddcup2026-data-agent (master branch)
- sycomorus: github.com/sycomorus/Data-Agent
- ShaeRay: github.com/ShaeRay/kdd2026 (master branch)

### Data Agent taxonomy (from awesome-data-agents survey)
- L0: Manual — human does everything
- L1: Assisted — LLM as Q&A helper
- L2: Partial autonomy — agent perceives environment, executes procedurally
- L3: Conditional autonomy — agent orchestrates autonomously, human supervises
- L4: High autonomy — agent proactive, human delegates
- L5: Full autonomy — generative agent, no human needed

Most competition entries span L2–L3. The gap between L2 (procedural execution) and L3 (autonomous orchestration) is the active research frontier.

## Download commands used

```bash
# All used codeload.github.com with curl
curl -L -o "<name>.zip" "https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
# branch = main or master (try both)
```
