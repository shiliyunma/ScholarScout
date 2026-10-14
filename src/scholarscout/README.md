# scholarscout/ —— 根包：应用装配、配置与落盘路径

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。
> 各分层的目录树与模块边界见 `docs/03-目录结构与规则文件.md`（不复述）。

## 1. 这个目录放什么

根包只放**进程级、跨层**的少数几件事，其余都是分层子包（`runtime/` `tools/` `state/` …）：

- **`paths.py`** —— **所有落盘路径的唯一入口**：拼 `data/...` / `run/...` 并断言不越界（防 `../` 逃逸）。
  其他模块**不许**自己拼 `data/` 字符串。资料的命名 slug 与布局分桶也在这里。
- **`config.py`** —— 配置加载：设置来自 `config.yaml`、**密钥只从 `.env`/环境变量读**（设置与密钥分离）；
  文件不存在时用内置默认值。
- **`settings_io.py`** —— 设置的写回：**校验 → 深合并 → 原子写回**，**密钥只写不回读**。
- **`app.py`** —— 应用装配：配置 + 库 + 模型客户端 + 出网客户端 → `AppContext`（只有一处装配逻辑）。
- **`__main__.py`** —— `python -m scholarscout` 的 CLI 主体（子命令见下）。
- **`__init__.py`** —— 包标记与 `__version__`。
- 分层子包：`rpc/` → `runtime/` → `tools/` → `{providers, fetch, grading, library, learning, summary, state}`；
  入口层 `desktop/`、`channels/` 只经 `rpc/` 说话。

**`paths.py` 为什么是"唯一入口"**：路径逃逸是桌面壳/前端能碰到的真实攻击面（一个被篡改的库，
或前端传来一个相对路径，就能让壳打开任意文件）。所以入口只有一处、越界**直接抛**（`PathOutsideDataError`，
调用方不许 `try` 掉）。另外 `home()` 在打包后指向 **exe 所在目录**，让 `data/`、`run/state.db`、
`config.yaml` 落在用户看得见、拷得走的地方。

## 2. 先看哪个文件

1. **`paths.py`** —— **先读它**：所有落盘位置与命名口径的总闸，别的模块的路径都从这来。
2. **`config.py`** —— `load_config()` 与几个 `Profile` 数据类（`Config` 是根）；密钥如何注入看它。
3. **`app.py`** —— `build_context()`：把上面两样 + 库 + 模型 + 出网装成一个 `AppContext`。
4. **`__main__.py`** —— CLI 子命令的分发（`--rpc` / `--chat` / `--desktop` / `--initdb` / `--setup-feishu` /
   `--check-ui`…）。**飞书入站没有 `--feishu-listen` 这种子命令了**：起长连接走 `scripts/feishu_ws_up.py`
   （入站传输在 `channels/feishu_ws.py`，与 CLI 分开）。
5. **`settings_io.py`** —— 设置的写入路径（白名单字段、原子写、密钥只写不回读）。
6. **`__init__.py`** —— 只是个包标记，没有逻辑。

## 3. 哪些是 API（外部可依赖的契约）

调用方是整个进程（CLI、`rpc/`、`desktop/`）与 `tests/`。改签名 = 破坏契约。契约权威见 `docs/01-接口契约.md`。

- **`paths.py`**：`home()`、`data_dir()`、`run_dir()`、`db_path()`、`ensure_dirs()`、`under_data()`、
  `layout_dir()`、`raw_path_for_layout()`、`text_path_for_layout()`、`doc_basename()`、`slugify()`、
  `doc_type_for()`、`year_month_bucket()`、`library_index_path()`、`note_path_for()`、`rel_to_home()`、
  `resolve_rel()`、`normalize_layout()`、`class PathOutsideDataError`；
  模块级 `DATA_SUBDIRS` / `LAYOUT_SEGMENTS` / `DEFAULT_LAYOUT`。
- **`config.py`**：`load_config(config_path, *, load_env)`、`load_dotenv(path)`、`normalize_style(value)`、
  `STYLE_CHOICES`；数据类 `Config` / `LlmProfile` / `RuntimeProfile` / `Features` / `Generation` /
  `UiSettings` / `LibraryProfile`。
- **`settings_io.py`**：`read_settings(config)`、`config_path_of(config)`、`apply_updates(config, updates, ...)`、
  `write_env(path, values)`、`drop_env_key(path, key)`、`data_usage(root)`、`cleanup(...)`、
  `optimize_database(db, root)`；`class SettingsError` / `class Field` / `FIELDS` / `SECRET_FIELDS`。
- **`app.py`**：`build_context(*, config, db_path, llm, http, toolsets, auto_migrate)` → `AppContext`。
- **`__main__.py`**：`build_parser()`、`main(argv)`。**`__init__.py`**：`__version__`。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 目录树与模块边界：`docs/03-目录结构与规则文件.md` §1（目录树）、§2（谁可以 import 谁）。
- 命名与放置口径（**`paths.py` 的布局与 slug 的依据**）：`docs/19-资料命名与放置方案.md` §1 / §2。
- 数据模型（表结构）：`docs/02-数据模型.md`；接口契约：`docs/01-接口契约.md`。
- 规则与错误处理：`AGENTS.md` §3 错误处理（其中一条：「落盘路径必须经 `paths.py` 并断言在 `data/` 之下」）。
- 测试：`tests/test_paths.py`、`tests/test_settings_io.py`、`tests/test_cli_defaults.py`、
  分层门禁 `tests/test_layering.py`。
- 符号与文件清单：`docs/代码地图/00-符号清单.md`（生成物，勿手改）。

## 改这个目录之前先知道

- **落盘路径只有一个入口**：任何"造路径"的活都回 `paths.py` 加函数；别在业务模块里拼 `data/...`。
  `under_data()` / `resolve_rel()` 越界**直接抛**，调用方不许把异常吞掉。
- **`scripts/scout.py` 与 `__main__.py` 是"启动器 + 主体"两件事**：真正实现 CLI 的是 `__main__.py`；
  `scripts/scout.py` 只是**启动器**，它把 `src` 显式插进 `sys.path` 再调 `__main__.main()`。
  原因是项目路径含中文，而 editable 安装的 `.pth`（UTF-8）会被 CPython 用 locale 编码（GBK）读取、
  解释器启动即崩（`docs/03 §9`）。**因此 CLI 一律用 `python scripts/scout.py ...`，禁止 `pip install -e .`**。
- **配置只做"取值合不合法"的校验，不反向依赖 `runtime`**（如风格档文本在 `runtime/prompts.py`，
  `config.py` 只校验档位名；两处一致性由测试钉住）。
- **设置写回的三条纪律**（`settings_io.py`）：只认白名单字段（写未知字段直接报错）；
  在原始 YAML 上深合并（不抹掉用户自己加的键）；**原子写**（先 `.tmp` 再 `os.replace`）。
- **密钥只写不回读**：`apply_updates()` 遇 `SECRET_FIELDS` 只写进 `.env`，读回来的只有"是否已配置"这类布尔，
  绝不把密钥送回前端。
- **改 `paths.py` 的任何 `*_path_for` / 布局口径 = 动所有落盘位置**：库里的相对路径、迁移脚本、
  `tools/` 与 `library/` 都跟着变，改前先看 `docs/19`。
