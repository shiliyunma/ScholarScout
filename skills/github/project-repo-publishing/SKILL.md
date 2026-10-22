---
name: project-repo-publishing
description: "Use when 把一个本地项目首次发布到 GitHub：首推前复核、首推后验证、README 分工。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [git, github, 首次发布, 仓库卫生, README, 换行符]
    category: github
    related_skills: [github-repo-management, repo-secret-scrubbing]
---

# 本地项目首次发布到 GitHub

适用：把已有本地目录变成公开/远程仓库，或用户说"把这个 demo 初始化 git 并连到远程库"。
只看本地能跑不够 —— 发布环节会引入三类新风险：**推上去跑不起来**、**推了不该推的东西**、
**README 与规划文档混在一起**。

仓库 API 操作（创建/克隆/fork/发布/密钥）见 `github-repo-management`；
已经泄露的密钥要洗历史见 `repo-secret-scrubbing`。本技能只管**首次发布前的复核与首推后的验证**。

## 顺序

1. **先看有没有已存在的远程仓库，再决定本地怎么建。**
   `git ls-remote <url>` **退出码 0 且无输出** = 仓库已存在但还没有任何 ref（新建的空仓）——这是首推前的正常状态，不是错误；
   仓库不存在时它会打印 `Repository not found` 且非 0 退出。这条能区分"我已经建好了空仓"与"我把名字拼错了"。
   `git remote add` 之前先确认本地是不是已经是仓库（`git rev-parse --is-inside-work-tree`）——
   重复 `git init` 会另起一份历史。
2. **列真实发布集，再提交。**
   ```bash
   git add -A && git status --short
   git ls-files            # 这才是真正要发出去的东西
   git ls-files | grep -Ei '\.env$|\.db$|__pycache__|/run/|\.log$|token|credential'   # 应无输出
   git ls-files -z | xargs -0 du -b 2>/dev/null | sort -rn | head                      # 有异常大文件吗
   ```
   运行产物（`run/`、`*.db`、`__pycache__/`、输出目录）在**同一个提交里**加进 `.gitignore`；
   `.env.example` 入库、`.env` 忽略。
3. **Windows 作者的项目必须加 `.gitattributes`**（否则别的机器一 checkout 就整文件重写，`git status` 永久噪音）：
   ```
   * text=auto
   *.py    text eol=lf
   *.md    text eol=lf
   *.json  text eol=lf
   *.jsonl text eol=lf
   *.yaml  text eol=lf
   *.sh    text eol=lf
   *.ps1   text eol=crlf
   *.db    binary
   ```
   和首个提交一起提，索引从一开始就是规范的。`git add` 吐的 "LF will be replaced by CRLF" 是提示，不是失败。
4. **推完要做两个验证**：
   - `git ls-remote origin refs/heads/main` 必须等于 `git rev-parse HEAD`（证明远程真的收到了）。
   - **克隆到临时目录跑一次项目自检**（`python check.py` / build / 测试）：
     这是"仓库自包含"的**唯一证据** —— 能抳出“需要但没提交”的文件（被过宽的 .gitignore 误杀，或干脆忘了）。
     `git push` 成功对此一言不发。验完把临时克隆删掉。
5. **写清哪些不应上传**：把第 2 步的检查结果写进汇报（"run/ .env __pycache__ .db 均未上传"），
   而不是只说"推送成功"。

## README 只写项目，不写规划

公开仓库的 README 只装：这是什么 / 怎么跑 / 架构与目录 / 已知的坑 / 配置项。
**路线图、里程碑、验收标准、论文结构、进度打卡属于本地规划目录，不进 README** ——
即使这个仓库就是课程/毕设项目也一样。项目同时有仓库和规划目录时，新增的规划文字默认放规划目录，
不确定就问一句，不要顺手写进 README（用户会明确要求这个分工）。

## 推完记得回头改"让你去 git init"的文档

仓库一旦存在，任何还写着 `git init` 的规划/清单/笔记都会让下一个会话**另起第二份历史**，
之后两端无法调和（推送报 unrelated histories，或只能强推覆盖）。
grep 规划目录里的 `git init`，改成真实状态：仓库已建、远程已连、日常三步
`git add -A && git commit -m ... && git push`；并提醒"想另建顶层仓库先说一声"。

## 首次发布后可以顺手提的两件事

- **仓库可见性 / LICENSE 我不能自己判断**：Git 协议不返回可见性；告知用户当前不知、
  建议他把公开仓库配一个 LICENSE（开源项目标配，对求职是加分）。
- **图片/大文件不进历史**：发现要发布的仓库里有大二进制，先转 .gitignore + 外部引用，
  别先推后洗（洗历史代价高得多）。
