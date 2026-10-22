---
name: resume-optimization
description: "Use when writing or auditing a resume for a target role."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [resume, cv, job-search, careers, portfolio]
    related_skills: [portfolio-repo-audit, grounded-citations, docx]
---

# Resume Optimization

## When to use

Rewriting or reviewing a resume/CV for a specific target role. Policies that hold
for every instance:

- **Pin the target direction before writing a word.** The same experience gets a
  different emphasis per direction, and averaging them produces a resume strong for
  nothing. Produce a separate version per direction rather than one blended
  document.
- **Every number must be traceable to an artifact.** If it cannot be retrieved, it
  does not go in.
- **Mark unknowns as bracketed placeholders and ask; never invent them.** GPA,
  class rank, award level, internship vs full-time, certificate scores.
- **Cross-check the resume against its own public evidence in the same session.**
  When the resume links a repo, audit that repo too — use `portfolio-repo-audit`.
- **Keep two files, never one:** the clean resume, and a companion notes file for
  commentary. The resume file is meant to be converted to Word/PDF as-is.
- **Keep the previous version alongside a new one** instead of overwriting, so a
  regression can be diffed.

## Step 1 — Pin the target role

Ask which direction is primary. Then the whole document shifts with it: an
AI/LLM-application version leads with retrieval, prompting, and model integration;
a backend version leads with service design, storage, and concurrency and compresses
the AI work to a few lines. Section order, bullet selection, and the skills list all
move.

Write the target direction into the header line as the job objective. Narrowing it
reads as more senior than a generic "software engineer".

## Step 2 — Inventory the evidence

Collect the artifacts behind every claim first: repos, commit counts and dates,
file contents, measured numbers, certificate names, award levels. Then draft.

## Step 3 — Section order for a student / new-grad

1. Name, contact line, **target role**, links (a live repo URL belongs here)
2. A three-line summary — positioning, strongest concrete asset, engineering range
3. Education: degree, dates, coursework grouped by theme, GPA **and class rank** —
   rank is more persuasive when available
4. Projects, strongest first — this is where a new-grad resume is actually won
5. Internships / work experience
6. Skills, ordered by the target role
7. Awards and certificates
8. A three-line self-assessment at most, stripped of personality adjectives

The summary and the first project carry the resume; everything else is supporting
evidence. Delete hobby lines and adjective stacks — they consume space and add no
signal.

## Step 4 — Bullet formula

Every project bullet: **problem → approach → quantified result**, verb-first.

- Describe the architectural decision, not the activity. "Designed an
  offset-mapped parent-child chunking scheme so child windows never break at parent
  boundaries, cutting total chunks 44% and removing tail loss" beats "implemented
  chunking".
- Put the number where the reviewer's eye stops: counts, latency before/after,
  compression ratios, window sizes, candidate set sizes.
- Mark ablation-style engineering honestly. A CLI exposing toggles to compare each
  module's contribution is a genuine strength — most candidates cannot show why
  their choices were right.
- Compress a module list into one or two bullets; readers do not track line items.

## Step 5 — Skills order is a positioning statement

Order by target role, not by proficiency. The stack you want to be hired for comes
first even if you are stronger in something else. Push unrelated stacks down and
delete anything you would not want to be interviewed on.

## Step 6 — Consistency audit against public evidence

This is the step most resumes skip, and where a resume gets caught.

- Project date ranges must match the linked repo's **first commit date**:
  `git log --reverse --date=short --pretty=format:'%ad %h %s' | head -1`. If the
  resume claims an earlier start, either fix the resume or be ready to explain the
  gap.
- File counts, dataset sizes, record counts, and compression ratios quoted in the
  resume must be findable in the repo.
- A claim in the linked repo's README that overstates (an accuracy improvement with
  no measurement behind it) is a resume problem too — the reviewer reads both.

## Step 7 — Predicted-interview check

For each bullet, write down the follow-up question it invites. If the answer does
not exist, cut or soften the bullet — a claim that cannot survive one follow-up is a
liability, not a strength.

Typical probes: "why that approach over the obvious alternative?", "how did you
measure that improvement, and on what eval set?", "what happens at the boundary or
when it fails?", "what did you change after reading that source?".

**Label the evidence level accurately.** Reading a codebase and running it is
reproduction, not authorship — say "reproduced the official baseline and read the
source" and the interviewer is impressed by the depth. Say "built" and the first
follow-up exposes it. The same applies to a reverted or abandoned approach: honest
history reads as engineering maturity.

## Formatting rules

- One page for a student / new-grad. Cut bullets, not detail.
- One accent colour; body text in a single serif or sans face.
- Omit photo, hometown, political affiliation, and expected salary.
- Filename: `Name-School-TargetRole.pdf` — the reviewer files it and later recalls
  whose it was without opening it.

## Working style

- Present the plan and the changed sections for approval before rewriting wholesale.
- Give absolute file paths and the exact commands so the work can be re-run.
- Flag contradictions (a claimed date the repo disproves, an award line that looks
  like the same competition recorded twice) rather than quietly normalizing them.
- When recommending a cut, give the reason in one clause.
