---
name: subagent-delegation-verification
description: "Use when delegating to subagents and verifying results."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [delegation, subagents, verification, orchestration]
    category: autonomous-ai-agents
---

# Delegating work to subagents — and verifying what comes back

A child's summary is a **self-report**, not a fact. Every rule here exists because a child returned
something that looked finished and was not: 0 commits with the work sitting in the working tree,
a gate claimed green that was never run, a module claimed created that does not exist.

## When to Use

- Fanning work out with `delegate_task` (one or many children)
- A child came back `TRUNCATED`, hit its iteration cap, or returned no terminal result at all
- The user asked for work to be delegated "so it doesn't pollute this context"
- You are about to accept a child's "done / pushed / tests pass" without looking

## Writing the brief

1. **Children know nothing.** Repeat the background in every task that needs it: absolute paths, repo
   name, stack, and the constraints that apply *to that child*. Never say "as discussed above".
2. **State the deliverable and how it will be judged.** Not "improve X" — "new file `<abs path>`, plus the
   command whose output proves it works".
3. **Write the verification-then-commit order into the task, explicitly**: *run the tests and the project
   gate, and only commit when it passes.* Without this, children write everything first and stop at the
   verifying step (the most common failure seen in practice). Give the pass criterion as a literal string
   to look for, and have them chain the gate to the commit so a red gate cannot be committed.
4. **Give the evidence, not the vibe.** File paths and `file:line` anchors of the reference implementation
   beat prose descriptions. If you hold a real transcript (a user's terminal session, a captured API
   response), paste it in — it outranks anything the child would infer.
5. **Forbid what it must not touch**: read-only reference trees, credential files, other people's
   in-flight edits. Name them.
6. **One deliverable per child, and no two children on the same files.** Overlapping write sets
   silently clobber each other's work.
7. **Write the iteration-cap fallback into the brief.** A child that runs out of iterations stops where it
   stands — mid-item, tree dirty, nothing committed. State the priority order explicitly: *if iterations
   run low, finish the FIRST item end to end (gate green, committed, pushed) and report the rest as
   undone rather than half-done.* Long multi-item briefs reliably return zero commits and several
   unfinished items without this clause; with it, every round still lands something on main.
   **Same trap, different shape, for "download sources + build a demo + write the docs" briefs:** the child spends
   every call fetching and debugging, and the deliverable that has no test to fail — the prose — is the one that never
   gets written. Observed: 3 of 3 children hit the iteration cap with materials and demos done and **not one lecture
   file on disk**. Order that triad explicitly: *fetch only the 3–5 pages the doc will actually cite, then write the doc
   and save it, then fetch more, then touch the demo.* Apply the same rule to the recovery round — a follow-up brief of
   "the materials are already local, write ONLY the doc" finishes in a fraction of the budget.
8. **Demand artifacts, not adjectives.** "Paste the key output lines and the test counts" produces
   checkable material; "confirm it works" produces a sentence.
9. **For a bug fix, demand the red-first pair.** Have the child reproduce the failure *before* touching
   the fix, and paste both texts: the pre-fix error line verbatim, then the post-fix result. "Fixed it,
   tests pass" cannot be checked; "before: `OperationalError: cannot start a transaction within a
   transaction` → after: 5 passed" is checkable and proves the test is capable of failing. Ask the same
   question of any test offered as proof: **has it ever been able to fail?** A guard that always skips,
   or a timing assertion finer than the platform clock, is a false green that no amount of reading the
   summary will reveal.
10. **When a sibling may be committing, gate the dispatch on a clean tree.** Two `git commit`s in one
   worktree race on the index. If the child's task touches a shared file (README, docs index, the
   generated symbol map) while another child is still running, write this as step zero: *poll
   `git status --porcelain` until it is empty, then touch nothing until you have re-checked `git log -1`;
   if it stays dirty for N minutes, stop and report that you did not start.* A child that begins on a
   dirty tree commits someone else's in-flight edits under its own subject. The cheap alternative —
   holding the dispatch until the other batch's completion message arrives — is usually better than a
   polling loop; use it when the dependency is that hard.

