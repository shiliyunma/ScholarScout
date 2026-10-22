---
name: exam-planning
description: "Create structured exam study plans through interview-guided decision trees. Covers exam selection, subject decomposition, phased study timelines, resource/material planning, and ongoing conversation record maintenance."
version: 1.0.0
author: Hermes Agent (adapted from 考研规划 session)
license: MIT
platforms: [windows, macos, linux]
metadata:
  hermes:
    tags: [exam, planning, study, schedule, interview, roadmap, Chinese-exams]
    related_skills: [plan, kanban-orchestrator]
---

# Exam Planning

Use this skill when the user wants to create a study plan for a major exam (考研, 雅思, 托福, 公务员, 资格认证 etc.).

## Core Behavior

You are building a structured, actionable study plan. Do NOT just output a generic template — interview the user to understand their specific situation, then build a plan tailored to their target, baseline, and time budget.

## Interview Methodology

Adapt the **design interview pattern** from the `plan` skill (one question at a time, recommended answers, decision tree). The exam-specific branches to walk:

### Branch 1: Goal & Target
- What exam? What school/program? (学硕 vs 专硕, 统考 vs 自命题)
- When is the exam? (date or month)
- Current status (what year of school, employed, etc.)

### Branch 2: Subjects & Structure
- What subjects are tested? Codes (e.g. 11408, 201, 301)?
- For each subject: what's the score weight? What content domains?

### Branch 3: Current Baseline
- Current level per subject (grades, scores, self-assessment)
- What materials already used? What teachers/courses followed?
- Phase completed (基础/强化/真题/冲刺)?

### Branch 4: Time Budget
- Hours per day available for study (total and per subject)
- Days per week
- Major time conflicts (school, work, internship)
- Total weeks until exam

### Branch 5: Material & Teacher Selection
- What materials owned vs needed?
- Teacher recommendations for each subject (domain-specific — stored in references/)
- Book buying priorities

## Building the Plan

Once interviews complete, produce a structured plan with:

### 1. Phase Breakdown
Divide remaining time into clear phases with dates:
- **Phase 1**: Foundation/completion of basics
- **Phase 2**: Intensive review (强化阶段)
- **Phase 3**: Past papers / mock exams (真题阶段)
- **Phase 4**: Final sprint / simulation (冲刺阶段)

### 2. Time Budget per Phase
Calculate total available hours per phase and allocate:
```
Phase 1: 3 weeks × 27h/week = 81h
Phase 2: 8 weeks × 27h/week = 216h
...
```

### 3. Milestones
Add key checkpoints with dates and concrete targets.

### 4. Material Roadmap
What to buy now vs later in the timeline.

## Conversation Record Maintenance

When the user specifies a **work directory** for recording the process:

1. Create `{workdir}/考研规划-对话记录.md` (or `<exam>-规划-对话记录.md`)
2. After **each round of questions**, update the file using `patch` (append new sections, never rewrite from scratch)
3. Structure the MD with clear tables for:
   - Basic info (target, current status)
   - Per-subject progress tables
   - Decision logs (books chosen, teachers, timeline)
   - Phase plans
4. Use `patch` with `old_string` targeting the last line of the file (typically the `"*本文件持续追加*"` footer) to append content

## Pitfalls

- **Don't ask multiple questions at once.** The user explicitly wants one at a time. Piling on is confusing.
- **Don't dig into irrelevant details.** If the user says "那些先不需要你去了解", stop immediately on that branch and return to the agreed scope.
- **Don't assume exam structure.** Verify exam codes (e.g. 11408 vs 11407, 数学一 vs 数学二) — they differ by school and program. Ask or look up the official site.
- **Don't skip the time budget.** Without knowing hours/day, a phased plan is meaningless. Always compute total available hours.
- **Don't forget to update the record file.** The user may explicitly remind you ("继续维护对话记录文件") — build the habit of updating after every major checkpoint.
- **For Chinese exams (考研 specifically):** Teachers matter significantly. Don't just recommend any teacher — store known-good pairings in references/ and update them as you learn.
- **Book recommendations need purchase timing.** Some books (foundation) are needed immediately; others (simulation papers) only in the final phase.
