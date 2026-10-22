---
name: repo-secret-scrubbing
description: "Use when a repo goes public: scrub its secrets and history."
version: 1.0.0
tags: [git, secrets, privacy, filter-repo, github, history-rewrite]
---

# Repo Secret Scrubbing

Before a repo becomes public — or before its URL goes on a resume, portfolio or application — audit it for secrets,
personal files and machine-specific paths, then scrub the working tree **and the history**. The reader will open the
repo and read `git log`; a repo that still leaks a password in an old commit is worse than no link at all.

## Phase 0 — Agree the scope before touching anything

State the plan first: which files/dirs go, whether history gets rewritten (it changes every commit hash and needs a
force push), and what the backup is. Get explicit confirmation for anything destructive. Never run a destructive git
command and explain afterwards.

## Phase 1 — Audit (working tree + history)

1. **Audit tracked files only.** Untracked files are not published:
   ```bash
   git ls-files -z | xargs -0 grep -ln '<pattern>'
   ```
2. **Do the scanning in Python, not bash grep.** git-bash garbles non-ASCII output, so CJK filenames and matched lines
   come back unreadable and you cannot tell what you found. Run the ready-made sweep in this skill instead of
   hand-rolling greps: `python scripts/audit_repo_secrets.py [repo_path]`.
3. **Patterns that matter**: password/passwd/pwd literals, `sk-…` API keys, `mysql://` connection strings,
   `BEGIN … PRIVATE KEY`, phone numbers, emails, student/staff IDs, real names, local absolute paths (`C:\…`, `E:/…`,
   the machine username).
4. **Sweep for personal files, not only secrets.** Resume text, ID scans, personal notes and joke/placeholder files
   routinely land in a catch-all directory (commonly `其他/`, `misc/`, `tmp/`) created by a "categorize my documents"
   helper. Grep the tracked list for the categorizer's fallback directory name.
5. **History is the part everyone forgets.** Two commands cover it:
   ```bash
   git log --all --oneline -S '<secret>'                       # commits that ever contained it
   git log --all --diff-filter=D --name-only --pretty=format:  # files deleted from HEAD but still in history
   ```
   A file removed from HEAD is still published in history — the `--diff-filter=D` list is what catches the file you
   "already deleted".
6. **Confirm what is already safe.** Check the real `config.py`/`.env` is untracked, `.gitignore` covers it, and the
   repo ships a scrubbed `config.example.py` with empty values. Say so explicitly — it is the one part that is right,
   and it keeps you from over-scrubbing.

## Phase 2 — Back up before rewriting (non-negotiable)

```bash
git bundle create "<outside-the-repo>/<name>-history-<YYYYMMDD>.bundle" --all
git bundle verify "<...>.bundle"     # must print: The bundle records a complete history
```

A bundle is a complete clonable backup: `git clone <file>.bundle <dir>` restores everything. Also `cp` personal files
out of the worktree before deleting them, so the deletion is reversible.

## Phase 3 — Clean HEAD

- Replace the secret literal in docs/changelogs but **keep the functional record**: a changelog line saying "password
  protection added" is legitimate project history; only the literal goes. Never delete the entry to hide a secret.
- Prefer `git rm -r --cached <dir>` (file stays on disk) or copy the files out first, then `git rm -r`. A plain
  recursive delete with no copy is the one irreversible step in the whole workflow.