## While children run

- **Steer with the evidence they lack.** Steer text lands on the child's next tool result, so send
  authoritative facts (a real API response shape, a user's pasted transcript, a reversed premise) as soon
  as you get them — cheaper than a re-dispatch, and it prevents implementation-by-imagination.
  **When a child's demo keeps failing, stop reading its summary and go read the failing subsystem's own
  log yourself** — `docker compose logs <svc>`, the app's error log, `netstat`. Then send three things: the
  literal error line, the mechanism it proves, and the exact fix. A child retrying blind burns its whole
  budget on the same wrong layer (observed: the identical port conflict attempted twice, each time by
  editing a different wrong file). Three steers built this way — an occupied port owned by the user's own
  container; a `proxy_pass` using a variable that therefore needs a `resolver` directive; an auth strategy
  answering 407 where nginx only maps 401 — each fixed the child on its next turn, for the cost of one log
  read. Cheaper than a re-dispatch and better than a guess.
- **Two children with disjoint files still collide on a shared whole-tree gate.** One child writes
  `src/a.py` while another writes `src/b.py`; neither touches the other's file, yet each gate run sees the
  other's half-applied edits and reports reds neither caused. Observed twice in one batch: the second child's
  first gate run failed on the first child's in-flight module, and a third round's child had to run the gate in
  a `git worktree` of `HEAD` + its own diff to get a clean signal. If the project has one whole-tree gate, run
  **one** `src/`-touching child at a time; when you must overlap, brief the escape hatch explicitly — *copy
  `HEAD` to a sibling worktree, re-apply only your hunks, run the gate there, and before committing confirm
  the touched files are byte-identical to what you gated* — and tell the child **not** to "fix" a red that
  belongs to the sibling.
- **Do not report a dispatch as started before the tool call returned.** "已经开工" written from intent is
  indistinguishable from a lie when the call was never made — the user plans around it. Dispatch first, then
  describe; if the batch is queued rather than launched, say "queued behind X".
- **Don't poll.** Do whatever does not depend on the child; otherwise end the turn with a one-line status
  and let the result arrive as a message.
- **A child's subprocess notification is not the batch result.** One green gate line from a child's
  background process means only that one run passed.
- **A dispatch made while the pool is at capacity runs SYNCHRONOUSLY and blocks your turn.** With the
  concurrency cap already full, `delegate_task` executes the new tasks inline and returns them in the same
  call instead of handing you ids to collect later — so a "background" batch you expected to overlap with
  your own work ends up serialized behind it. Check `action='list'` for live children before dispatching
  if you needed them in the background, and don't stack a new batch on siblings that are still running.

## Verifying the return

Work down this list before repeating any claim:

| Claim | How to actually check |
|---|---|
| "committed" / "pushed" | `git log --oneline -N` and `git rev-list --count origin/main..HEAD` (0 = pushed) |
| "gate is green" | Re-read the summary line **verbatim**; confirm the skip count is 0, not just the failure count. A child that ran only `pytest` reports it as "gate green" — ask which stages printed `[OK]` |
| "file X created" | List it with a size; open it if the size looks thin |
| "tests pass" | Re-run the specific test files yourself — it is one command |
| "the source says …" | Spot-check 1–2 cited `file:line` anchors for real function/constant names |
| "nothing else changed" | `git status --porcelain` (uncommitted leftovers are extremely common) |
| "the doc describes the code correctly" | Extract every identifier the prose names (functions, classes, tables) and confirm each exists in the source — script it against a generated symbol list. Prose is the cheapest thing to fake and has no test to fail. **Expect the checker's first draft to drown you in false positives** — scope it before you trust a number: exclude pattern placeholders (`NNNN_*.sql`, `*.docx`), doc references carrying a section (`docs/03 §2`), **package-relative paths** (`state/db.py` → retry under `src/<pkg>/`), **extension-less test basenames** (`test_http`), brace lists (`tools/{a,b}.py`), and builtin exception names. A first run reported 71 "suspicious" for 8 files; after those six exclusions the same files had **0**. An un-scoped checker wastes a whole review round |
| "the UI change took effect" | Geometry, not class names or DOM nesting: assert **containment** for "inside" (`innerRect` inside all four edges of `outerRect`), **viewport intersection** for "visible" (`offsetParent`/`display` style checks produce "in the DOM, invisible in the screenshot"), and **the neighbouring panes' rects** for any grid/layout change (a probe that measured only its own element once reported success while the middle column was squeezed to 1px). Disable CSS transitions before measuring — headless does not advance them, so values sit at the animation's start. Read the baseline from the existing probe instead of copying numbers. Screenshots confirm a state; they do not prove one. **For a panel that must not resize with its content** (a settings/profile sheet whose size must stay put across categories), measure the panel's own rect in **every** category and assert it is field-equal (x/y/width/height), plus one small-viewport run that asserts it stays inside; when two panels are required to be "the same size", assert the two rect objects are equal rather than each against a constant. The mechanism that makes it hold is `flex: 0 0 auto` on the panel with the scrolling moved into the content area — `height: auto` or a flex child that grows is the usual reason a category switch makes it jump |

Two failure modes worth naming because both were observed:

- **A child that corrects YOUR premise** is a signal to trust it more, not less — a wrong assumption of
  yours (a file you believed existed) propagates to every child you brief. Because a confident parent
  sentence reads as an instruction, add the licence to the brief itself: *"if the code contradicts this
  diagnosis, follow the code, do not implement my guess, and quote the line that proved it."* Without that
  clause a child edits the file you named instead of the one that is wrong, and the wrong diagnosis is
  laundered into a second bug.
- **A child that "fixed" a failing test** must show its diff: changing a test's *fixtures* because the old
  data could not trigger the scenario is legitimate (and must be explained); relaxing an *assertion* is not.
  Ask for the before/after of that one hunk.

## Recovering a child that died mid-task

Do **not** re-run the whole task. Do **not** trust the partial summary.

0. **If the tree no longer imports, restore it before anything else.** An extraction or rename cut short leaves a
   call site pointing at a function that was never written, so the module raises `NameError` at import and every
   later check reports something unrelated. `git restore --source=HEAD -- <file>` the uncommitted half-done hunk,
   confirm it parses, then re-read the diff. Do not try to finish an unfinished extraction by guessing what it was
   meant to do.
1. `git log --oneline -N` + `git status --porcelain` — see what actually landed and what is lying uncommitted.
2. Read the summary for items it explicitly marks undone; those are the only work to re-dispatch.
3. Re-dispatch with the state spelled out: which files are already modified, which are untracked, what the
   last verified gate line was, and what remains. Children cannot see each other's transcripts.
4. Require the successor to *finish and commit*, not to restart.

## Finishing a child's work by hand

- **Edit existing files with the `patch` tool, never with ad-hoc scripts or heredocs.** A scripted
  replacement on CRLF files fails in two ways: it silently matches nothing, *or* it eats a line break and
  turns a valid file into a syntax error. An untracked file cannot be rescued with `git checkout`, so the
  damage must be repaired by hand from the original text.
- **A file that stays `M` after `git restore` is line endings, not content.** Under `core.autocrlf=true` a
  whole-file diff is CRLF↔LF. Compare `git diff --stat <file>` with `git diff --ignore-all-space --stat <file>`:
  if only the whitespace-ignoring form is empty, it is endings. Accept the formatter's normalization in a commit
  whose body says so; do not revert content to "fix" it, and do not change `.gitattributes` repo-wide in the same round.
- **Re-run the child's key test yourself** before accepting the batch; one command closes the gap.
- **Run the project gate with the project's tooling on PATH.** A gate script that shells out to `ruff` /
  `mypy` / `pytest` will report success with every stage **skipped** when they are not resolvable — a false
  green that reads like a pass. Equally, an exported `PYTHONPATH` pointing at `src/` can produce a false
  **red** in tests that inspect child-process environments. Read the skip count, not just the failure count.

- **Finish in one chain, in this order**: formatters → regenerate any *generated* artifact the gate
  checks (symbol map, lockfile, doc index) → run the gate and grep its literal pass string → add files by
  explicit path → commit with `-F` → push → print `git rev-list --count origin/main..HEAD` as proof.
  Regenerating a generated artifact **before** the formatters run leaves it stale the moment they rewrite
  a file, and the gate then reports the artifact as outdated — a whole round lost to ordering. The same trap
  fires when *you* patch a file after the child formatted it: format goes red on a file nobody touched since
  the last green run. Fix the order, not the content — fixer, formatter, generator, gate.
- **Product first, harness second.** When the *verification tooling* is what is broken (a probe that cannot
  find its own DOM anchor, a stub still emitting the old event shape), do not hold verified product work
  hostage to it: run the gate, commit the product and its tests by explicit path, push — *then* repair the
  probe. The harness usually lives in a gitignored run directory, so committing without it is clean, and the
  round still lands on main when the harness work exhausts its budget. Write that order into the brief; a
  child told to "finish the probe and commit" re-runs the same broken harness until its iterations are gone.
- **Evidence you cannot commit is not evidence a future reader can check.** Probe scripts, screenshots and
  recorded JSON live in a gitignored `run/` more often than not. Say so when you report ("this is local-only"),
  and make sure a **committed** test or guard carries the same claim — otherwise the reader the work was for
  (a thesis appendix, a reviewer, a later session) finds nothing to verify. When the user will cite numbers,
  tell them which ones are reproducible from the repo and which were observed once on this machine.

## When the deliverable is prose (docs / README / long-form notes)

A prose deliverable has no test to run, so the brief must carry the constraints a test would otherwise
enforce. Four items — a child briefed without them invents plausible content and the whole dispatch is
wasted:

1. **A calibration sample path.** Point at one already-good instance and tell the child to read it first
   and match its tone and granularity. Far more effective than describing the format in prose.
2. **The section template, verbatim.** The exact heading list, plus "do not invent structure".
3. **Fact discipline.** Every identifier must be one the child read itself; forbid rot-prone specifics
   (counts, line numbers, thresholds, default values) — those belong in a generated artifact or a linked
   authority; anything unverified goes into an explicit "uncertainties" list rather than a confident sentence.
   **Exception — when the deliverable IS the code map (源码带读讲义):** do not forbid `file:line`
   anchors; require each one grep-verified by the child at write time against the live tree, and require the
   explicit fallback (约 N 行附近，搜 X 可定位) instead of a guessed line number. Forbid only anchors the
   child did not read itself — an invented line number ends up in the user's interview notes, where it cannot
   be corrected by anyone.
4. **Hard boundaries.** Only create these files, touch nothing else, do not run gates, git, or network calls.

Then verify the artifacts, never the summary (see the table above). An "uncertainties" list doubles as a
cheap quality signal: an empty one from a child describing code it could not fully read is itself suspicious.

## Whole-tree gates, half-finished batches, and children that do not survive the session

- **A killed gate run is not a red gate.** A project gate that runs the full suite takes minutes; a terminal
default timeout SIGTERMs it and the log ends at `bash: no job control in this shell`. That is an **unknown**
result, neither red nor green. Re-run with a generous explicit timeout, or in the background with a
completion notification, and judge only on the literal summary line. Say the same to the child — one that
reads the kill as "red" starts rewriting code that was never broken.
- **Stash a half-finished batch outside the worktree instead of interleaving two batches.** Under a
whole-tree gate, two uncommitted batches block each other: whichever commits first inherits the other's
red. Move the unfinished batch's files into a project-internal stash (`_wip/<batch>-stash/`, **never
`%TEMP%`** — the OS clears it), record the file list, gate and commit the finished batch cleanly, then
**re-apply the stash's intent, never the stash's copies.** A stashed file is a snapshot taken *before* the
batches that landed in between, so copying it back silently reverts their edits to that file. On every
collision, re-read the current file and add only the hunks that batch was for, then prove it with
`git show --stat` — shared files must show pure additions, no deletions of the other batch's lines. Say this
in the brief as a hard prohibition ("do not restore the stash wholesale"), because a fresh child reading
`_wip/` sees a complete, plausible, stale file. Once the gate is green, land the queued batches **back to
back with no gate re-run between commits** — the tree is unchanged, so the green still holds; any edit in
between (including a fixer pass) invalidates it. Never run two `git commit`s concurrently.
- **Prove the committed state runs, not the working tree.** A green run proves nothing while the worktree
holds uncommitted edits. Copy HEAD to a sibling directory outside the repo (a `git worktree` or a plain
copy), run the suite there with the project's own interpreter, then delete the copy. It is the cheapest
way to catch "green only because of my local files".
- **Children do not survive your session.** Ending the session kills a running child mid-call: files it
already wrote stay on disk, but its commit does not happen. So (a) put the commit before the nice-to-haves
in the priority order, and (b) at the start of the next session verify with `git log` / `git status` before
re-dispatching — an "outcome unknown" entry means exactly this.
- **Never re-state a child's test count.** A child reporting "291 passed" is usually quoting a subset; the
real collection was 1537. Parse `--junitxml=<file>` (tests/failures/errors/skipped) or read the true
collect count, and cite that.
- **Adding a model-visible tool changes the tool table, and the tool table is part of the prompt.** Where
the project treats the system-prompt hash as an experimental condition, a "split this capability into more
tools" request silently invalidates every earlier report. Offer the hash-free alternative first (one tool
plus a runtime preference that selects providers/handlers), and defer tool-surface changes until after the
evaluation data exists.
- **A cross-process race is not fixed by a thread lock.** Two processes writing the same state file make
`os.replace` fail intermittently on Windows (EACCES while the other process holds a handle), and no
in-process lock crosses process boundaries. Make the writer unique by construction (the child reports over
RPC; the parent owns the file), add a short retry/backoff around `os.replace`, and prove it with a
counter-proof probe (without the change N records lost; with it 0).

