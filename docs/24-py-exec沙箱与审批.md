# 24 py.exec 沙箱执行与审批

> 建立 2026-10-22 ｜ 状态：**v0.1（后端完成，界面卡片未接）**
> 关联：`src/scholarscout/tools/py_exec.py`、`src/scholarscout/sandbox/{runner,approval,rules}.py`；
> 工具信封见 `01-接口契约.md` §5.0；落盘归属见 `03-目录结构与规则文件.md` §1.1；
> 缺陷记录见 `23-缺陷修复记录.md`。
> 测试：`tests/sandbox/test_runner.py`（10 条，真子进程）、`tests/sandbox/test_approval.py`（25 条）、
> `tests/tools/test_py_exec.py`（7 条，工具层信封）。

**一句话**：`py.exec` 让模型"写一段 Python 在指定工作目录里跑一遍"，用来改 docx/xlsx/pdf
这类**只有代码才改得动**的产物；它的主护栏是**运行前的人工确认**，沙箱只是把事故半径压小。

---

## 1. 为什么需要它

### 1.1 场景：产物是二进制容器，不是 Markdown

到 2026-10 为止，本项目的"写文件"只有 `note.save` 一条路 —— 它写 Markdown。
但用户真正要交的东西不是 Markdown：

| 场景 | 为什么现有工具做不到 | 代码类库 |
|---|---|---|
| 改论文 docx 的**第 3 段**字号/行距、替换某一节的表头、按模板插目录 | `note.save` 只会整篇写 Markdown；"改第 3 段" 这种**定位 + 局部改**在文字层无法表达 | `python-docx` |
| 给导出的 xlsx 台账**追加一列**、按条件着色、把多个 sheet 合并 | 同上；且 xlsx 是 zip + XML，手写字符串必坏 | `openpyxl` |
| pdf 拆页、合并、抽某几页的文本做校对 | 同上 | `pymupdf` |

这些操作共同点：**操作面是开放的**。`python-docx` 有几百个 API，`pymupdf` 更多；
每加一个"改 docx 的工具"只能覆盖枚举得到的那几种改法，永远差一种。
而"写段代码"能覆盖它们的**组合**（"遍历所有表格第 2 行，把日期格式统一成 YYYY-MM"这种，
没有工具能预设）。

### 1.2 于是问题变成：要不要让模型在用户机器上跑代码

两个都不能接受：

- **不许跑**：功能缺失，用户只能自己写脚本 —— 这正是"AI 帮他改文件"要解决的问题本身；
- **随便跑**：一段 `import shutil; shutil.rmtree("C:/")` 就够把毕设删了。

所以要的是第三条路：**能跑，但每一次都要用户点头，且跑的时候被关在一个盒子里**。
`py.exec` 就是这条路。它不是"给模型加了个 shell"，而是"给用户加了一个确认框，
确认之后才允许模型动那几个文件"。

### 1.3 已知前提（不装作没有）

- 模型写出的代码**经常有错**（路径写错、API 记错），所以失败必须**可读**：
  `stderr` 原样回、`violations` 逐条回，模型才有机会改对；
- 用户**不会读代码**。所以确认框里给的不是源码，而是 `data.risks` 折成的
  「要写 `exports/**`、要出网、要起子进程」这种**人话**（见 §5.2）；
- 一个能跑代码的把手**天然是高风险面**。本文档 §3 专门写"防不住什么"，
  不把"装了 audit hook"说成"安全了"。

---

## 2. 方案

### 2.1 四层，各管一件事

| 层 | 管什么 | 实现 | 代码 |
|---|---|---|---|
| ① 独立子进程 | **进程边界**：一次调用一个新进程，`cwd = workdir` | `sys.executable -I -c <driver>`；`-I` 拿掉用户 site 与 `cwd` 的 `sys.path` 注入；环境变量**白名单重建**（不带用户配置与 token），`TEMP` 指到本次临时目录 | `sandbox/runner.py::run_python` |
| ② Windows Job Object | **资源上限 + 杀一整棵树**：内存、进程数、关闭即杀 | 纯 `ctypes`（零新依赖）：`JOB_OBJECT_LIMIT_PROCESS_MEMORY` / `_ACTIVE_PROCESS` / `_KILL_ON_JOB_CLOSE`；超时或父进程收尾时 `TerminateJobObject` | `sandbox/runner.py::JobObject` |
| ③ 子进程内 audit hook | **调用级闸门**：越界读写、网络、子进程、原生调用 | `sys.addaudithook`，先于用户代码装上；`open` 判**规范化后的真实路径**；被拦下记进 `violations` 并当场 `RuntimeError` | `sandbox/runner.py::_DRIVER::_install` |
| ④ 人工确认 | **主护栏**：跑之前用户点一下 | 两段式：第一次不带 `approval` → `ok=false` + `kind=needs_approval` + `risks` + 四个选项；界面回填 `approval` 后**重新调用同一工具**才执行 | `tools/py_exec.py` + `sandbox/approval.py` |

