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

- 提 Bug / 提需求：<https://github.com/shiliyunma/ScholarScout/issues>
- 安全问题请**不要**公开提 issue —— 走仓库 Security 页的「Report a vulnerability」：
  <https://github.com/shiliyunma/ScholarScout/security/advisories/new>

---

## 校验下载完整性

```text
文件：ScholarScout-Portable-v0.1.0.zip
大小：39.9 MB（39,883,274 字节）
SHA256：50d15029ac4add4706ded53685cf92bd6059a9e5db870577dcbbc94d5a0fc4f1
```

下载后自行算一遍，值一致才解压运行 —— 不一致说明下载不完整或被篡改，请重新下载。

Windows（PowerShell）：

```powershell
Get-FileHash .\ScholarScout-Portable-v0.1.0.zip -Algorithm SHA256
```

macOS / Linux：

```bash
sha256sum ScholarScout-Portable-v0.1.0.zip
```

---