## When the deliverable is a review (a findings list)

A review is a **claim list**, not a work order. Every line is unverified until a second pass opens
`file:line` and quotes the code:

1. **Re-check first, fix second.** Brief the successor with: *for each finding print the actual code or doc
   line; mark 成立 / 不成立 / 部分成立; do not trust the report.* Expect drift in both directions — a
   "14 unregistered methods" that recounts to 24, "49 long functions" that is 45, and a plausible mechanism
   asserted from a diff while the code reads the other way (an ordering bug whose write happens *before*
   the branch that supposedly swallows it).
2. **Fix only what the re-check confirms.** An unproven finding stays in the report as "存疑"; a fix built on
   a guess is later indistinguishable from a regression. Say in the report which findings you refused and why.
3. **Split the fixes by risk, one commit each**: rule/security violations (a secret echoed back, a config key
   that is silently inert) → behaviour-preserving hardening (a swallowed exception becomes a log line, the
   control flow unchanged and asserted) → docs-only alignment. Refactors and missing features are *reported*,
   never smuggled in; list them explicitly so "not done this round" cannot be misread as "missed".
4. **Recounted totals replace the old ones everywhere**, including the summary already given to the user. A
   number in a review is an assertion.
5. **Pin known defects instead of deleting the guard.** When a new gate surfaces real defects you are not
   fixing in this round (a tool returning a flat dict instead of the envelope, shell code writing raw error
   codes), write the assertion anyway and mark it `xfail(strict=True)` with the offending `file:line` in the
   reason. `strict` makes the case fail the day someone fixes it, forcing the marker's removal — so the
   defect stays visible, the gate still lands green, and the fix cannot be forgotten. Deleting or weakening
   the assertion instead is how a guard stops guarding.