这四层**顺序也有意义**：确认在最前（不确认就不起进程），资源上限在起进程时立刻生效
（跑飞了才想起来设就晚了），audit hook 在用户代码之前（装上之后再 `exec`）。

### 2.2 实测数字（本机 Windows 11 + Python 3.11 跑出来的）

| 断言 | 实测 | 出处 |
|---|---|---|
| **128MB 上限下要 600MB 会失败** | `bytearray(600*1024*1024)` → `MemoryError`，`returncode=1`，**266ms**，父进程照常拿到结果（`killed=False`） | `test_runner.py::test_memory_limit_kills_the_child_but_not_the_parent`（同款片段手工再跑一次核对） |
| **超时杀的是整棵树** | `while True: pass` + `timeout_s=2` → `timed_out=True, killed=True`，**2047ms**；另一条用例让父进程再起孙进程，`job.terminate()` 后**两个 PID 都消失**（`_wait_gone` 轮询确认） | `test_runner.py::test_timeout_kills_the_process_tree` |
| **`../` 逃逸被拦** | `open("../escaped.txt", "w")` → `ok=False`，`violations=[{"event":"open","detail":"w ../escaped.txt"}]`，且**外面那个文件真的没被创建** | `test_runner.py::test_dotdot_escape_is_blocked` |
| **符号链接逃逸被拦** | 工作目录里 `link.txt` → 指向外面的 `secret.txt`，`open("link.txt").read()` → `ok=False`，`violations=[{"event":"open","detail":"r link.txt"}]` | `test_runner.py::test_symlink_escape_is_blocked`（本机开发者模式能建链接，不跳过） |
| **网络/子进程/原生各拦一次** | `socket.socket()` / `subprocess.run(...)` / `ctypes.CDLL("kernel32")` 三例全部 `ok=False` 且 `violations[0].event` 分别以 `socket.` / `subprocess.` / `ctypes.` 开头 | `test_runner.py::test_network_subprocess_and_native_are_blocked` |
| **工具层回包不抛** | 超时 / 内存超限都是 `ok=false` + `data.kind="failed"` + 非空 `error`（超时那条 `error` 里带"超时被杀"），异常一条都没冒到调用方 | `tests/tools/test_py_exec.py` |

### 2.3 为什么判据是"规范化之后"而不是"字符串里有没有 `..`"

字符串判据永远漏：`..%5c`、`C:\WINDOWS`、`c:/windows/`、junction、`subst`、
大小写变形、UNC 路径……列不完。所以判据只有一条：

```python
real = os.path.normcase(os.path.realpath(os.fspath(path)))
allowed = real == root or os.path.commonpath([real, root]) == root
```

`realpath` 把 `..`、符号链接、junction 全部折到真身，`commonpath` 判"是否在这个子树里"。
于是 §2.2 表里那两条（`../` 与 symlink）**不需要任何特判**就都拦住了 —— 它们是同一条规矩的两个例子。

同一套判据在**两处**都用了：静态侧（`approval.classify` 判风险范围）与运行时侧
（audit hook 判实际调用）。两处故意**不共享代码**：运行时那份跑在子进程里、
只能依赖标准库（`-I` 下没有本项目），共享就得把包塞进子进程的 `sys.path` —— 那等于为了省几行代码扩大攻击面。

### 2.4 输出与成败口径

- `ok` 的判据是**进程正常退出（`returncode == 0`）且没被超时/内存杀掉**，不是"stdout 非空"：
  写了半天没打印但文件改对了，也是一次成功的执行；
- `stdout` / `stderr` **分开回**，各自最多 20000 字符（超出截断并置 `truncated=true`）——
  死循环里 `print` 是最常见的炸法，管道不能无上限往内存里灌；
- `violations` 非空 → 一律 `ok=false`（`data.kind="blocked"`），**即使代码自己吞掉了那个 `RuntimeError`
  并且退出码是 0**：被拦下过就说明这次执行的结果不可信，不能报成功；
- 中文输出：`-I` 会忽略所有 `PYTHON*` 环境变量（含 `PYTHONIOENCODING`），所以编码只能在驱动脚本里
  `stream.reconfigure(encoding="utf-8")` —— 不改的话中文 Windows 的管道按 cp936 走，中文输出乱码。

