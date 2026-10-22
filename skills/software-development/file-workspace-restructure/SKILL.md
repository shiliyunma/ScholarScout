---
name: file-workspace-restructure
description: "Use when moving or renaming many files in a workspace."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [filesystem, refactor, windows, verification]
    category: software-development
---

# Restructuring a file workspace

Reorganising a directory tree that other files point into — document vaults, project folders, a
campaign workspace. The moves are trivial; the breakage lives in the references.

## When to Use

- "把这些目录重新分成三个模块" / "把这个目录整理一下" / "目录太乱了"
- renaming or moving top-level folders that `.md` files, scripts or configs refer to
- consolidating date-stamped dump folders (`9月15日整理\`, `2026-09-16\`) into a scheme

## Procedure

1. **Inventory before touching anything.** Recursive listing of every candidate dir, sizes, and a total
   file count (`find . -type f | wc -l`). Confirm duplicates by **content, not filename**
   (`md5sum a b`) — a stray root copy of a generated PDF was byte-identical to the one inside the
   project, so deleting the root copy cost nothing and removing the wrong one would have.
2. **Map every reference before renaming.** `grep -rn -e OLD1 -e OLD2 --include=*.md --include=*.py .`
   Read each hit and classify it: path reference (will break) vs. ordinary prose (must not be touched).
3. **Move with a same-volume rename** (`mv` / `os.rename`) — instant, reversible, no copy. Prefer this
   over copy+delete. Never delete user material: park strays, ambiguous files and verified duplicates
   in `_归档/` (archive).

   Write the moves as a **re-runnable script**, not a chain of shell one-liners: skip entries whose
   source is gone or whose destination already exists, and retry a refused rename a few times with a
   short backoff. A bulk move that dies half way (see the handle pitfall below) is then resumed simply
   by re-running it — no bookkeeping, no double-moves. Run it from a CWD *outside* the workspace; a
   shell or interpreter sitting in the workspace holds a handle that can block its children.
4. **Write the mapping down** as `*重构记录*.md` at the workspace root: old→new table, the reference
   fixes, and a rollback command block. This reversible record is what lets a later session undo the
   move — and it is the artifact that makes "可以" safe to say. Ship a **navigation index** at the root
   alongside it (`00-总索引.md` or similar): the new module names, one line on what each holds, the
   files the user reaches for daily, and the current state of any live tracker in the tree. The move
   record answers "what changed"; the index answers "where does this go next time" — and the index is
   the half the user actually opens.
5. **Fix references with path-shaped rules only.** Replace `OLD\` → `NEW\`, `OLD/` → `NEW/`, and
   drive-absolute forms `D:/x/OLD` → `D:/x/NEW`. The trailing separator is the whole trick — a
   bare-word replace corrupts prose that happens to contain the folder name (e.g. 「面试准备一个案例」).
6. **Verify by resolving, not by eye.** Re-grep for the old names and expect **zero** hits; extract each
   absolute path from scripts and assert the target exists; count files again and reconcile against
   step 1 (`before − deleted + created = after`). Report the counts — that arithmetic is the acceptance
   check, not a vibe.

## 当路径同时存在数据库里（文件托管、资料库、附件表）

情形不同：磁盘可以慢慢搬，但库里的路径字段一旦与盘不一致，应用就找不到文件。按这个顺序拆开做：

1. **让读路径一律走库里的路径字段**（`raw_path` / `text_path` 之类），不在代码里重新拼路径。
   这样磁盘还停在旧布局时系统照常可用 —— **代码与数据就能分成两次独立验证**，不必一步到位。
2. **先交代码**（命名规则、布局参数化、索引生成、迁移脚本），门禁绿再入库；**不要在同一轮里顺手搬数据**。
3. **再单独跑迁移**：`--dry-run` 先打“旧→新”清单（不动盘）→ **先备份库**（库不入 git，改盘前必须有退路）→
   正式跑时**先 rename 文件、再同一事务更新库路径**，库写失败就把文件改回去并报告 → 迁完**清空并删除空的旧目录**（只删空的）→
   结束打四个计数：**改名 / 跳过 / 冲突 / 失败**。
4. **幂等要自证**：重跑必须变成“全部跳过”，这是唯一能证明“不会搬两次”的方式。
   冲突（目标已存在且内容指纹不同）追加 `--2` 并报告，**不静默覆盖**。
5. **布局参数化，别把目录段序写死**：把“目录怎么切”做成配置（默认值写在代码里），生产路径与迁移脚本都从它读；
   未知段**直接报错**，不要静默写到错位置。这样用户下次改排法只改配置，不用第三次重写。
6. **人类可读命名不能丢可回溯性**：用真标题（取不到就回落域名），**绝不编一个标题**；
   末尾统一拼一段**内容指纹前缀**（如 sha256 前 8 位）→ 同名不同文不冲突、删了索引也能回溯。
   同时定好无效字符、空白、长度上限（按字符计，保护 Windows 260）与截断标记。
7. **一个对象两种时间时先问清口径**：如“我什么时候收录的” vs “它什么时候产出的”——目录按哪个、
   另一个体现在哪一列，要明确；缺失一律落 `unknown`，**不得用另一个时间冒充**。
8. **清理只扫已知旧形状**：删旧目录的代码不要 `rglob` 整棵数据树（库大时慢且易误删），
   只按已知道的旧布局形状下探，并在注释里写清“这是故意口径，不是漏扫”。

### 没有元数据的孤儿文件：隔离，不要编名字

盘上有文件、库里却没有对应行时，**没有标题/时间/类型可供命名** —— 硬编一个名字就是伪造元数据，
删掉又不可逆。做法：移到 `_orphan/` 类隔离目录并在报告里给出数量；索引只覆盖有元数据的那部分。
若用户确认其中某些“本该有库行”，再写回填脚本补元数据后按新方案迁移 —— 不要一次把两侧都改掉。

## Pitfalls

- **`WinError 5 拒绝访问` on a directory rename is an open handle, not permissions.** Usual cause is an
  Explorer window parked inside that folder (a process with it as CWD does it too). Files *inside* the
  directory still rename fine, so: move the contents into the new directory, then `rmdir` the emptied
  shell — a retry a minute later usually succeeds. Retrying the rename itself 5× over ~8s is wasted
  time while the handle lives. Drill down recursively: a subdirectory inside the locked folder can be
  refused too, so move its children individually and keep recursing until only renameable entries are
  left. Accept the fix by the file count, not by the shells vanishing — an empty shell that survives
  cleanup costs nothing, and chasing it further is time spent on the user's Explorer window.
- **Build scripts embed absolute paths and fail silently.** Python/JSON helpers holding
  `D:/proj/OLD/x.docx` keep running and write to the wrong place, or fail only at the next render.
  Treat every script that names the workspace as a reference site in step 2.
- **Tool-generated profile dirs are not content.** A stray directory whose children are
  `autocorr/ autotext/ basic/ config/ database/ extensions/ gallery/` is an office-suite user profile
  left behind by a headless docx→pdf render — archive it, never present it as user material.
- **An unidentified file gets identified, not guessed.** Open it (a PDF with a garbled filename turned
  out to be the official transcript) and give it a descriptive name; the user finds files by searching
  for meaning, never for the original hash-like string.
- **Report before/after file counts.** The user's trust in a bulk move rests on "nothing was lost", and
  only the count proves it.