6. **Classify a mechanical unification by contract before briefing it.** A grep for a repeated literal
   (`"ok": false`, `{ok, data}`) returns sites that only *look* alike: one repo's 34 hits were four separate
   contracts — a tool envelope, an RPC receipt carrying `code`/`message_id`, a shell bridge whose `error` is
   an object for the UI, and a sender receipt — plus two non-envelopes (an audit record, a model-facing
   projection). Forcing one constructor over all of them changes the JSON the frontend reads. So make the
   child emit a family table (`location | key set | family`) and migrate only the family the constructor was
   designed for; the counter-proof for "do not migrate" is to run the constructor on a sample and show the
   key set changes. **Zero sites migrated after such a proof is a correct result** — accept it and correct
   the number you gave the user.
7. **A prose rule with no gate has already drifted past what the report counted.** A documented "only
   `state/*` may touch SQL" had 12 sites in the first pass and 69 across 18 modules in a later audit, because
   the layering test only checked import direction. When a review finds a *class* of violation, the
   deliverable is the gate: land it with today's offenders as an explicit, **shrinking** whitelist and delete
   one entry per fix — otherwise the same cleanup is paid for again next quarter.
   **The gate's scan criterion then becomes the number.** The un-gated count was three different figures in
   three passes because nobody had defined what counts: a literal `\.execute(` grep misses code that goes
   through a facade (`db.query("SELECT …")`), and a per-file *set* whitelist lets a new site hide inside an
   already-listed file. Scan with `ast` (not regex — comments and strings produce false hits), count
   `{relative path: sites}` so a new site in a listed file still fails, add a zombie check (every whitelisted
   path exists and has >0 sites), and prove the gate can go red by adding one violation, watching it fail,
   then reverting. Report the gate's number and say which earlier number it replaces.

