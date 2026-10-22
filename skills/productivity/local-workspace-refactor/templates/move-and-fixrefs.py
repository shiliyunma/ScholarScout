# -*- coding: utf-8 -*-
"""移动 + 修引用 + 校验 的脚本骨架（复制后按需改）。

用法：改 ROOT / MOVES / REFS，先跑一遍看打印，再确认写入。
铁律：每处替换 assert 命中；REFS 顺序「先长后短」；改前先备份。
"""
import io, os, re, shutil, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = r"D:\工作目录"                 # 上层目录
D    = os.path.join(ROOT, "模块")      # 被整理的目录

# 1) 目标子目录（用户要求：分类进文件夹，台账类留根）
for p in (
    os.path.join(D, "1-准备"),
    os.path.join(D, "2-计划"),
    os.path.join(D, "3-素材"),
):
    os.makedirs(p, exist_ok=True)

# 2) 移动清单（源名, 目标目录）——目标目录不存在时要显式建，避免 move 嵌套
MOVES = [
    # ("文件A.md", os.path.join(D, "1-准备")),
]
for name, dst in MOVES:
    src, tgt = os.path.join(D, name), os.path.join(dst, name)
    if not os.path.exists(src):
        print("  跳过（不存在）:", name); continue
    if os.path.exists(tgt):
        print("  跳过（目标已存在）:", name); continue
    shutil.move(src, tgt); print("  移动:", name)

# 3) 修引用：先长后短！替换串里的反斜杠要转义给 re.sub
REFS = [
    # (r"长路径\\子目录\\文件.md", r"长路径\\新子目录\\文件.md"),
    # (r"文件.md",             r"1-准备\\文件.md"),
]
changed = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in ("_归档", ".git")]
    for fn in filenames:
        if not fn.endswith((".md", ".py", ".json")):
            continue
        fp = os.path.join(dirpath, fn)
        try:
            txt = io.open(fp, encoding="utf-8").read()
        except Exception:
            continue
        new = txt
        for pat, rep in REFS:
            new = re.sub(pat, rep.replace("\\", "\\\\"), new)
        if new != txt:
            io.open(fp, "w", encoding="utf-8").write(new)
            changed.append(os.path.relpath(fp, ROOT))
print("\n修引用的文件:")
for c in changed:
    print("  ", c)

# 4) 校验
for dp, dn, fn in os.walk(D):
    lvl = dp.replace(D, "").count(os.sep)
    print("  " * lvl + os.path.basename(dp) + "/")
    for f in sorted(fn):
        print("  " * (lvl + 1) + f)
# 表格没被截断：
# grep -c "^| " <文件>
