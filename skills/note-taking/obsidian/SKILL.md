---
name: obsidian
description: Read, search, create, and edit notes in the Obsidian vault.
version: 1.0.0
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Obsidian, Notes, Markdown, Vault]
    related_skills: []
---

# Obsidian Vault

Use this skill for filesystem-first Obsidian vault work: reading notes, listing notes, searching note files, creating notes, appending content, and adding wikilinks.

## Vault path

Use a known or resolved vault path before calling file tools.

The documented vault-path convention is the `OBSIDIAN_VAULT_PATH` environment variable, for example from `${HERMES_HOME:-~/.hermes}/.env`. If it is unset, use `~/Documents/Obsidian Vault`.

File tools do not expand shell variables. Do not pass paths containing `$OBSIDIAN_VAULT_PATH` to `read_file`, `write_file`, `patch`, or `search_files`; resolve the vault path first and pass a concrete absolute path. Vault paths may contain spaces, which is another reason to prefer file tools over shell commands.

If the vault path is unknown, `terminal` is acceptable for resolving `OBSIDIAN_VAULT_PATH` or checking whether the fallback path exists. Once the path is known, switch back to file tools.

## Read a note

Use `read_file` with the resolved absolute path to the note. Prefer this over `cat` because it provides line numbers and pagination.

## List notes

Use `search_files` with `target: "files"` and the resolved vault path. Prefer this over `find` or `ls`.

- To list all markdown notes, use `pattern: "*.md"` under the vault path.
- To list a subfolder, search under that subfolder's absolute path.

## Search

Use `search_files` for both filename and content searches. Prefer this over `grep`, `find`, or `ls`.

- For filenames, use `search_files` with `target: "files"` and a filename `pattern`.
- For note contents, use `search_files` with `target: "content"`, the content regex as `pattern`, and `file_glob: "*.md"` when you want to restrict matches to markdown notes.

## Create a note

Use `write_file` with the resolved absolute path and the full markdown content. Prefer this over shell heredocs or `echo` because it avoids shell quoting issues and returns structured results.

## Append to a note

Prefer a native file-tool workflow when it is not awkward:

- Read the target note with `read_file`.
- Use `patch` for an anchored append when there is stable context, such as adding a section after an existing heading or appending before a known trailing block.
- Use `write_file` when rewriting the whole note is clearer than constructing a fragile patch.

For an anchored append with `patch`, replace the anchor with the anchor plus the new content.

For a simple append with no stable context, `terminal` is acceptable if it is the clearest safe option.

## Targeted edits

Use `patch` for focused note changes when the current content gives you stable context. Prefer this over shell text rewriting.

## Wikilinks

Obsidian links notes with `[[Note Name]]` syntax. When creating notes, use these to link related content.

## 大浩的 vault：`E:\Obsidian`（实际使用的那一个）

`OBSIDIAN_VAULT_PATH` 未必指向它；先看 `E:\Obsidian` 是否存在。顶层：`操作系统/ 计算机网络/ 计算机组成原理/ 数据结构/ DAHAO/ AIHOT/`。

- **每个专题文件夹用 `_index.md` 当入口**（如 `计算机组成原理/_index.md`、`AIHOT/_index.md`）。新入口页照这个惯例命名，别另起一个名字。
- **没有装任何社区插件**（`.obsidian/plugins/` 不存在）→ **不要用 Dataview**，写 `dataview` 代码块等于死代码；改用 frontmatter 属性 + 标签 + callout + wikilink，表格视图用核心插件 **Bases**（`.base` 文件，`core-plugins.json` 里 `bases: true`）。核心插件已启用：properties、tag-pane、bases、daily-notes、templates、backlinks、outline。
- Bases 写法保持保守（只写全局 `filters:` + 一个 view 的 `order:`），`sort:` 之类版本间语法有差异，容易在 Obsidian 里报错。
- 笔记正文里的 `|` 在表格里要写成 `\|`；摘要里的 `<` `>` 要转义成 `&lt;` `&gt;`；标题里的 `[` `]` 换成 `（）`，否则会破坏 Markdown 结构。
- **AIHOT/`**：AI 行业动态监控。`_tools/reorganize_aihot.py` 把抓取脚本追加的快照流水（原 `AIHOT-动态.md` / `AIHOT-精选链接.md`，已有原件在 `_archive/原始快照/`）重建为 Obsidian 格式：`流水/AIHOT 流水 YYYY-MM.md`（按月分文件、按天分组、`#aihot/<category>` 标签、`> [!quote]- 摘要` 折叠）、`精选榜.md`（score ≥ 85）、`AIHOT 总览.md`、`_index.md`、`AIHOT.base`。改脚本后要 `python reorganize_aihot.py --no-backup` 重跑并校验 wikilink 目标都存在（尤其 `_prev(month)` 会生成不存在的上月链接）。抓取计划任务：`schtasks /query /tn AIHOT-Fetcher`，脚本 `E:\Obsidian\AIHOT\fetch-aihot.ps1`，任务文件 ACL 只给用户读权限，改任务必须 UAC 提权（`Start-Process schtasks -ArgumentList ... -Verb RunAs -Wait`）。