## Refactoring by extraction — the shape that survives a child

Splitting a god-function or a god-file is the task most likely to come back half-applied, because every
cut changes a call site the child has not read. Rules that held up across several rounds:

1. **Judge the target by the FILE, not the function.** Splitting a 450-line function into eight private
   methods *inside the same module* took the function to 300 lines and the file from 868 to **987** — the
   debt moved from "one function with six reasons to change" to "one file with the same six". Set the
   acceptance criterion to the rule itself (`<file> must end up < 600 lines`), have it measured with
   `wc -l`, and put the number in the brief. A round whose only metric was "the function got shorter"
   reports success while making the file worse.
2. **One extraction per dispatch — commit before opening the next cut.** Repeatedly, the *last* cut of a
   multi-cut round ended half-applied: functions moved out, import not added, tree raising `NameError`. A
   child with budget for three cuts attempts three. If a second seam is genuinely needed, brief it as
   "commit this cut, push, then open the next in a new round". Two cuts landing in one uncommitted blob is
   tolerable only if each commit point still runs — say which it is.
3. **Pick the seam by responsibility, and make the child justify it in the commit body.** "It is 240 lines
   and that gets us under 600" is not a seam. Good signs: the block is pure (no IO), it has one call site,
   or its dependencies are already explicit parameters.
