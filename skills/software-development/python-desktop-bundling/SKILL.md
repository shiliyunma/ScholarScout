---
name: python-desktop-bundling
description: "Use when bundling a Python desktop app for Windows delivery."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [packaging, pyinstaller, pywebview, windows, desktop, delivery]
---

# 把 Python 桌面应用打成自包含包（Windows）

适用：GUI 应用（pywebview/Tk/Qt）要**交给别人**或**换台机器跑**。
本 skill 只收“构建成功但跑不起来、而且报错看不见”的那一类坑。

## 先分清两种交付物，别用一个方案套两种需求

| | 启动器（launcher） | 自包含包（bundle） |
|---|---|---|
| 依赖 | 本机 Python + venv | 无（解释器与依赖全带上） |
| 换机器 | 跑不起来 | 能跑 |
| 改代码后 | 不用重打包 | **必须重打包** |
| 用途 | 自己日常用/开发 | 演示、交作业、给别人 |

**两个都留着**。用户说“打包成安装包/点开即用”时要的是 bundle；别拿“bundle 每次都要重打包”当理由拒绝，
那只是启动器存在的理由，不是不做 bundle 的理由。产物目录进 `.gitignore`。

## 硬规矩（每条都对应一次“构建成功但打不开”）

### 1. 运行期非 `.py` 资源必须显式带上

PyInstaller 默认**只收 `.py`**。数据库迁移 `.sql`、模板、JSON、内置词表漏了，表现是**双击没窗口**
（应用启动时先建库→找不到迁移目录→异常退出，而 windowed 应用连报错都没地方显示）。

```bash
--add-data "src/pkg/state/migrations;pkg/state/migrations"
--add-data "src/pkg/desktop/ui;pkg/desktop/ui"
```

先枚举包内所有非 `.py` 文件（`find src/pkg -type f ! -name '*.py'`），逐个决定“打进去”还是“放 exe 旁边”。
**可改的资产（提示词/配置模板）放 exe 旁边**更好——用户还能自己调；**必须一致的资源（UI/迁移）打进包**。

### 2. 冻结态的数据目录 = exe 所在目录，不是解包目录

```python
def home() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent   # 数据/配置落在 exe 旁边（便携）
    return Path(__file__).resolve().parents[2]          # 源码运行
```

- `sys._MEIPASS`（onefile）是**临时解包目录**：往里写 `data/`、`*.db` 等于“重启一次资料全没了”。
- onedir 布局里 `__file__` 在 `_internal/` 下，恰好能算出正确项目根——**但别依赖这个巧合**，显式判 `frozen`。
- 验收：起一次应用，确认 `data/`、`run/*.db` 出现在 **exe 旁边**。

### 3. 冻结态的子进程要“拉起自己”，且不要注入源码路径

```python
if getattr(sys, "frozen", False):
    cmd = [sys.executable, "--rpc"]        # 引导器不认 `-m 模块`
else:
    cmd = [sys.executable, "-m", "pkg", "--rpc"]
```

- **PyInstaller 引导器不认 `-m 模块`**：`[sys.executable, "-m", pkg, ...]` 起出来的子进程会立刻退出 →
  界面能开、但页头红字“后端进程已退出”。
- **冻结态别把源码目录塞进子进程 `PYTHONPATH`**：打包目录里往往也有一份包源码，一旦挂上，
  子进程会去 import **源码**而不是包内模块，输出编码退回系统代码页（中文 Windows 是 GBK），
  父进程按 UTF-8 读就直接 `UnicodeDecodeError` → 读线程退出 → 界面显示“后端已退出”，而其实后端好好的。
