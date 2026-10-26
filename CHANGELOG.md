# 更新日志

本项目的所有重要变更都记录在这个文件里。

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)。

## [0.1.1] - 2026-10-26

**发布工程规范修正版。** 功能与 v0.1.0 一致，改的是"发出去的东西怎么管"——
v0.1.0 发完就把包留在了仓库根目录、`pyproject` 还写着旧版本号，两件事都得先修。

### Fixed

- **版本号只有一个事实源**：`scholarscout.__version__` 不再手写常量，改为按顺序读
  `importlib.metadata`（装出来的包）→ `pyproject.toml`（源码树）→ exe 旁边的 `_VERSION`
  （PyInstaller 包，由 `scripts/build_bundle.py` 从 `pyproject` 写出）。v0.1.1 之前
  `pyproject` 写 `0.1.0`、包名却叫 v0.1.1，打包版 `--version` 也报 `0.1.0`——
  用户报 bug 时给的版本号是错的。`tests/test_version_alignment.py` 钉住这条闸。
  （`src/scholarscout/__init__.py`、`scripts/build_bundle.py`、`pyproject.toml`）
- **发布产物按版本落盘**：新增 `scripts/release.py`，一条命令完成
  构建 → 打 zip → 算 SHA256 → 落 `releases/v<版本>/` → 写 `SHA256SUMS` /
  `RELEASE_NOTES.md` / `MANIFEST.json`（含 version / commit / built_at / bytes /
  sha256 / exe_bytes / skill_count）。zip 走标准库 `zipfile`（本机无 `zip` 命令），
  并在顶层套一层 `ScholarScout-Portable-v<版本>/`（v0.1.0 的包是平铺的，
  解压会把 `_internal/`、`skills/` 撒进当前目录）。
- **产物不进 git**：`.gitignore` 补 `*.zip` 与 `/releases/`——v0.1.1 的 zip 之前就躺在
  仓库根目录、对 `git status` 永远显示未跟踪，一不留神就会被 `git add -A` 带进历史。

### Added

- `docs/29-发布产物管理.md`：产物怎么放、旧版本在哪找、`release.py` 用法、发版台账。
- `releases/v0.1.0/MISSING.md`：如实写明本地副本已丢失、官方副本在 GitHub Releases
  （不伪造哈希与体积）。

## [0.1.0] - 2026-10-26

首个公开版本。此前是毕设开发期（`0.0.x` 只在本机迭代），没有对外发布过，
所以这里不列历史版本 —— 编一份「以前发过什么」等于给使用者一个假前提。

### Added

- **受限代码执行 `py.exec`**：模型不再只能「回一段说明让你自己动手」，而是能读并改
  你本机的 docx / xlsx / pdf。执行发生在**独立子进程**里，Windows 用 **Job Object**
  限内存、限进程数、超时 `TerminateJobObject` 杀整棵树；子进程内 `sys.addaudithook`
  拦 `open` / `socket.*` / `subprocess.*` / `ctypes.*` / `os.system|exec|spawn` / `os.chdir`，
  判据是 `realpath` + `commonpath` 而不是「字符串里有没有 `..`」。**两段式审批**：
  工具先回 `needs_approval` 与完整代码，你点头才真跑；永久授权落盘
  （`run/sandbox_rules.json`，原子写、坏文件降级空表），设置页可查看并**撤销**。
  （`src/scholarscout/tools/py_exec.py`、`src/scholarscout/sandbox/runner.py`、
  `sandbox/approval.py`、`sandbox/rules.py`）
- **本地全文检索 `kb.search`**：在**已收录**的资料里检索，SQLite **FTS5** 建索引，
  中文走**逐字 n-gram** 列（`unicode61` 会把「深度学习」当一个整串 token，只有正文能命中）；
  库不带 FTS5 时 `probe_fts5` 检测并降级 `kb_plain`，**不静默**。受
  `features.enable_kb` 控制（**默认关**；开着才真多出这个工具，不是「开了什么都不发生」）。
  （`src/scholarscout/tools/kb_search.py`、`src/scholarscout/state/kb_index.py`、
  `src/scholarscout/config.py::Features.enable_kb`）