4. **Let the line count veto your suggested cut.** A proposed "extract the text-conversion block" was
   correctly refused because it was 123 lines and would have left the source at 659; the child chose the
   243-line block instead, which is what actually crossed the threshold. Brief the seam as a *proposal*,
   not an instruction, and reward the counter-proposal.
5. **Re-export everything the module's importers use.** Tests import from the old module path; after the
   move they must keep working with **zero edits**, or the split is unverifiable. `from .new_module import
   (…)` plus a synced `__all__` is the whole compatibility story.
6. **Grep the test tree for `monkeypatch.setattr(<module>` before moving a factory.** A test that patches
   `module.send_file` binds to that module's global; move the factory that reads the global and the stub
   silently stops applying — the test still passes, now against the real implementation. This is a false
   green, not a failure, so nothing will alert you.
7. **Prove a pure move with hashes, not with green tests.** Have the child slice every moved and remaining
   definition with `ast`, hash the source segments, and compare against `HEAD`. "21 moved + 12 remaining
   objects byte-identical to HEAD" is evidence; "tests pass" is a weaker claim that a behaviour change can
   also satisfy.
8. **No second cut when there is no second seam.** If the file is still over the limit and everything left
   is one responsibility, keep it and say so — a split that does not follow the real boundary should not be
   made. Padding a split to hit a number produces two mediocre modules and a new import graph.