- Add the offending directory to `.gitignore` **and fix the script that generates it**, or it returns on the next run.
- Relativeize hardcoded machine paths. In Python this needs no new import — `os` is already imported:
  ```python
  PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root, for a script in scripts/
  DOCS_DIR = os.path.join(PROJECT_ROOT, "docs")
  ```
  Verify with `py_compile` on every edited file, then assert `PROJECT_ROOT` resolves to the directory containing
  `README.md`. Paths pointing OUTSIDE the repo (the user's own source-material folders) cannot be relative — leave them
  and mark them `# ← 按你的实际路径修改` rather than inventing a portable form.

## Phase 4 — Rewrite history

`git filter-repo` is the tool (plain `filter-branch` is deprecated and slow). Run it as **two passes** — mixing path
filtering and content filtering in one invocation is fragile:

```bash
git filter-repo --force --invert-paths --paths-from-file <paths.txt>
git filter-repo --force --replace-text       <replace.txt>
git remote add origin <url>     # filter-repo DELETES the origin remote; re-add it or the push has no target
git push --force origin <branch>
```

Spec-file formats, one entry per line:
- `paths.txt`: `literal:<path relative to repo root>`
- `replace.txt`: `<old>==>(redacted)`

Use spec files with `literal:` prefixes rather than paths as CLI arguments — it sidesteps shell quoting entirely, which
is the difference between working and not for CJK filenames, full-width punctuation and spaces.

State these expectations to the user before running:
- Every commit hash changes; commit count, order and messages survive, so the "I iterated" evidence stays.
- `--force` is required on a repo that is not a fresh clone.
- Write the spec files somewhere durable next to the backup, not in `$TEMP` — the user may run the commands later.

## Phase 5 — Verify from the REMOTE, not locally

A locally clean repo proves nothing. Clone fresh and grep the clone.

**Enumerate objects, not changed paths.** `git log --name-only` lists only the paths *changed* by each commit, so a
file that sits in a tree without being touched by that commit never shows up — it silently misses exactly the files
you are hunting. Sweep every reachable path, and grep content at every commit:

```bash
git clone <url> <scratch-dir> && cd <scratch-dir>
git rev-list --all --objects | grep -E '<personal-file-pattern>'   # every reachable path in all of history
git grep -I '<secret>' $(git rev-list --all)                       # file content at every commit
git rev-list --all --count                                        # expect the same count as before the rewrite
```

Also confirm the removed directory is absent from the GitHub file tree in a browser.

## Phase 5b — The server still has it (a clean clone does NOT mean clean)

**This is the step everyone skips, and a clone cannot detect it.** `git push --force` moves the branch pointer; it
does not delete objects. GitHub keeps pre-rewrite commits as *unreachable* objects that stay retrievable **by their
old SHA** until it happens to run GC — timing is unspecified (weeks, months, or never). So the fresh clone in Phase 5
passes while the password is still one `curl` away. Never report "cleaned" on clone evidence alone.

Get the old SHAs from the pre-rewrite bundle (they no longer exist in the rewritten repo):

```bash
git clone --no-checkout <backup>.bundle /tmp/old && git -C /tmp/old log --all --oneline
```

Then probe the server, and read the content back to be sure:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://api.github.com/repos/<o>/<r>/commits/<old_sha>
# 200 = STILL EXPOSED   404/422 = gone   409 = repo has no commits at all (fresh/empty)
```

A 200 is not conclusive on its own — an SHA can resolve while the specific file you care about is absent from that
tree. Fetch the tree, then the blob, and decode it (base64, so CJK paths and full-width punctuation need no URL
encoding):

```bash
curl -s https://api.github.com/repos/<o>/<r>/commits/<old_sha> \
  | python -c "import sys,json;print(json.load(sys.stdin)['commit']['tree']['sha'])"
curl -s "https://api.github.com/repos/<o>/<r>/git/trees/<tree_sha>?recursive=1" \
  | python -c "import sys,json;[print(t['path'],t['sha']) for t in json.load(sys.stdin)['tree']]"
curl -s https://api.github.com/repos/<o>/<r>/git/blobs/<blob_sha> \
  | python -c "import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)['content']).decode())"
```

Two ways to actually purge, in order of preference for a personal project:

| Option | How | Trade-off |
|---|---|---|
| **Delete + recreate the repo** | Settings → Danger Zone → delete, then create a same-named empty repo (do **not** tick README/.gitignore/license) and `git push -u origin <branch>` from the already-clean local clone | Destroys the old object store instantly; URL unchanged; commit dates and authors survive so the contribution graph is unaffected. Safe **only if `forks_count == 0`** — check it first, and confirm the local clone plus the bundle are intact before deleting. GitHub's delete is a soft delete; tell the user not to use "restore". |
| **Ask GitHub Support to GC** | support.github.com/request → "Remove data" | No data loss, takes days, not guaranteed |

Report this as **still exposed**, not as pending. Until the credential is rotated, rotation remains the only complete
fix — say so in the same breath.

## Phase 6 — Rotate the credential (do not skip)

**Scrubbing history is remediation, not defence.** A password that sat in a public repo for weeks is compromised from
the moment it was pushed, and force-pushing does not recall forks, caches or crawlers. Tell the user this first and
plainly, and list the places it may be reused.

Note the *shape* of leaked passwords: they are usually derivable (`name-initials` + `DDMMYYYY` birthdate, `admin123`,
`123456`). When the leak is derivable, say so — it turns "change one password" into "audit your accounts".

## Pitfalls

- **Never batch two edits to the same file in one turn.** Mechanical multi-file work (e.g. relativeizing 15 paths across
  8 scripts) tempts one patch call per file in a single message; every call in that batch is evaluated against the same
  pre-edit snapshot, so two calls on the same file leave the second unmatched or clobbering the first. Batch across
  *different* files; serialize same-file edits into separate turns or merge them into one hunk.
- **Destructive commands are gated by an approval prompt.** If it times out the command is blocked and you are told not
  to retry. Do not retry in a loop. Switch to the handoff: complete every non-destructive step (backup, copy files out,
  write spec files, relativeize paths), write a copy-paste command sheet to a file, and give it to the user. This user
  prefers to run destructive commands themselves anyway.
- **`git rm -r` without a copy is irreversible.** Copy first, then delete — or use `--cached`.
- **Say "pending", not "done".** If the rewrite was handed off rather than executed, report exact state — what is
  committed, what is not, what still has to run — instead of implying the repo is clean.
- **Don't audit untracked files and conclude the repo is dirty**, or scrub the whole worktree. Published surface =
  tracked files + history, nothing else.

## Support files

- `scripts/audit_repo_secrets.py` — run this first; full tracked-file + history sweep, CJK-safe output.
