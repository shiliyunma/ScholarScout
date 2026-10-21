# scripts/ —— 运维 / 门禁 / 构建脚本（非 Python 包）

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这个目录是干什么的、先看哪个文件、
> 哪些是入口契约（用法 / 退出码）、去哪看更多。
>
> **文件清单、行数、导出符号刻意不写在这里** —— 它们会随代码腐烂，由
> `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`（该生成器默认扫
> `src`、`scripts`、`eval`，**覆盖本目录**）。
> 每个脚本的用法与退出码**以该文件头部的 docstring 为准**（那里是权威），本文件只交代跨脚本的约定。

## 1. 这个目录放什么

项目级的一次性 / 可重复脚本，**不在 `src/scholarscout` 包内**（因此不参与运行时的分层 import 规则，
但仍在门禁的 lint / format 覆盖范围内）：

- **门禁与自检**：`check.sh`、`check_rules_sync.py`、`check_new_deps.py`、
  `check_search_sources.py`、`check_mineru.py`
- **环境与入口**：`bootstrap.sh`、`install_deps.py`、`scout.py`
- **构建 / 打包**：`build_exe.py`、`build_bundle.py`、`make_icon.py`、`build_thesis_docx.py`
- **文档 / 论文数据**：`make_code_map.py`、`make_thesis_bib.py`、`collect_thesis_refs.py`、
  `collect_thesis_refs_targeted.py`、`thesis_stats.py`
- **迁移与真机诊断**：`migrate_raw_names.py`、`diagnose_grounding.py`、`diagnose_turns.py`、
  `verify_multiturn.py`
- **拉起外部服务**：`searxng_up.py`（自建 SearxNG）、`feishu_ws_up.py`（飞书 websocket 长连接一键开机）

## 2. 先看哪个文件

1. **`check.sh`** —— 门禁总入口。先读它，就知道"什么算合"。
2. **`bootstrap.sh` → `install_deps.py` → `scout.py`** —— 建环境、装依赖、跑 CLI 的固定三步。
3. **`make_code_map.py`** —— 符号清单的唯一生成器（清单禁止手改）。
4. 按需：真机自检（`check_search_sources.py`、`check_mineru.py`）、拿起外部服务
   （`searxng_up.py`、`feishu_ws_up.py`）。

## 3. 入口契约（用法 / 退出码）

- **`check.sh [--fast|--strict]`**（默认 full）：full 跑 lint + format + types + tests +
  rules-sync + deps-approval + ui-assets；`--fast` 只跑 lint + format + rules-sync + deps-approval；
  `--strict` 下缺工具即算失败（CI 用）。
  **验收标准是汇总行** `======== 汇总：失败 N 项，跳过 M 项 ========` ——
  只有 **`失败 0 项，跳过 0 项`** 才算真全绿；脚本自己在"通过"那行提示"跳过 M 项，不等于全绿"。
  **跑之前把 `.venv/Scripts` 加进 PATH**：脚本用 `command -v` 找 ruff / mypy / pytest，
  找不到就记 **SKIP（跳过）而不是失败** —— 这会造成"跳过若干项"的假绿。
- **`bootstrap.sh`**：`uv venv --python 3.11` → `install_deps.py` → `check_new_deps.py` → `scout.py` 自检；
  缺 `uv` 退出 1。
- **`install_deps.py [--runtime]`**：读 pyproject 装依赖、**不装本包本身**；找不到 `.venv` 报错退出。
- **`scout.py`**：CLI 启动器（把 `src` 插进 `sys.path`），如 `--version` / `--initdb` / `--desktop`。
- **`check_rules_sync.py [A B]`**：0 = 逐字一致；1 = 不一致 / 缺标记 / 缺必需文件。
- **`check_new_deps.py`**：出现未审批依赖即退出 1。
- **`check_search_sources.py [--all] [--query Q] [--limit N]`**：**免 key 来源全部成功**才返回 0
  （缺 key 的商用来源不算失败，打印 `missing_api_key`）。
- **`check_mineru.py`**：`--make-sample PATH`（造扫描版样本）或 `--pdf PATH`（真机验证抽取）。
- **`make_code_map.py [--check] [--roots ...]`**：生成 `docs/代码地图/00-符号清单.md`；
  `--check` 只比结构，过期退出 1。
- **`migrate_raw_names.py --dry-run`**：旧命名 → 当前布局；失败数为 0 才返回 0。
- **`feishu_ws_up.py [--check]`**：飞书**一键开机**（`lark-oapi` 的 websocket 长连接，**不需要隧道/回调地址**）。
  先做前提自检（SDK 装没装 / 凭据有没有 / 出站开关开没开），再装配长连接客户端并**阻塞**收包；`--check` 只自检不起连接。
  退出码 **0 正常（含 Ctrl-C）/ 2 前提没满足 / 3 起连接失败**。**首次 `import lark_oapi` 本机约 116 秒**（脚本会先打这句提示）。
- **`searxng_up.py [--status] [--query Q]`**、**`build_exe.py [--out-dir D] [--diagnose]`**、
  **`build_bundle.py [--with-config] [--with-secrets] [--name N] [--dist-dir D]`**（默认只带 `.env.example`；
  真 `.env` 要 `--with-secrets`，且打完自检会因"包里有非空密钥"直接失败）、**`make_icon.py [--src --out]`**、
  **`diagnose_grounding.py --task/--limit`**、**`diagnose_turns.py [--db --limit]`**、
  **`verify_multiturn.py [--goal --follow]`**（会花真钱）：参数与用法以各文件头部 docstring 为准。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 门禁组成与依赖政策：`docs/03-目录结构与规则文件.md` §4（门槛表）/ §5 / §6。
- 关键路径清单（必须人工逐行 review）：`AGENTS.md` §5 —— 含 `eval/*`（计分逻辑）等。
- **中文路径坑（本目录存在的理由之一）**：`docs/03-目录结构与规则文件.md` §9。
- 各脚本的离线回归：`tests/scripts/`；符号清单：`docs/代码地图/00-符号清单.md`。

## 改这个目录之前先知道

- **不做 editable 安装**：项目路径含中文（`D:\AI+教育毕设毕设\...`），`pip install -e .` 写的
  `.pth` 会让解释器启动失败（连 `print(1)` 都跑不了）—— 所以装依赖走 `install_deps.py`、
  跑 CLI 走 `scout.py`（`docs/03 §9`）。
- **改脚本要过门禁**：`check.sh` 的 ruff lint / format 覆盖 `scripts`，别绕过。
- **符号清单是生成物**：改 `make_code_map.py` 采到的 docstring 首句会让 `--check` 变红；
  别手改 `docs/代码地图/00-符号清单.md`。
- **拿起外部服务是显式动作**：通用检索依赖 Docker 里的 SearxNG，容器不会在 Docker Desktop
  重启后自己回来 —— 用 `searxng_up.py` 一条命令拉起（`docs/03 §7`）。
