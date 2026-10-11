# migrations/ —— 数据库迁移脚本（DDL 的执行体）

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **文件个数、行数刻意不写在这里** —— 会随代码腐烂，一律见 `docs/代码地图/00-符号清单.md`。
> 表结构也不在这里复述，权威说明见 `docs/02-数据模型.md`。

## 1. 这个目录放什么

只放 `.sql` 文件，是 `run/state.db` 全部 DDL 的**唯一执行体**（对应说明与权威在 `docs/02-数据模型.md`）。

- **`0001_init.sql` 建全部表**；之后每一处结构变更 = **新增一个文件**。
- 还有 `0002_source_grades_and_tier_rules.sql`（判权表 + 分层规则种子）、`0003_tier_rule_gaps.sql`
  （E1 评测发现的规则缺口，如 `.gov.cn`），以及 `0004` 起按主题的增量。
- **不建第二份 `schema.sql`**：两份 SQL 必然漂移（`docs/02` 明文禁止）。

**"只增不改"的纪律（最重要）**：已应用过的迁移文件**不许再改** —— 改了，历史库与"新库从
0001 跑到最新"得到的结果就不一致，且没有任何机制能发现。**结构变更新增 `NNNN_*.sql`，
不改 0001 / 0002 / 0003 这些历史文件**（它们是"当时补了哪些缺口"的历史记录）。

**命名规则**：`NNNN_描述.sql` —— `NNNN` 是四位数字版本号（升序、唯一），描述只含
`[A-Za-z0-9_-]`。这条由执行器 `state/migrate.py` 的正则 `MIGRATION_FILE_RE` 强制：
**文件名不合规直接抛错**（不让它静默漏掉），**版本号重复也抛错**。

## 2. 先看哪个文件

1. **`0001_init.sql`** —— 全部建表。先读它看数据模型全貌（头部注释也写清了
   "结构变更请新增 `NNNN_*.sql`，不要改本文件"）。
2. **`0002_source_grades_and_tier_rules.sql`** —— 判权表 + 分层规则种子（权威性判定的分母）。
3. **`0003_tier_rule_gaps.sql`** —— E1 评测跑出来的规则缺口（`.gov.cn` 与 `.gov` 是两条规则）。
4. 之后按主题读：`0004`（`documents.extract_detail`）、`0005`（会话置顶）、`0006`（支持度校验）、
   `0007` / `0011` / `0014` / `0015`（抽背卡与排期）、`0008`（产物版本）、`0009` / `0013`（用量）、
   `0010`（跨轮研究台账）、`0012` / `0016`（跨会话记忆双账）、`0017`（飞书配对）。

## 3. 哪些是 API（外部可依赖的契约）

迁移文件本身**没有 Python API**；它对外暴露的"契约"是**跑完之后得到的那套表结构 +
`schema_version` 记账**。要把它们应用起来的执行器 API 在 `state/migrate.py`：

- `migrations_dir()` —— 本目录的路径
- `discover(directory)` —— 返回 `[(版本号, 文件路径)]`，按版本号升序；命名不合规 / 版本重复直接抛
- `current_version(db)` —— 当前库版本（未初始化返回 0）
- `apply_migrations(db, *, target, directory)` —— 按序应用，返回本次实际应用的版本号列表

应用机制（**改本目录前必须知道**）：

- **幂等**：已应用的版本记在 `schema_version` 表里，`number <= current_version` 的直接跳过，
  重复执行不会重跑。
- **原子**：每个迁移与它的版本登记（`INSERT INTO schema_version ...`）**在同一个事务里提交** ——
  执行器显式写 `BEGIN; … COMMIT;`（因为 `executescript` 会先结束隐式事务）。
- **入口**：建库/升级走 `state/initdb.py` 的 `init_db(db_path, ...)`，即
  `python -m scholarscout.state.initdb --db run/state.db`。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- **DDL 权威**：`docs/02-数据模型.md`（§10 建库与迁移也写了这里的纪律）。
- **关键路径**：`AGENTS.md` §5 / `docs/项目规则.md` §5 —— `src/scholarscout/state/migrations/*.sql`
  属"必须人工逐行 review"的文件（**数据迁移不可逆**）。
- 测试：`tests/state/test_schema.py`（空库建表成功、`PRAGMA foreign_keys` 生效、
  **迁移重复执行幂等**）。
- 目录职责与整体放置：`docs/03-目录结构与规则文件.md` §1。
- 符号清单（含 `migrate.py` 的导出符号）：`docs/代码地图/00-符号清单.md`。

## 改这个目录之前先知道

- **结构变更 = 新增文件，绝不改历史文件**（这是本目录的第一条铁律）。
- **先改 `docs/02-数据模型.md`（它是权威），再写迁移** —— 代码与本文件不一致时改代码；
  要改模型，先改 `docs/02` 并说明理由。
- **上主库前先备份、先在副本库跑通**：`sqlite3 run/state.db ".backup run/backup_YYYYMMDD.db"`。
- **PRAGMA 不写在迁移里**：`journal_mode` / `foreign_keys` 由 `state/db.py` 在连接时设置
  （`0001_init.sql` 头部注释有说明）。
- 迁移与版本登记必须同一事务 —— 别绕开执行器自己跑 SQL，否则 `schema_version` 会与实际结构脱节。
