---
name: repo-code-map-docs
description: "Use when 给仓库做目录级文档/代码地图。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [documentation, code-map, readme, diataxis, adr, stale-docs, scholarscout]
    category: software-development
---

# 目录级文档 / 代码地图（按业界规范，不是按直觉）

触发场景：用户说"给每个目录写文档 / 目录说明 / 文件关系 / 术语表 / 代码地图"，
或仓库文档开始与代码不一致。

人最容易犯的错是"每个目录写一堆手写 md"；业界主流做法恰好相反：**少而准、不重复、
能生成的绝不手写**。本 skill 是那套做法的可执行版本。
一手出处与逐条原话：`references/industry-sources.md`（Diátaxis / Chromium / C4 模型 / ADR）。

## 三条硬规则（顺序不能反）

1. **会腐烂的一律生成，不手写**：文件清单、行数、导出符号、依赖方向、目录树。
   依据：C4 模型官网 Code diagram 页 "Ideally this diagram would be automatically generated
   using tooling"；Chromium 规范 "Duplication is evil"。
2. **不随代码变的那部分才手写**：这一层是干什么的、为什么这么切、被放弃的替代方案、改它的影响面。
3. **同一份信息只存一处**：术语表**全局一份**，各目录链接过去，别按目录切。
   实测：`source_id` 会横跨 8 个 src 子目录、`tier` 6 个 —— 按目录切 = 同一个词解释 8 遍。

## 落地结构（四个产物）

| 产物 | 谁写 | 内容 |
|---|---|---|
| 每个重要目录的 `README.md` | 手写 | 按 Chromium 的四问，见下 |
| `docs/代码地图/00-符号清单.md` | **脚本生成，禁止手改** | 文件/行数/docstring 首句/顶层符号/依赖 |
| `docs/代码地图/01-全局术语表.md` | 手写，**唯一副本** | 术语 → 定义 → 代码名 → 权威出处 → 涉及目录 |
| ADR（决策记录） | 手写 | 一条决策 + 理由 + 代价；**唯一完全不会腐烂的手写文档类型** |

### README 按 Chromium 的四问写（别自己发明结构）

1. What is this directory intended to hold?（这一层放什么、职责边界）
2. Which files should the developer look at first?（阅读顺序，带一句为什么先读它）
3. Are some files an API?（外部可依赖的契约：函数名/签名，破坏它要同步哪些调用方）
4. Who maintains this directory and where can I learn more?（维护者 + 权威文档/测试/题集入口）

**再补两条必需项**："改这里之前先知道"（坑与不变量）与"非目标"（哪些活不在这层做）。
**README 里禁止**：行数、文件数、依赖列表、阈值表 —— 全部指向生成物或权威副本。

### 生成器的设计约束（漏一条就会被反噬）

- **纯标准库 `ast`**：新增依赖在本类项目里要审批（白名单门禁），生成文档不该引入运行期依赖。
- **输出必须确定**：目录/文件排序，**禁时间戳、禁本机绝对路径** —— 否则每次生成都是脏 diff，
  且把用户机器路径泄漏进仓库。
- **必须带 `--check` 模式**：与磁盘内容逐字比较，不一致退出码 1。这是"文档漂移"能被门禁抓到的唯一手段；
  没有它，生成物照样会悄悄过期。
- **坏文件显式暴露**：`ast.parse` 抛 `SyntaxError` 时写进清单（"解析失败：…"），不许静默跳过。
- **无 docstring 要报警**：代码本体的模块 docstring 就是"这个文件干什么"的唯一不腐烂位置，
  缺失要标 `⚠️ 无模块 docstring`，并考虑在 lint 里开启 `D100`。

## Pitfalls（都真踩过）

- **别用 `_map/` 之类自造目录名代替 `README.md`**：IDE、GitHub 都会自动渲染 `README.md`，
  自造目录名谁都不会认。"文件在码边"与"仓库干净"并不矛盾 —— README 本来就该入库、随代码同一提交更新。
- **别把"目录说明"和"文件关系"拆成两份手写文件**：两者都是"导航级"信息，内容必然重叠；
  Diátaxis 的告诫是四类文档不许混写，reference 和 explanation 混在一起会四不像。
  导航归 README，清单归生成物。
- **别为 docstring 已有的内容再写一份 md**：先 `ast` 统计有多少模块缺 docstring —— 往往是 0 个；
  那"各文件的作用"就已经写好了，再写一份就是纯重复。