- 父进程读子进程输出时加 `errors="replace"`：一个坏字节不该弄死整条通道。
- 子进程环境显式 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`。

### 4. windowed 应用没有控制台——用退出码与文件诊断

`--windowed` 下 stdout/stderr 都没了，**异常是静默的**。诊断顺序：

1. 给应用留几个**不开窗口的 CLI 子命令**（`--version` / `--initdb` / `--diagnose`），看**退出码**与落盘文件；
2. 手动以**同一条命令 + 同一份 env** 跑子进程（父进程怎么起它，你就怎么起），把 stderr 打出来；
3. 界面上能显示的错误（如“后端已退出”）只说明*通道*断了，别当成根因。

**同时把日志落到文件**：只写 stderr 的打包应用等于没有现场 —— 用户报"点了没反应"时你手上只有这一句话。
至少一个 `run/logs/app.log`（滚动），每条带 `session_id` / `turn_id`，并让自检命令能直接 `tail` 它。
这不是可观测性的加分项：没有它，上面 1–3 的诊断顺序全部失效。

### 5. 双击 = 零参数：冻结态的默认行为必须是“开界面”

CLI 的默认行为通常是“无参数 → 打印用法并退出”。打包后双击 exe 就是零参数运行，
于是**双击什么都不发生**。做法：`frozen` 且没有任何功能参数时，把 `--desktop/--gui` 打开；
源码运行时保持原行为（脚本与排障还要那行用法）。

### 6. 入口要放在交付目录的最外层

PyInstaller `--distpath X --name App` 会生成 `X/App/App.exe`（多一层）。用户找不到入口时，
把内容抹平到 `X/App.exe`（构建后 move 一层即可），**别让入口藏在子目录里**。

### 7. 重打包前清构建缓存，打完**核对包内资产**

改的是**打进包里的资产**（UI/模板/迁移）时，增量构建会沿用旧的暂存副本 ——
构建日志全绿，包里却还是改动前的版本。表现：新功能在源码里存在、在包里不存在。

```bash
# 先杀掉还在跑的实例（文件被占用会让构建中途失败），再清干净——spec 也是缓存
rm -rf build dist *.spec <App>-Portable
```

**别只看 “Build complete”**：给这次改动里的某个文件留一句唯一标记（新函数名、新注释都行），
打完 `grep` 包内那份拷贝：

```bash
find <App>-Portable -name app.js        # 路径可能是 _internal/<pkg>/... ，甚至嵌套两层
grep -c "<这次改动里的唯一字符串>" <包内路径>
```

两个信号都说明残留参与了收集，清掉重来即可：包内出现 `_internal/_internal/...` 这种**嵌套重复**；
以及整包体积异常（同一份代码，干净构建与带旧缓存构建可能差一倍）。

### 8. 内嵌网页的界面改完，要防“看的是旧资源”

pywebview/WebView2 一类内嵌浏览器会**按 URL 缓存** JS/CSS：改了前端、重启应用，跑的可能还是旧脚本
（无头浏览器探针却是新的，因为探针用的是源文件）。对策：给资源加版本查询串
`app.js?v=N` / `style.css?v=N`，并在脚本里放一个 `REV` 标记常量；
真机启动后先把标记读回来确认“跑的是哪一版”，再判断功能对不对。

## 验收清单（照抄，别凭感觉说“能跑”）

```bash
B=MyApp-Portable
$B/MyApp.exe --initdb        # 退出码 0；库文件出现在 $B 旁边
$B/MyApp.exe --version       # 退出码 0（证明包内解释器与依赖齐全）
$B/MyApp.exe                 # 模拟双击：不加任何参数
# 起窗口后核对进程数：壳 + 它自己拉起的后端 = 2 个进程
powershell -NoProfile -Command "(Get-Process MyApp -ErrorAction SilentlyContinue | Measure-Object).Count"
```

- **进程数 = 1 说明子进程没起来**（见第 3 条）；= 2 才是正常。
- 窗口核对用 `PrintWindow` 或按**进程名**取窗口（启动器与包内 exe 的进程名不同，截图脚本要两种都认）；
  现成脚本 `scripts/win_window_probe.py`（按进程名/标题找窗口、量标题栏与边框厚度、置前、抓图，只用标准库）：
  `GetWindowRect` 与 `GetClientRect` 的高度差就是标题栏+边框；抓图前先 `SetForegroundWindow`
  并**验证** `GetForegroundWindow()==hwnd`，否则拍到的是压在它上面的窗口；写 `.bmp` 行序要自下而上；
  旧实例还开着时按标题找会找到旧窗口，所以脚本按“启动前后窗口集合的差集”取目标。
  `.ps1` 脚本写**纯 ASCII**（PS 5.1 按 GBK 读无 BOM 文件会语法报错）。
- **驱动窗口逻辑优先走 JS 钩子，不要用合成鼠标事件**。留一个“启动后跑一段 JS”的环境变量口子
  （如 `MYAPP_DEBUG_JS`），让**真实窗口里的页面**自己调桥方法，再从外部观测**可测的副作用**：
  调“移动窗口”就量窗口坐标、调“改尺寸”就量宽高（两者都实测可精确命中）。
  反过来，`SetCursorPos + mouse_event` 的合成点击/拖拽只有当目标窗口**确实是前台**
  （先 `SetForegroundWindow` 并比对 `GetForegroundWindow()`）时才落得准，落在内嵌 webview 子窗口上
  还可能收不到事件 —— 症状是“功能没反应”而其实是**测法**失效，很容易误判成产品 bug。
  结论：**能用 JS 钩子替代的验证，就不要用合成输入**；合成输入只用于验“人真的点了会怎样”的最后一个环节，
  且失败时先怀疑测法。
- **能改变窗口行为的开关，默认值必须是“已在真机验证能用”的那一侧**。
  无边框/自绘标题栏/自绘缩放这类改动会接管系统的拖动与缩放：真机上验不过就不要默认开启，
  保留回退开关（配置文件里一个布尔 + 默认关闭）并在交付说明里写清楚怎么回退 ——
  一行标题栏换“窗口能拖能缩”是划算的交换，“没验过就先开着”不是。
- **界面探针全绿 ≠ 真机可用**：凡内容由真实桥/RPC 返回值决定的（窗口模式、权限、能否写），桩探针只会照着桩画。
  交付前必须在**打包后的 exe**上再看一次（见第 8 条的缓存问题：真机跑的可能仍是旧脚本）。
- **密钥不许靠纪律，要靠结构**：打包默认只拷 `.env.example`；真要拷真 `.env` 必须显式旗标
  （`--with-secrets`）且日志大声警告；并在构建末尾加**自检**：包里出现非空 `.env` → 构建失败、退出码非 0。
  这样"忘记改参数"不会变成泄密。已经发出去的包若含真密钥，当安全事故处理：重打包 + 换密钥 + 收回旧包，
  而不是只在交付说明里补一句"别外发"。
  **列目录查 `.env` 必须带 `-a`**：`.env` 是点文件，`ls <bundle>/` 看不见它 —— 一次实际发生的误判就是
  "列了十个文件、没有 .env，所以包是干净的"，而真 `.env` 就躲在里面。验收命令写死为
  `ls -a <bundle>/ | grep '^\.env'`，再对真 `.env` 逐个键判空（只打印键名，不回显值）。
- **那运行期密钥从哪来？从包外读，这也必须在文档里写明**：冻结后 `home()` = exe 目录，
  所以 `<exe 旁>/.env` 能被读到；进程环境变量优先级更高（dotenv 加载器通常"不覆盖已存在变量"）。
  交付说明写成：`cp .env.example .env` 填自己的 key，或者直接设环境变量 —— 两者都行，**包里永不需要**。
  另给一个 `HOME` 类环境变量（如 `MYAPP_HOME`）指向外部数据目录，密钥与数据都能离包更远。
- **独立入口要自己加载 `.env`**：不走主装配流程的 CLI（渠道长连接、自检、评测脚本）如果只依赖
  `os.environ`，会永远报"凭据未配置"而 `.env` 明明就在旁边——因为那个加载器只在主程序启动时调过。
  凡独立入口，第一行显式调用项目的 dotenv 加载函数；症状是"配置对、却始终报没配置"。
- **交付前比对包与源码的新旧**：`stat` 包内 exe 的时间 vs `git log -1 --format=%cI`（或 `src/` 最新 mtime）。
  比源码旧就说明交付的是旧代码 —— "构建成功"和"构建的是今天这版"是两件事。

## 常见反例

| 症状 | 真因 |
|---|---|
| 双击**没有任何窗口**，进程立刻消失 | 运行期非 `.py` 资源（迁移/模板）没打进包，启动即崩；windowed 看不到报错 |
| 界面能开，页头红字“后端进程已退出” | 子进程用 `-m 模块` 起（引导器不认），或父进程塞了源码 `PYTHONPATH` 导致编码崩 |
| 重启后“资料全没了”/换机器资料不见 | 数据目录写到了 `_MEIPASS` 或包目录，而不是 exe 旁边 |
| 双击没反应，命令行加参数才出窗口 | 零参数默认打印用法（第 5 条） |
| 找不到入口 exe | PyInstaller 多套了一层同名目录（第 6 条） |
