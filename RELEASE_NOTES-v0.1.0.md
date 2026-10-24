# ScholarScout v0.1.0

ScholarScout 是一个**本地优先**的学术调研与学习辅助智能体：你给它一个研究主题或一句问题，
它去公开来源检索、抓取、精读，把**带出处**的结论和摘要留在你本机的资料库里。
它解决的是「查了一堆资料，最后说不清哪句话是从哪来的」——每条结论都能点回原文，资料、会话、
索引全部落在你自己的盘上，只有模型调用和资料抓取会出网。

> 这是第一个公开版本。它按「自己用得下去」的标准做的，不是按「覆盖所有人」的标准。

---

## 安装

### ① 桌面版（Windows，下载即用）

下载下面的 `ScholarScout-Portable-x.y.z-win64.zip`，解压到**不含中文的路径**（如 `D:\apps\ScholarScout`）后双击 `ScholarScout.exe`。
首次启动会生成 `config.yaml` 与 `.env.example`，复制成 `.env` 填 `LLM_API_KEY`，重开即用。

### ② 源码跑（Windows / macOS / Linux）

需要 Python 3.11 与 [uv](https://docs.astral.sh/uv/)：`uv venv --python 3.11` → `scripts/install_deps.py` → `cp .env.example .env` 填 key。
再 `scout.py --initdb` 建库（幂等），最后 `scout.py --desktop` 开窗口，没有图形界面就用 `--chat`。
**不要 `pip install -e .`** —— 装了会连 `print(1)` 都跑不了，原因见 [QUICKSTART](QUICKSTART.md) 的 FAQ。

### ③ Docker（无界面：只有 CLI / JSON-RPC / 飞书机器人）

`cp .env.example .env` 填 key 后，`docker compose run --rm scout --initdb` 建库，`--chat` 终端对话，`docker compose up -d` 后台常驻。
数据落在命名卷 `scout-data` 里。容器内没有 GUI 也没有 WebView2，桌面窗口只能在宿主机跑。

---

## 本版亮点

1. **受限代码执行 `py.exec`**：模型能真的改你的 docx / xlsx / pdf，但跑在独立子进程 + Job Object + 审计钩子里，**你点头才执行**，永久授权随时可撤销。
2. **多源调研与引用核查**：检索 → 抓取 → 判权 → 合成，结论带出处；另有一步单独判「这条来源是否真的支撑这条结论」。
3. **技能系统不烧 token**：技能索引进库但**不常驻系统提示词**，模型按需 `skill.list` / `skill.load`；界面 `/` 菜单与模型看到的是同一份排序。
4. **本地全文检索 `kb.search`**：SQLite FTS5 + 中文逐字 n-gram，在**已收录**的资料里找，默认关闭、开了才真多出一个工具。
5. **看得见的现场**：日志落 `run/logs/scout.log`（轮转可配），密钥按真实值 + `sk-` 正则两道脱敏 —— 打包版「界面没反应」时不再无话可说。

---

## 已知限制

完整清单与逐条说明见 [CHANGELOG.md 的 Known Limitations 一节](CHANGELOG.md#known-limitations)，要点：

- **没有 JS 引擎** —— SPA / 动态渲染页抓不到正文，只拿得到空壳 HTML。
- **旧版 Office 格式需先转换** —— `.doc` / `.xls` / `.ppt` 不解析，请先另存为 OOXML。
- **单文件上限 20 MB** —— 超过即拒，不会先下完再算。
- **扫描件依赖 MinerU** —— 无文字层 PDF 走在线接口，免 token 接口按 IP 限频且可能整段不可用。
- **Docker 镜像无 GUI** —— 容器里只有 CLI / JSON-RPC / 飞书机器人。
- **微信 / QQ 未实现** —— 目前只有飞书一个渠道。

---

## 许可证

[MIT](LICENSE) © 2026 shiliyunma

## 反馈

- 提 Bug / 提需求：`<GitHub Issues 地址占位>`
- 安全或隐私问题请勿公开提 issue，走 `<私有联系方式占位>`

---

## 发布日执行（**命令仅供参考，本文件提交时不执行**）

> 打 tag 与建 Release 是**发布那一刻**的动作，和写文档不是同一件事，所以这里只留命令。

**前置：先把 Portable 打成 zip 并算 SHA256** —— Release 附件名要与下面命令里的
`ScholarScout-Portable.zip` 一致，且校验值要能对上，否则「下载即用」就变成「下载即报毒/损坏」。

```bash
# 0) 前置：打 zip + 算校验值（在项目根执行；产物不要入库）
cd "D:/AI+教育毕设毕设/ScholarScout"
( cd ScholarScout-Portable && zip -r ../ScholarScout-Portable.zip . )
sha256sum ScholarScout-Portable.zip > ScholarScout-Portable.zip.sha256

# 1) 确认要发的那笔就在 main 上，且工作区干净
git status --short          # 必须为空
git log --oneline -1        # 记下这个 SHA，写进 Release 正文

# 2) 打附注 tag（版本号与 pyproject.toml / src/scholarscout/__init__.py 一致 = 0.1.0）
git tag -a v0.1.0 -m "ScholarScout v0.1.0 — 首个公开版本（MIT）"

# 3) 推 tag（只推 tag，不推别的）
git push origin v0.1.0

# 4) 建 Release，正文用本文件，附件是 Portable 包与校验值
gh release create v0.1.0 ScholarScout-Portable.zip ScholarScout-Portable.zip.sha256 \
  --title "ScholarScout v0.1.0" \
  --notes-file RELEASE_NOTES-v0.1.0.md
```

打完 tag 之后**不要再改这一节** —— Release 正文是从这个文件读的，改文件不会改已发布的正文；
要修正文请用 `gh release edit v0.1.0 --notes-file RELEASE_NOTES-v0.1.0.md`。