- **README 里不要复述阈值/公式/条数**：改代码时没人会记得同步它。真实事故：`docs/05` 写
  "E2 12 条"而文件是 13 条、写"40 条规则"而实际 45 条。**数字只活在自动生成的报告里**。
- **测试里用 `importlib` 动态加载被测脚本时，必须 `sys.modules[spec.name] = module`**：
  目标脚本若有 `from __future__ import annotations` + `@dataclass`，dataclasses 要靠
  `sys.modules[cls.__module__]` 解析字符串注解，不注册就抛
  `AttributeError: 'NoneType' object has no attribute '__dict__'`。
- **新脚本先过 `ruff format`**：项目的 `check.sh` 里有 `ruff format --check`，格式不对直接算失败。
- **测试断言里别写"路径+文件名"**：生成物把目录放在标题、表格里只写文件名，断言要按实际形态写。

## 验收（缺一条就不算做完）

```bash
export PATH="$PWD/.venv/Scripts:$PATH"                       # 不导出会有 4 项 SKIP 假绿
.venv/Scripts/python.exe scripts/make_code_map.py            # 生成
.venv/Scripts/python.exe scripts/make_code_map.py --check    # 必须退出码 0
bash scripts/check.sh                                        # 只认"失败 0 项，跳过 0 项"
```

报告时给出**真实数字**（多少个文件、多少行、几个用例通过、门禁汇总行） —— 不说"应该没问题"。

## 参照实现

`D:\AI+教育毕设毕设\ScholarScout` 是这套做法的落地样例：
`scripts/make_code_map.py`（生成器 + `--check`）、`tests/scripts/test_make_code_map.py`（7 个回归用例：
稳定输出 / 无绝对路径 / 坏文件暴露 / 过期检测）、`src/scholarscout/grading/README.md`（四问样板）、
`docs/代码地图/01-全局术语表.md`（全局唯一 + 脚本量出的"涉及目录"）。生成物实测覆盖
133 个 Python 文件、26395 行；全量门禁零失败零跳过。

## 核验（并行铺开时必做）

铺 8 份以上 README 时用子 Agent 并行是对的，但**它们汇报的“已完成”不是事实**。核验三件：

1. 文件真存在、行数多少、**且没有误改别的文件**（`git status --short` 除自己新增的外不应多出条目）。
2. **符号存在性校验**：把 README 里反引号提到的每个标识符抽出来，验证它在该目录或全仓库代码里真实存在。
   写这个校验器时，下面五类**必须排除**，否则满屏误报（第一版报了 71 处“可疑”，收紧后剩 0 处真实问题）：
   - 占位符与通配：`NNNN_*.sql`、`.docx/.xlsx/.pptx`、含 `*` `<>` `…`
   - 带章节号的文档引用：`docs/03 §2`（不是文件路径）
   - **包内相对路径**：`state/db.py`、`tools/source_fetch.py` —— 要再按 `src/scholarscout/` 前缀试一次
   - **无扩展名的测试文件名**：`test_http`（文件名不在文件内容里）
   - 内置异常名：`KeyError` / `TypeError` / `ValueError`
3. 子 Agent 自己报的“不确定”清单要逐条去核 —— 本轮它们揪出的三件真问题
   （示例配置里遗留的限速项、过时的模块 docstring、缺 `__init__.py` 的包）**都不是我教的**，是它们读代码读出来的。
   推论：给子 Agent 的任务里可以带已知的坑，但要求它“去代码里核实后再写”比自己复述一遍值钱。

## 生成物会当场过期（把 `--check` 接进门禁的最强论据）

实测：`00-符号清单.md` 生成于 10:49；**10 分钟后**并行开发的人新增了
`src/scholarscout/fetch/formats.py`（11:01:47），`make_code_map.py --check` 立刻退出码 **1**，
文件数 133 → 134。
—— 这就是陈旧文档的真实速度。**人眼不可能发现** 248 行清单里少了一个文件；门禁可以。

## docstring 门禁：先量再开（往往是零成本）

开 `D100`（模块 docstring 必填）前必须先量：实测 `src`+`tests`+`scripts`+`eval` 共
**273 个 py 文件，缺模块 docstring 的 0 个** → 开规则当天**零报错**，收益纯防御性。
**只开 `D100`，不要开整个 `D` 族**：D2xx 的格式规则会与中文 docstring 的全宽标点打架
（而中文项目通常已 ignore `RUF001-003`）。
