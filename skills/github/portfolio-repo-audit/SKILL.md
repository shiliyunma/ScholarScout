---
name: portfolio-repo-audit
description: "Use when a repo is linked from a resume. Audit + polish."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [github, portfolio, readme, open-source, resume, code-review]
    related_skills: [github-repo-management, github-auth, grounded-citations]
---

# Portfolio Repo Audit

## When to use

Any repo that will be linked from a resume, CV, portfolio, or application — the
reviewer will open it. Policies that hold for every instance of this task:

- Audit it as a deliverable with a reader, not as code with an author.
- Read every image for leaked content before committing it: a screenshot showing a
  sidebar of real query history can expose a real name, a customer name, or
  internal data. Images are a desensitization surface exactly like code — a repo
  you just cleaned can be re-exposed by one careless screenshot.
- Any figure quoted *about* the repo (in the resume, README, or application) must be
  traceable to the repo. Audit the claim and its evidence in the same session.
- Report claim and wording changes as findings for the owner to decide; fix
  file-level defects (missing LICENSE, undeclared dependency, broken setup step)
  once the plan is agreed.
- A README describes the *project*: what it is, how to run it, how it is layered,
  its config, its known pitfalls. Roadmap, milestones, next-step priorities,
  acceptance criteria, thesis-chapter mapping and interview framing belong wherever
  the author keeps planning material — usually a directory outside the repo. When
  asked to tidy a README, *move that content out* to the author's planning home
  rather than polishing it in place, then grep the README for the giveaway words
  (roadmap / next steps / milestone / acceptance / interview) and expect zero hits.

This is distinct from repo *management* (create/clone/fork/release mechanics). If
the repo has secrets in its history, load `github-repo-management` and run its
sanitization plus history-rewrite sections first — a force push alone does not
remove them from the server.

## Step 1 — Read the repo the way the reviewer will

Open the home page as a stranger: description present? screenshot above the fold?
license shown? README opening with what the thing *does*? Then read the commit
history as a story — does it read as iteration (consistent prefixes, honest
reverts, build-up over time) or as one bulk dump?

Collect the facts now so claims get checked rather than assumed:

```bash
git rev-list --all --count                                          # commit count
git log --reverse --date=short --pretty=format:'%ad %h %s' | head -1  # first commit date
git log --date=short --pretty=format:'%ad %h %s' | head -30           # recent history
wc -l *.py **/*.py 2>/dev/null | tail -1                             # code volume
```

## Step 2 — Dependency coverage (the most common blocker)

A clean clone that dies on `ImportError` is the fastest way to lose the reviewer.

```bash
python scripts/check_requirements_coverage.py /path/to/repo
```

It exits non-zero on anything undeclared, so it can gate the "repo is ready"
claim. Two things it handles that a naive scan does not:

- **It prunes `.venv`/`node_modules` before walking.** Walking the tree without
  pruning descends into tens of thousands of dependency files and reports hundreds
  of bogus "missing modules" (numpy, pytest, scipy, ...) — an alarming, worthless
  result. Prune first.
- **It maps import names to distribution names.** Code may `import PyPDF2` while
  requirements lists `pypdf`, `import PIL` for `pillow`, `import win32com` for
  `pywin32`, `import docx` for `python-docx`, `import langchain_text_splitters`
  for `langchain-text-splitters`. Declare what the import statement needs, and
  declare *both* when both are genuinely imported.

## Step 3 — README promises vs repo reality

Each of these reads to a reviewer as "said it, didn't do it":

- **LICENSE declared but missing.** README ends with a `## License / MIT` section
  while no `LICENSE` file is in the tree → add the file.
- **Setup instructions starting from an untracked file.** A README saying "edit
  `config.py`" when `config.py` is gitignored means a clone has no such file.
  Every step must start from a tracked file: "`cp config.example.py config.py`,
  then edit", with the Windows `copy` form for Windows users.
- **Clone command only in SSH form.** Add HTTPS; a reviewer without an SSH key
  stops at step 1.
