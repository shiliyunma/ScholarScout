---
name: local-workspace-refactor
description: "Use when reorganizing a user's local file workspace."
version: 1.0.0
metadata:
  hermes:
    tags: [files, refactor, markdown, windows, cron, references]
    category: productivity
---

# 本地工作目录重构（分层 / 改名 / 保持引用一致）

适用：用户说「这个目录太乱，整理一下」「把 X 分成两本 / 几个模块」「改个名」—— 即对他本地的工作目录做**结构性改动**，而不是写正文。判定标准：移动完不算完，**所有引用它的地方都还得对**（索引文件、其他模块的 md、脚本硬编码路径、定时任务 prompt）。

## 铁律（每次都要做）

1. **先列引用，再动手**：`grep -rn "<目录名或文件名>" --include="*.md" --include="*.py" --include="*.json" .`。改完**再扫一遍**，确认无残留、无双重前缀（如 `图片\图片\`）。
2. **改已有文件前先留副本**：`cp` 到 `_归档\`，或先写临时文件、比对行数与**字节数**无误再替换。脚本一次失误就能覆盖源文件；本地目录常常没有 git 兜底。
3. **用户的分层/命名要求照做，不要自由发挥**：他说「台账不进文件夹」就只把台账留根，其余按类进子目录；子目录用编号前缀，并与上层模块编号一致（`1-情报\` 里就用 `1-x / 2-x`）。归类拿不准时，把「台账类 / 记录类」留根、「素材与图片」进子目录，是安全默认。
4. **搬动后必须同步写死路径的地方**：① 定时任务（cron）的 prompt；② 索引/导航文件（如 `00-总索引.md`）；③ 脚本里的硬编码路径；④ 各模块 README。**漏 cron prompt 尤其隐蔽** —— 任务照旧 `last_status: ok`，只是第二天输出变成「读不到文件」。
5. **收尾写索引 + 在进度日志里记一笔**（移动明细 + 回滚命令），让下次会话能恢复上下文。

## 顺序

1. `find . -maxdepth 2 | sort` + `ls -la` 看清现状（含 `_` 前缀的临时目录）。
2. grep 列引用（铁律 1）。
3. 备份或确认没有 git 可回滚。
4. 写一个一次性 Python 脚本：建目录 → `shutil.move` → 文本替换修引用；**每处替换都 `assert old in text`**，替换顺序「先长后短」（否则已被改过的路径会被更短的规则再次命中）。骨架见 `templates/move-and-fixrefs.py`。
5. 运行后**逐项校验**：新树结构、行数/字节数、`grep -c "^| "` 确认表格没被截断。
6. 复扫引用 + 同步 cron prompt + 更新索引与进度日志。

## Pitfalls

- **不要用 `split("---")` 切 markdown 章节**：表格分隔行 `|---|---|` 里含 `---`，会被当成章节边界**静默截断表格**（丢数据但不报错）。按 `re.split(r"\n(?=## )", t)` 切章节，只剥**行尾**的 `---`：`re.sub(r"\n[ \t]*-{3,}[ \t]*$", "", s.rstrip())`。
- **校验文件大小用 `os.path.getsize()`**，不是 `len(text)` —— 中文按字符计，`len()` 只有字节数的约 1/3，用它写 `assert len(t) > N` 会误报。
- **`shutil.move(src_dir, dst_dir)` 在 `dst_dir` 已存在时会把整个源目录塞进去**（变成 `dst_dir/src_dir`）→ 目标不存在再移，或移完显式改名。
- **`WinError 5 拒绝访问` 不是权限问题**：资源管理器窗口停在那两个文件夹里占着目录句柄。先逐个搬内容（文件能改名），隔一会儿再删空壳。
- **git-bash 里给原生程序传路径必须用 `C:/x/y` 正斜杠**，`/c/x/y` 不会被转换（`cd` 是 bash 内建所以能用）。
- 引用替换要覆盖**所有相关扩展名**（`.md` `.py` `.json`），并排除 `_归档\`、`.git` 等目录。
- 中文内容的读写一律显式 `encoding="utf-8"`；脚本头加 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`，Windows 下运行加 `PYTHONIOENCODING=utf-8`。
- **时间段/分类标签不要自己猜**（例：把「下午收到的消息」命名成「晚」）：档案类目录的时段、批次命名要么照用户原话，要么先问；猜错就要改名 + 再扫一遍引用。

## 重叠提示

用户的 `qiuzhao-ledger-zh`、`hermes-qqbot-channel`、`qq-bot-group-monitoring` 等 skill 里也有部分同类内容（台账分层、cron 推送验证）。那些是用户自有的，后台 curator 不能改；如果它们需要更新，让用户执行 `hermes curator adopt <name>`。