---

## 3. 能防什么 / 防不住什么

### 3.1 能防（这次真的拦住了，有测试）

| 类别 | 具体行为 |
|---|---|
| 模型随手写错路径 | 读 `C:\Users\...\Desktop\x.docx`、写 `../../x`、往 `C:\Windows` 写 —— 拦下并给出可读理由 |
| 顺手装包/出网 | `pip install`（要起子进程）、`requests.get`（要 socket）—— 拦下 |
| 绕过 Python 层 | `ctypes.CDLL` / `cffi` —— 静态侧硬底线直接拒，运行时侧 `ctypes.*` 审计事件再拦一次 |
| 跑飞 | `while True: pass`（超时杀树）、吃内存（Job Object 上限）、刷屏（输出截断） |
| 不确认就跑 | 第一次调用**永远**只回 `needs_approval`，代码一行都不执行（`test_py_exec` 断言文件没被创建） |
| 自己给自己发许可证 | `approval` 由界面回填；代码里出现 `run/sandbox_rules.json` 是硬底线（拒），改不了规则表 |
| 反复试探 | 同会话连拒 3 次后熔断，第 4 次起直接拒、不再询问 |

### 3.2 防不住（**这是本文档最该读的一段**）

1. **它不是安全边界**。audit hook 是"同一个进程内的一道 Python 级检查"，
   能被**刻意**绕开。最直接的一条：把编译好的 `.pyd` / `.dll` 放进工作目录再 `import` ——
   那是一次普通导入，没有任何审计事件；`ctypes` 被拦，但**已经编译好的原生代码**不被拦。
2. **"用户在确认框里点了允许"才是主护栏**。这套东西的威胁模型是
   **"模型会写错/写歪，用户会看一眼"**，不是"有恶意代码要跑出去"。
   真要跑不受信任的代码，得用容器/虚拟机/独立账户 —— 本项目不做，也做不到。
3. **磁盘没有配额**。内存有 512MB 上限，工作目录里写满 10GB 是**允许的**（那是它该干的事）。
4. **CPU 只靠墙钟超时**：单次最多 120s（`MAX_TIMEOUT_S`），但**没有跨调用的额度** ——
   连点十次确认就是十次 120s。
5. **网络是在 Python 层拦的**：`socket.*` 审计事件挡得住 `socket`/`urllib`/`requests`，
   挡不住"原生代码里的 winsock 调用"（同第 1 条）。所以**不要把它当断网环境**。
6. **进程数上限是 job 级的（4 个）**，线程不限；一个进程开 500 个线程照样能把机器拖慢。
7. **批过的范围不再问**：一旦"永远允许写 `exports/**`"，此后在该目录里写**任何**文件都不再弹确认。
   范围是按目录算的，不是按文件 —— 这是为了可用性做的取舍（否则每个文件名都要点一次）。
8. **界面还没接**：`tools/py_exec.py` 已经把四个选项与 `risks` 交出来了，
   但桌面端**还没有渲染确认卡片的代码**（`grep -rn "needs_approval" src/desktop` → 0 处）。
   当前链路上没有东西会回填 `approval`，所以工具**只会回 `needs_approval`**，一次都不会执行。
   这是"后端完成、界面未接"的如实状态，接的时候只需渲染 `data.options` + 把选择回填成
   `approval` 重调同一工具，**不需要改沙箱**。

---

## 4. 硬底线清单（任何授权都不放行）

硬底线的意思是：命中即**终局**。它不看 `approval` 参数、不读规则表、不分 `session`/`always`，
连"问用户一句"都不问。