9. **Recovering a half-applied cut is usually one import, not a revert.** When the moved functions are all
   present in the new module and only the old module's import line is missing, mypy reports
   `Name "x" is not defined` for exactly those names. Parse them, keep only the ones that exist in the new
   module, add the import, `ruff check --fix` to drop the imports that became unused, then run the gate.
   Restore from `HEAD` only when the definitions themselves are absent — that is the case where finishing
   by guessing is worse than rolling back one cut.

## Pitfalls

- **"Fixed" without a re-run is not fixed.** A child that edits a test or guard to clear a red and reports it
  green must paste *that one test's* output; a directory-level run hides it (the file may not be collected,
  or the case sits among hundreds). When you inherit such a fix, run the single test before anything else —
  and when the fix was to an anchor/slice helper, check the helper's own boundary order, not just the anchor
  text (a start anchor placed after its end anchor makes the slice swallow thousands of characters or fail
  outright, so the guard silently stops guarding).

- **Localize a red gate from the stages that are green.** A truncated gate log still tells you where the
  failure is *not*: if the tail shows `tests` / `rules-sync` / `deps-approval` / `ui-assets` / `code-map`
  all `[OK]`, the red is in the stages that run **first** (lint, format) — a one-minute fix — and the child
  must not go hunting through the test suite or the generator. Send that deduction as the steer, with the
  exact command order, instead of letting it infer from a partial log.
- **Deleting a shared symbol needs a zero-reference guard.** When a child removes a global, a CLI flag, or
  a config key, "the tests pass" proves nothing about the *other* call sites — the dangling reference shows
  up as a runtime `AttributeError` on an unrelated code path. Require the refactor to also land a guard that
  asserts the deleted name now has **zero** references (a source grep in the test, or `assert "X" not in
  vars(module)` for an import-level ban). That guard is what catches the half-done refactor, and it doubles
  as the spec for the next child.
- **Accepting the summary as the deliverable.** Every "done" above costs one command to confirm.
- **Briefing from memory.** A path or flag you half-remember propagates into files; verify the reference
  before writing it into a task.
- **Chaining one child's output into the next brief without checking it.** A single over-generalized
  sentence from child #1 ("editing the prompts changes the system-prompt hash") reads like an established
  fact to child #2, who is then told to *document* it. Child #2 did the right thing — it went to the code,
  found the hash comes from a Python module that never reads that prompt directory, and reported the
  correction. Trace every inherited sentence to its source file before reusing it as briefing material;
  the parent is the one who amplifies a child's paraphrase into "the project's documented behavior".
- **Skipping the commit instruction.** "Do the work" yields work-in-progress in the tree; "run the gate,
  then commit if it passes" yields history you can build on.
- **Letting a child invent internals.** When a child reports function or endpoint names in a codebase it
  could not fully read, require `unknown` over a plausible guess — and check a sample.
- **Re-dispatching instead of inspecting.** Re-running a half-finished batch duplicates work and can revert
  the part that succeeded.
- **A `git diff`-based preservation snapshot silently drops every new file.** Untracked files do not appear
  in `git diff`, so a rescue patch built from the diff restores the edits and loses the new modules the
  child created. Capture both the diff *and* the list of untracked files (or copy them), and **re-take the
  snapshot after every hand-fix** — the tree moves as you patch it, so an earlier capture goes stale.
- **Create the commit-message file unconditionally.** `git commit -F <file>` sidesteps quoting bugs, but if
  the file is written *inside* the `if gate-passed` branch it never exists when the gate fails, and the
  next append promotes a line of that text to the commit subject. Create it before the branch; a bad
  subject is fixed with `--amend` (safe to `--force-with-lease` on a single-user repo), never by rewriting
  history a fetch may have seen.
