#!/usr/bin/env python3
"""Audit a git repo for secrets, personal files and machine-specific paths.

Usage:
    python audit_repo_secrets.py [repo_path]      # defaults to cwd

Scans TRACKED files only (untracked files are never published) plus git history.
Deliberately uses Python for all reading/printing: git-bash garbles non-ASCII
output, so a bash grep of CJK filenames or matched lines is unreadable.

Exit code is always 0 - this is a report, not a gate.
"""

import os
import re
import subprocess
import sys

PATTERNS = [
    ("password-literal", re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*[\"'][^\"'\s]{4,}[\"']")),
    ("api-key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("api-key-assign", re.compile(r"(?i)(api_key|apikey|secret|token)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']")),
    ("db-url", re.compile(r"(?i)(mysql|postgres(ql)?|mongodb)(\+\w+)?://[^\s\"']+")),
    ("private-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("phone", re.compile(r"\b1[3-9]\d{9}\b")),
    ("email", re.compile(r"[\w.\-]+@[\w\-]+\.[a-z]{2,}", re.I)),
    ("student-or-id", re.compile(r"\b[A-Z]\d{8}\b")),
    ("abs-path", re.compile(r"(?i)[a-z]:[\\/]{1,2}(users|home)|[a-z]:[\\/]")),
]

# Directory names a "categorize my documents" helper commonly dumps unmatched
# files into. Personal files (resume text, notes, jokes) hide here and get pushed.
FALLBACK_DIRS = ("\u5176\u4ed6", "misc", "other", "tmp", "temp")


def run(args, cwd):
    p = subprocess.run(args, capture_output=True, cwd=cwd)
    return p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def main():
    repo = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    out, err = run(["git", "rev-parse", "--is-inside-work-tree"], repo)
    if out.strip() != "true":
        print("NOT A GIT REPO:", repo, err.strip())
        return
    os.chdir(repo)

    out, _ = run(["git", "ls-files"], repo)
    tracked = [l for l in out.splitlines() if l.strip()]
    print("repo:          %s" % repo)
    print("tracked files: %d" % len(tracked))

    hits = {}
    for f in tracked:
        if not os.path.isfile(f):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    for name, pat in PATTERNS:
                        if pat.search(line):
                            hits.setdefault(name, []).append((f, i, line.strip()[:160]))
        except OSError as e:
            print("READ_ERR %s %s" % (f, e))

    print("\n=== HEAD: sensitive hits in tracked files ===")
    if not hits:
        print("  (none)")
    for name, lst in hits.items():
        print("\n--- [%s] %d hit(s) ---" % (name, len(lst)))
        for f, i, line in lst[:25]:
            print("  %s:%d  %s" % (f, i, line))
        if len(lst) > 25:
            print("  ... %d more" % (len(lst) - 25))

    print("\n=== Personal-file sweep (fallback dirs: %s) ===" % ", ".join(FALLBACK_DIRS))
    flagged = [p for p in tracked if any(d in p.replace("\\", "/").split("/") for d in FALLBACK_DIRS)]
    for p in flagged:
        print("  ", p)
    if not flagged:
        print("  (none)")

    print("\n=== git history ===")
    out, _ = run(["git", "rev-list", "--all", "--count"], repo)
    print("  total commits:", out.strip())

    out, _ = run(["git", "log", "--all", "--diff-filter=D", "--name-only", "--pretty=format:"], repo)
    deleted = sorted({l.strip() for l in out.splitlines() if l.strip()})
    print("  files deleted from HEAD but STILL IN HISTORY (%d) - these are published:" % len(deleted))
    for d in deleted:
        print("    ", d)
    if not deleted:
        print("     (none)")

    print("\n  To test a specific secret across all history:")
    print("    git log --all --oneline -S '<secret>'")
    print("  Expect no output after scrubbing.")

    print("\n=== git status / remote ===")
    out, _ = run(["git", "status", "--short"], repo)
    print(out.strip() or "  (clean)")
    out, _ = run(["git", "remote", "-v"], repo)
    print(out.strip() or "  (no remote)")

    print("\nReminder: scrubbing history is remediation, not defence -")
    print("tell the user to ROTATE the leaked credential everywhere it may be reused.")


if __name__ == "__main__":
    main()