- **技能系统**：扫 `skills/` 建索引但**不常驻系统提示词**（常驻会按技能数线性烧 token），
  模型用 `skill.list` / `skill.load` 按需取；界面的 `/` 菜单与模型看到的是
  **同一份** `skill.list({query})` 排序（前端不自己筛，否则菜单第一名与模型拿到的分叉）；
  技能目录旁的 `references/`、`scripts/`、`templates/` 随技能一起走。
  （`src/scholarscout/skills/index.py`、`src/scholarscout/tools/skills.py`、
  `src/scholarscout/rpc/skills.py`、`src/scholarscout/desktop/ui/app.js`）
- **渠道群与模块绑定**：`channel.list_chats`（设置页「渠道与群」的唯一数据源）/
  `channel.set_module` / `channel.get_module`。群里拉进来的消息按绑定模块解析，
  没绑定时回退默认 —— 绑定是**真生效**的，有端到端用例钉住。
  （`src/scholarscout/rpc/channel.py`）
- **会话级工具调用预算**：`runtime.max_tool_calls_per_session`（默认 `None` = 不限）。
  超限时**剩余调用一个都不执行**、每个未执行调用补一条如实 `role="tool"` 回复，
  随后给一次无工具收口并置 `stop_reason="session_tool_limit"`；再提交新轮直接拒
  （`session_tool_limit_exceeded`）。`chat.status.session_tool_budget` 报
  `{enabled, limit, used, remaining, exceeded}`，没配时**不假报 0**。
  （`src/scholarscout/runtime/budget.py`、`src/scholarscout/config.py`）
- **日志落盘与密钥脱敏**：打包版双击没控制台，「界面没反应」时曾经没有任何现场。
  现在落 `run/logs/scout.log`，轮转大小 / 份数 / 级别由 `runtime.log_max_mb`、
  `runtime.log_backups`、`runtime.log_level` 驱动（非法值直接报错）；脱敏**两道** ——
  真实密钥值精确替换 + `sk-[A-Za-z0-9_-]{10,}` 兜底正则，`record.getMessage()` 先格式化
  再脱敏（`logger.info("%s", key)` 走 args 也拦得住），`%` 转义防「脱敏把日志打挂」。
  一个密钥都没配就**不装**过滤器（不假装在脱敏）。
  （`src/scholarscout/logging_setup.py`）
- **多源调研与引用核查**：`research.search` → `source.fetch` → `source.grade` →
  `topic.build_map` / `route.plan`，结论带得出处；`grading/support.py` 单独判
  「这条来源是否**真的**支撑这条结论」，不是「有引用就算」。抓取侧按 host 限速、
  指数退避、缓存与 robots，正文抽取覆盖 HTML / PDF（本地 pymupdf）/ docx / xlsx / pptx /
  epub / rtf / zip。
  （`src/scholarscout/tools/research_search.py`、`tools/source_fetch.py`、
  `tools/source_grade.py`、`src/scholarscout/grading/support.py`）
- **飞书渠道**：走官方 SDK 的 **websocket 长连接**（本机主动外连，**不需要公网地址、
  回调或隧道**），支持入站消息、回发文件、应用内建应用向导与凭据引导。
  （`src/scholarscout/channels/feishu_ws.py`、`channels/feishu.py`、
  `channels/feishu_setup.py`）

### Changed

- **发布包不再带用户配置，但带上技能**：`ScholarScout-Portable/` 不带 `.env`、
  `config.yaml`、`data/`（别人的密钥与资料不该出现在交付物里），带 `skills/`；
  打包自检把「带了用户配置」与「少了技能」都判成失败，不靠手看。
  （`scripts/build_bundle.py`）
- **`state/` 之外不再有任何 SQL**：曾经 56 处 / 18 个模块的查询散在 rpc、runtime、
  tools、library、grading 各层，改一张表的列时没人知道哪个界面会崩。经过逐轮下沉，
  现在 `tests/test_layering_sql.py` 的白名单是**空字典** —— 空字典本身就是断言，
  此后 `state/` 之外再添一处 SQL 当场红。
  （`src/scholarscout/state/`、`tests/test_layering_sql.py`）
- **评测跑在隔离数据目录**：跑批不再往 `data/raw`、`data/text` 里写东西
  （以前跑一次评测就污染一次你自己的资料库），改用临时目录里的独立 `data_dir` 与 `db_path`。
  （`eval/run_eval.py`）