- **An unfamiliar gate stage is not your bug.** A gate can grow stages between rounds — another child adds
  one, or the project adds one — and a stage you have never seen going red on a structural change is
  usually a stale generated artifact. Read the remediation command the stage prints (often "regenerate
  X") and run that before touching the code you just wrote.
- **Sort a child's leftover reds into mechanical buckets before re-dispatching.** A child that ends
  "functionality done, three gate stages red" has usually left only classification work. Name each failure
  with its file and category: lint (import order, `int(round(x))`, quoted annotations — often `--fix` plus
  one manual hunk), types (a conversion applied to an `object` parameter — narrow with `isinstance`,
  excluding `bool`, never `type: ignore`), and a test whose `wait()` returns before the side effect (poll
  with a timeout, keep the assertion as strict). A successor brief listing the per-bucket fix finishes in
  one round; "make the gate green" makes it rediscover the same items.
- **A pre-existing red gets its own commit, labelled `pre-existing`.** A failing test that predates the
  batch still blocks a whole-tree gate, so it has to be fixed — but folded into the feature commit it
  falsifies history about what introduced the regression and what a revert would undo. Separate commit,
  and state in the body that this change did not introduce it.
- **A timing flake is proven fixed by a counter-proof, not by repetition.** "Ran 30 times, all green"
  cannot distinguish a real fix from luck. Inject the delay the old assertion assumed away (a few ms
  before the non-last thread is woken at a barrier): the old criterion must go red under it and the new one
  green, with the product's own counters unchanged. Classify the cause first — a barrier that synchronises
  only the first round leaves the wrap-up round to scheduler luck, so the product was right and the
  assertion was too strict; report which of the two it was instead of just "defuzzed".
- **The child's own background process can outlive its turn.** When the child dispatches the gate in the
  background, the turn can end before it reads the result, so its summary says "gate never ran" while the
  process in fact finished green. The notification reaches you: treat it as evidence the same as any other,
  and re-run the gate yourself only if it is ambiguous.
- **A structural check is a snapshot, and "missing" is usually your own path bug.** Re-run your audit
  immediately before you report, never once at the start of a batch: children write files while you are
  checking, so a gap you found ten minutes ago is often already closed, and reporting it as a defect
  wastes the user's attention. And when a check does say something is absent, first confirm the name you
  checked is the name on disk — an audit that looks for a directory named after the display label (a
  Chinese stack name) while the directory actually uses the repo-style short ASCII name will report
  "materials directory missing" for a directory that is right there. Same for a per-item deliverable whose
  file was renamed between the brief and the write.
- **Self-reports err in both directions.** Besides over-claiming, a child says "nothing committed / the gate
  never ran" when in fact the commit had landed before its cap or its own background process finished
  green. Check the repository and the process notification, not the sentence: `git log --oneline -1`,
  `git rev-list --count origin/main..HEAD`, `git status --porcelain`.
- **When a guard or test goes red, classify before editing**: (a) stale *verification tooling* (a probe
  still emitting the old event shape, a recorded fixture outliving the contract it was recorded under),
  (b) a stale *expectation* in a test baseline, (c) a real product bug. Fix (a) in the tooling and (b) in
  the baseline — never by widening or deleting the assertion — and state which of the three it was. A
  child that turns a guard green by relaxing it has removed the only thing that would catch the next
  regression, and it will not mention that it did so.
- **Concurrent writers: commit by explicit path.** When other sessions or children share the worktree,
  `git add -A` sweeps their half-finished work into your commit. Add only the paths you touched, run
  `git status --porcelain` before committing to confirm nothing foreign is staged, and re-read any file a
  sibling just modified rather than editing from your memory of it.
- **The finishing chain fails silently when you type a path from memory.** Guessing a Chinese filename
  (`00-检索层.md` for the real `00-总索引.md`) or wrapping `git add` with `|| true` turns "matched nothing"
  into "command succeeded", and the next `git commit` lands an empty or wrong staging set while every line
  of output looked fine. Take paths from `git status --porcelain` / `ls`, use `git add -u -- <dir>` when the
  intent is "every tracked modification under this directory" (untracked drafts stay out by construction),
  and test the staging set (`git diff --cached --quiet`) before committing.