| # | 命中 | 拒绝理由（大意） | 为什么不可协商 |
|---|---|---|---|
| 1 | 代码里有系统目录字符串：`C:/Windows`、`C:/Program Files`、`C:/ProgramData`、`/$Recycle.Bin`、`/etc`、`/usr`、`/bin`、`/sbin`、`/boot`、`/lib`、`/var`、`/dev`；或 `%WINDIR%` / `%SystemRoot%` / `%ProgramFiles%` / `$env:windir` 这类变形 | "这些位置一律不许 py.exec 触碰" | 改系统文件是**不可逆**的机器级损坏，一次误操作就能让系统起不来；这类需求本来就不属于"改用户的产物" |
| 2 | 凭据文件：`.env`、`*.env` | "`.env` 一律不许 py.exec 读写" | 项目自己的 `.env` 存 API key。让模型能读它 = 让模型能把 key 写进回答、日志、外发内容 |
| 3 | 审批规则文件 `run/sandbox_rules.json` | "不允许 py.exec 给自己发许可证" | 否则审批门可以被自己改写成"全部允许"，整套机制失效 |
| 4 | `import ctypes` / `cffi` / `_ctypes`，或字符串里出现 `ctypes`（`__import__("ctypes")` 这种形式） | "那等价于任意代码执行，任何授权都不放行" | 见 §3.2 第 1 条：原生调用能绕开所有 Python 级检查，因此必须在静态侧就拒 |
| 5 | 删除类调用（`rmtree`/`remove`/`unlink`/`rmdir`/`removedirs`）的目标是**工作目录本身或其上级**，或**工作目录之外** | "删除不可逆，一律不放行" | 删一个文件可以是误操作，删掉工作目录/它的上级是**整个毕设**。工作目录内的删除仍可审批放行 |
| 6 | 代码**无法解析**（`SyntaxError`）、**空代码** | "不能在无法静态检查的情况下执行" | 硬底线靠 AST 做，解析不了就等于"看不出它要干什么" —— 那就不跑 |

**"无法绕过"是代码级保证，不是承诺**：`hard_floor()` 的返回类型是 `str | None`
（拒绝理由或 `None`），**没有"允许"这个值**；`ApprovalGate.check()` 在读规则**之前**调它，
命中就 `return Decision(kind="rejected", ...)` —— 放行分支在命中之后**根本到不了**。
`tests/sandbox/test_approval.py` 里每条硬底线样本都是这么打的：**先把全类别
（`exec/write/delete/read/network/subprocess/native/outside_path/dynamic` 的 `@any` 与具体范围）
永久授权，再传 `approval="always"`，断言仍然 `rejected`**；
工具层同一件事再验一遍（`tests/tools/test_py_exec.py::test_hard_floor_rejects_even_when_every_category_is_pre_authorized`）。

---

## 5. 审批

### 5.1 四个选项

界面按 `data.options` 的顺序渲染（`approval_options()` 是唯一来源）：

| 值 | 按钮 | 语义 | 留痕 |
|---|---|---|---|
| `deny` | 不许执行 | 这一次不跑；**计入连拒**（熔断用） | 无 |
| `once` | 只允许这一次 | 这一次跑，**不留痕** —— 下次同样的代码还要再问 | 无 |
| `session` | 本会话内允许同类操作 | 记在**内存**，本会话内同类×同范围不再问；进程退出即消失 | 不落盘 |
| `always` | 永远允许同类操作（内容级风险只到本会话） | 写进 `run/sandbox_rules.json`，此后跨会话有效 | 落盘 |

**第一次调用不带 `approval`**，工具回 `ok=false` + `kind=needs_approval` + `risks` + 四个选项；
界面拿这四个选项问用户，**用户点完由上层带着 `approval` 重新调用同一个工具**才执行。
模型自己填 `approval` 是无效的（提示词第 2 条明写"不要自己填"）：`approval` 是**用户的选择**，
不是模型的参数 —— 模型能自己给自己发许可证的话，这道门就等于不存在。

### 5.2 记忆维度是 `类别:范围`，不是代码字符串

`classify()` 把代码折成「**操作类别 × 路径范围**」的风险清单：

| 键 | 含义 |
|---|---|
| `exec:@cwd` | 在工作目录里执行 Python（每条都带，`@cwd` = 就是那个工作目录） |
| `write:exports/**` | 写 `exports/` 下的文件 |
| `write:@cwd` | 写工作目录根下的文件 |
| `read:...` / `delete:...` | 读 / 删（范围同 write） |
| `network:@any` / `subprocess:@any` / `native:@any` / `dynamic:@any` | 内容级风险，范围不可穷举 |
| `outside_path:outside:C:/Users/x` | 碰工作区外的绝对路径（范围是那个目录） |

**为什么不按代码字符串存**：一模一样的 `doc.save("out/a.docx")`，参数从 `a.docx` 换到 `b.docx`，
哈希就变了 —— 规则永远命中不了，用户每次都要点。而"允许写 `exports/` 下的文件"这条**意图**
在文件名变的时候本来就该继续有效。反过来，按类别记也意味着
"允许写 `exports/`" **不会**顺手允许"在 `exports/` 里起子进程" —— 类别是规则的另一半。

范围匹配（`rule_covers`）是**前缀式**的：`write:@any` 覆盖一切写；
`write:exports/**` 覆盖 `write:exports/**` 与 `write:exports/sub/**`，但不覆盖 `write:@cwd`。

### 5.3 内容级风险降级为 `session`

