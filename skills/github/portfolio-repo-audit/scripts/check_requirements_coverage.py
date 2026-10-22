#!/usr/bin/env python3
"""Report third-party modules imported by a repo's own code that are NOT declared
in its requirements file(s).

Usage:
    python check_requirements_coverage.py [repo_dir] [req_file ...]

Exits 1 when something is undeclared, so it can gate a "repo is ready" claim.

WHY the pruning matters: a naive os.walk() over a repo with a checked-out
.venv/ or node_modules/ descends into tens of thousands of dependency files and
reports hundreds of bogus "missing" modules (numpy, pytest, scipy, ...). The
SKIP_DIRS prune below is what makes the result trustworthy, not optional.
"""
import ast
import os
import sys

# Directories that hold dependencies or vendored code, not the project's sources.
SKIP_DIRS = {
    '.git', '.venv', 'venv', 'env', '.env', 'node_modules', 'site-packages',
    '__pycache__', '.tox', '.nox', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'build', 'dist', '.eggs', 'vendor', 'third_party', 'thirdparty',
    'models', 'index', 'data', 'datasets', 'outputs', 'logs',
}

# import name -> PyPI distribution name, for the cases that are not 1:1.
ALIASES = {
    'PIL': 'pillow',
    'win32com': 'pywin32', 'win32api': 'pywin32', 'win32con': 'pywin32',
    'pythoncom': 'pywin32', 'win32pdh': 'pywin32',
    'docx': 'python-docx', 'pptx': 'python-pptx',
    'sklearn': 'scikit-learn', 'cv2': 'opencv-python',
    'yaml': 'pyyaml', 'dotenv': 'python-dotenv', 'bs4': 'beautifulsoup4',
    'fitz': 'pymupdf', 'Crypto': 'pycryptodome', 'jwt': 'pyjwt',
    'dateutil': 'python-dateutil', 'Levenshtein': 'python-levenshtein',
    'MySQLdb': 'mysqlclient', 'mysql': 'mysqlclient',
    'langchain_text_splitters': 'langchain-text-splitters',
    'sentence_transformers': 'sentence-transformers',
    'google': 'google-api-python-client',
    'serial': 'pyserial', 'usb': 'pyusb',
}

REQ_NAMES = ('requirements.txt', 'requirements-dev.txt', 'requirements-prod.txt',
             'pyproject.toml', 'setup.py', 'setup.cfg', 'environment.yml')


def collect_imports(root):
    """Map top-level imported module name -> set of files that import it."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith('.')]
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            path = os.path.join(dirpath, fn)
            try:
                tree = ast.parse(open(path, encoding='utf-8', errors='ignore').read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found.setdefault(alias.name.split('.')[0], set()).add(fn)
                elif isinstance(node, ast.ImportFrom):
                    # level > 0 means a relative import -> local by definition
                    if node.module and node.level == 0:
                        found.setdefault(node.module.split('.')[0], set()).add(fn)
    return found


def declared(req_files):
    """Normalized set of declared distribution names."""
    names = set()
    for rf in req_files:
        if not os.path.isfile(rf):
            continue
        for raw in open(rf, encoding='utf-8', errors='ignore'):
            line = raw.split('#')[0].strip()
            if not line or line.startswith('-'):
                continue
            line = line.split('[')[0]          # drop extras
            for sep in ('===', '==', '>=', '<=', '~=', '!=', '>', '<', ';'):
                if sep in line:
                    line = line.split(sep)[0]
            names.add(line.strip().lower().replace('_', '-'))
    return names


def main(argv):
    root = argv[0] if argv and os.path.isdir(argv[0]) else '.'
    req_files = [a for a in argv if os.path.isfile(a)]
    if not req_files:
        req_files = [os.path.join(root, n) for n in REQ_NAMES
                     if os.path.isfile(os.path.join(root, n))]

    stdlib = set(getattr(sys, 'stdlib_module_names', ()))
    local = {fn[:-3] for _, _, fs in os.walk(root) for fn in fs if fn.endswith('.py')}

    deps = declared(req_files)
    imports = collect_imports(root)
    third = {k: v for k, v in imports.items() if k not in stdlib and k not in local}

    print(f"repo      : {os.path.abspath(root)}")
    print(f"declared  : {', '.join(os.path.basename(r) for r in req_files) or '(none found)'}")
    print(f"scanned   : {len(third)} third-party imports in project sources\n")

    missing = []
    for name in sorted(third):
        pkg = ALIASES.get(name, name).lower().replace('_', '-')
        ok = pkg in deps
        if not ok:
            missing.append((name, pkg, sorted(third[name])))
        print(f"  {'OK  ' if ok else 'MISS'}  {name:30} -> {pkg:30} {len(third[name])} file(s)")

    if missing:
        print(f"\n{len(missing)} undeclared import(s):")
        for name, pkg, files in missing:
            shown = ', '.join(files[:4]) + ('...' if len(files) > 4 else '')
            print(f"  {name}  (declare as '{pkg}', used in {shown})")
        print("\nAdd the missing distributions, then re-run until the MISS column is empty.")
        return 1

    print("\nAll third-party imports are declared. A clean clone can install and run.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