- **Unmeasured metrics.** A feature table quoting an accuracy, recall, or
  precision improvement copied from the roadmap rather than from a measurement is
  an interview landmine — the first follow-up ("how did you measure it, on what
  eval set?") exposes it. Either measure it or replace it with a qualitative
  description of the mechanism.
- **Committed third-party material** (textbook text, courseware, scanned
  documents) → add a provenance line limiting it to local learning use and
  disclaiming ownership.

### Never write the README's factual tables from memory

A recalled env-var table, CLI flag list, directory tree or feature list *is* the defect
this step hunts: a documented falsehood the reviewer hits on the first command. Take every
value from the source before writing the section:

```bash
grep -rn "os.getenv\|environ" --include="*.py" .    # the real environment variables
grep -n "add_argument" <cli entry point>            # the real flags — a flag is NOT an env var
git ls-files                                        # the real tree, for the directory section
cat requirements.txt                                 # the real dependency claim
```

Copy names and defaults verbatim, and say plainly which settings are *not*
configurable (hardcoded in a config module, overridable only by a flag). A README naming
four variables that do not exist is worse than one naming none — and it is the same
"said it, didn't do it" failure as an unmeasured metric.

When a README section is moved elsewhere, cross-reference it by **section name, not
number** (`the "add a tool" section`, never `section 4`) — numbering shifts the moment a
section moves.

## Step 4 — Visual proof

A README with no screenshot loses a reviewer in the first five seconds, and an
architecture diagram is not a substitute. Put two images near the top, right after
the one-line description and before the feature table: the primary mode and the
differentiating secondary mode. Crop recipe, palette quantization, and the section
template are in `references/readme-presentation.md`.

## Step 5 — About panel: description and topics

An empty description wastes the reviewer's first glance, and topics drive GitHub
search and related-repo surfacing. Both are set from the repo home page's About
panel (gear icon). Suggested wording and a topic list are in
`references/readme-presentation.md`.

## Step 6 — Consistency with what is claimed about the repo

Every figure a resume or application quotes must be traceable here: project start
date vs first commit, chunk and record counts, dataset sizes, compression ratios.
A resume claiming a start date months before the first commit is a contradiction
the reviewer will notice — and it takes one line to fix. When one artifact links to
the other, audit both in the same session.

**Regenerate the size figures from the tree itself instead of trusting any existing
copy.** File / line / test / migration counts get written into the resume, the README
and the thesis abstract months apart and then drift: the same project carried
`72 files / 712 tests / 6 migrations` in its abstract while another document said
`75 files / 12,314 lines / 830 tests`, and the real migration count was already higher
than both. The stale copy is always the **summary** one (abstract, README badge, resume
line) — written once, never revisited. Count fresh (`git ls-files`, the test collector,
`ls <migrations dir>`) and align every artifact in the same pass.

**A link goes into a resume only after it resolves.** Probe the exact URL the reader
will click. A repo that was never pushed answers 404, and a dead portfolio link is
worse than no link: it converts the page's strongest claim into its most checkable
failure. Either publish first or drop the link and keep the project text.

## Step 7 — Tests

Zero tests is the most common substantive criticism of a portfolio repo. Even a
handful of unit tests on the core algorithm (chunking, merging, scoring) moves the
signal from "it runs" to "they understand its boundaries". Recommend them; never
silently invent tests and report coverage that does not exist.

## Pitfalls

- **Never issue two `patch` calls for the same file in one turn.** Both execute
  against the pre-edit state, so the second fails to match or is silently lost.
  README/CHANGELOG polish always means several edits to one file: apply them in a
  single `execute_code` call looping over `patch()` sequentially, then grep the
  file for every inserted anchor to confirm each landed.
- **Verify the push through the API, not through a raw-content fetch.**
  `raw.githubusercontent.com` is intermittently unreachable and its failures look
  like "the push did not land". Compare `git hash-object <file>` against the blob
  sha from the contents API — identical shas prove a byte-identical push.
- **A push's own output is not proof, and neither is a local `git status`.** Read the
  remote back: `git rev-parse HEAD` must equal `git ls-remote origin refs/heads/main`.
  Then check what actually *is* public — `git ls-tree -r --name-only origin/main |
  grep -E '^run/|\.env$|__pycache__|\.db$'` must be empty. `.gitignore` only governs
  what the next `git add` would pick up; the pushed tree is the published surface.
  Finally, `git clone` into a temp dir and run the project's own self-check there: it is
  the cheapest proof that nothing needed by an `import` was left uncommitted, and it
  exercises the README's setup steps for real. Delete the temp clone afterwards.
- **Confirm the default branch resolves after a repo is recreated.** A freshly
  created repo may report `default_branch: main` while you pushed `master`. Check
  that `GET /repos/{owner}/{repo}/readme` returns 200, otherwise the home page
  shows the empty-repo setup instructions instead of the README.
- **Probe the remote before assuming anything about it.** `git ls-remote <url>` is the
  single call that answers both "does it exist" and "does auth work": a repo that exists
  but has **no refs (freshly created, empty)** prints *nothing and exits 0*, while a
  missing repo or failed auth prints an error (`Repository not found`,
  `Permission denied`) and exits non-zero. Empty output is not evidence the repo is
  missing — go create it again and you will have two competing repos.
- **Never `git init` the same project twice once a remote exists.** Initialising a new
  or nested directory forks the history so it can no longer be pushed to the existing
  remote; moving a repo root is a deliberate migration (fresh clone / `filter-repo`),
  never a re-init. And on a Windows-edited project, add `.gitattributes`
  (`* text=auto` plus `*.py text eol=lf`) *before* the first commit, or every later
  checkout reports whole-file modifications; mark README/data files
  `linguist-documentation` so GitHub's language stats reflect code rather than prose.

## Support files

| File | Use |
|------|-----|
| `scripts/check_requirements_coverage.py` | Declares-what-it-imports gate; prunes dependency dirs, resolves import↔package aliases |
| `references/readme-presentation.md` | Screenshot crop + quantization recipes, README section template, About panel wording and topics, post-push API verification, reviewer's-eye checklist |