`network` / `subprocess` / `native` / `outside_path` / `dynamic` 这五类**不许永久放行**：
它们的范围不可穷举（"永远允许出网"等于"永远允许把任何数据发到任何地方"）。
所以用户点 `always` 时，只要风险清单里有任何一条内容级风险，就**降级成 `session`**，
并在回包 `data.approval.downgraded=true` + 理由里说清楚。
（照搬 Hermes 的做法：这类"范围说不清"的授权最多只到本会话。）

### 5.4 熔断

同一会话**连续**被拒 3 次（`MAX_CONSECUTIVE_DENIALS`）后，第 4 次起**连问都不问直接拒**，
理由写明"审批熔断：这类操作在本次会话内不再询问"。

两个细节：

- **成功放行即清零** —— 熔断罚的是"反复撞墙"，不是历史累计；
- **`needs_approval` 不算拒绝** —— 否则模型第一次问就被记一笔，问三次就熔断，等于不许问。

### 5.5 拒绝文案统一带一句"别绕"

所有拒绝（硬底线、`deny`、熔断）都在理由末尾带
`REFUSAL_TAIL = "不要换写法重试，也不要换条路达成同一目的。"`。
原因很实际：模型被拒后最常见的反应是换一种写法再试（`open` 换成 `os.open`、`Path.write_text`
换成 `shutil.copy`），把一次拒绝拖成十次。同一句话也写进了系统提示词
（`runtime/prompts.py::PY_EXEC_RULE` 第 3 条），并明确"把这个限制如实告诉用户"。

---

## 6. 为什么规则存在 `run/sandbox_rules.json`，不写 `config.yaml`

| 维度 | `config.yaml` | `run/sandbox_rules.json` |
|---|---|---|
| 谁改 | **人手写**（带注释、按主题分组） | **程序在运行中自己增删** |
| 改的代价 | 改完要人审、要重启（部分键） | 立即生效，用户点一下就写 |
| 内容性质 | 声明式配置（"我要什么环境"） | 运行状态（"这次运行里发生过什么"） |
| 归属 | 仓库里的 `config.example.yaml` 是模板 | `run/` 已经是运行状态的归属地（`run/state.db`、`run/logs/`） |

三条具体理由：

1. **程序改写 YAML 会吃掉注释与键序**。`config.yaml` 是用户手写、带解释性注释的文件；
   用 `yaml.dump` 回写会把注释全丢、键按字母序重排。下次用户打开看，会以为自己写错了。
2. **"配置要人审"的规矩会被打破**。`项目规则.md` 的取向是配置变更由人发起；
   而审批规则天生是**程序写、用户点**的。把两者塞进同一个文件，等于让程序去改一个人审的文件。
3. **坏文件的后果不同**。配置坏了应该拦启动（人看得见、改得回）；
   规则表坏了应该**降级为空表**（`RuleStore.load` 对缺失/坏 JSON/未知版本一律返回 `[]`）——
   审批门不能因为一个 JSON 文件坏了就打不开。同一个文件里放两种故障策略是错的。

配套细节：

- **原子写**：`mkstemp` 写临时文件 + `os.replace`，半个 JSON 不会把审批门卡住；
- **格式带版本**：`{"version": 1, "rules": [...]}`，换格式时**拒绝旧文件**而不是猜；
- **只存落盘级别**：`session` 在语义上"本会话有效"，所以 `PERSISTED_LEVELS` 里虽然列了它，
  实际只有 `always` 会落盘（`ApprovalGate` 里 `session` 只进内存字典）；
  `once` 连记录都不产生（`RuleStore.grant` 对 `once` 直接抛"不落盘的级别"）；
- **每次读都回磁盘**：设置界面看到的就是文件里的真值，没有"内存里改过但没写"的中间态。

---

## 附：还没接完的（如实记录）

1. **桌面端确认卡片**（§3.2 第 8 条）：`data.options` / `data.risks` 已经按界面需要组织好了，
   缺的是前端渲染 + 把选择回填成 `approval` 的那一次重调；
2. **设置界面的规则列表/撤销**：`ApprovalGate.list_rules()` 与 `revoke()` 已实现（永久 + 本会话两条来源分开给），
   没有 RPC 方法暴露出去；
3. **磁盘配额**：没有（§3.2 第 3 条），要做得在 Job Object 之外加一层（Windows 的磁盘配额按用户/卷算，粒度太粗）；
4. **跨调用的 CPU 额度**：没有（§3.2 第 4 条），要做得在工具层记账（与 `max_tool_calls_per_session` 同类）。