- **设置页新增「渠道与群」模块绑定表**：绑定关系的读写只经 `channel.*` 三个方法，
  界面不另拼一份。
  （`src/scholarscout/desktop/ui/app.js`、`src/scholarscout/rpc/channel.py`）

### Fixed

- **越层 SQL 清零**：`state/` 之外的查询从 **56 处 / 18 个模块**降到 **0 处 / 0 个模块**。
  不再是「文档里写了一条谁都没执行的规则」——`tests/test_layering_sql.py` 用 AST 逐文件
  计次，「多一处红、少一处也红」，白名单只许减不许增。
- **抓取路径的六个毛病**：① 超大文件要先**下完整包**才发现超限 → 改为按 `Content-Length`
  预判 + 边读边累加、一超就 `close()` 且不重试；② 音视频**白下整包**才判掉 → 按
  `Content-Type` 与魔数在下载前就拒；③ `.ts` 同时命中「TypeScript」与「MPEG-TS」两个集合，
  结果把源码当视频 → 改为按内容判定；④ `.doc/.xls/.ppt` 声称支持却抽不出正文 → 移除假支持
  并给明确提示；⑤ 限速只作用于检索、**抓正文不限速** → 正文抓取也按 host 遵守
  `min_interval_s`；⑥ 一次**瞬时空白响应**被缓存成 6 小时的「事实」→ 空响应不再落缓存。
- **事件名双前缀**：漏登记的事件名会被自动加上 `chat.` 前缀，于是 `chat.needs_approval`
  实际发成 `chat.chat.needs_approval`，界面**永远收不到** —— 审批卡不弹、代码一动不跑，
  看上去像「模型没调用工具」。修法是把事件名登进 `NOTIFICATION_METHODS`。
- **窗口几何每夜静默失效**：记住的位置与大小下次启动不生效 → 写盘前先 `normalize`，
  几何真的记住了，且启动时按屏幕尺寸收窄。
- **一批「写了没人读」的开关与配置**：`features.enable_kb` 曾是「开着什么都不发生」的
  空开关（比没有开关更坏：它让人以为已支持知识库），现在是真工具的总闸；
  `features.enable_mineru` 与 `MINERU_API_TOKEN` 两处配置**没有任何代码在用**、
  MinerU 的四个体积 / 页数上限只是注释里的数字、`runtime.max_parallel_sessions` 读了却没接 ——
  全部接线或删除，不留假功能。
- **配置 typo 静默关掉安全阀**：`max_tool_calls_per_session: 'abc'` / `-3` / `true`
  会被归一成「不限」，于是这道会话级上限**直接消失**，而 `enabled:false` 与「用户没配」
  长得一模一样。现在非法值抛 `ValueError` 并带上键名（`bool` 特判，`True` 不会被读成 1）。

### Known Limitations

- **没有 JS 引擎**：动态渲染（SPA）页面抓不到正文，只拿得到空壳 HTML。
  需要正文时请找该站的静态版 / PDF / API。
- **旧版 Office 格式需先转换**：`.doc` / `.xls` / `.ppt` 不解析，请先另存为
  `.docx` / `.xlsx` / `.pptx`（工具会明确提示，不会假装成功）。
- **单文件上限 20 MB**：超过即拒（`too_large`），不会先下完再算。
- **扫描件依赖 MinerU**：无文字层的 PDF 走 MinerU 在线接口；免 token 的 Agent 接口
  **按 IP 限频**且可能整段时间不可用，精准接口需要 `MINERU_API_TOKEN`。失败会如实
  记进 `documents.extract_detail`，不会返回一段编出来的正文。
- **Docker 镜像没有 GUI**：镜像故意不装 `pywebview`，`--desktop` 会明确回
  「未安装 pywebview」；容器里只有 CLI / JSON-RPC / 飞书机器人。桌面窗口只能在宿主机跑。
- **微信 / QQ 未实现**：目前只有飞书一个渠道（`lark-oapi` websocket 长连接）。
  其它平台需要自己按 `channels/` 的接口接。

[0.1.0]: https://github.com/shiliyunma/ScholarScout/releases/tag/v0.1.0
