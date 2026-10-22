# 运行时环境与全库核验（讲义包的配套工事）

只在真正要动手装环境、写核验脚本时读。

## `03-env`：运行时目录必须 ASCII

**实测规则**：MSYS 工具（`ls`/`mv`/`unzip`/`curl`）读写中文路径**完全正常**；但**原生 Windows 程序会崩** ——
PostgreSQL 的 `initdb.exe` 放在 `03-环境` 这种中文路径下会把路径按 ANSI 码页转码成乱码，报：

```
initdb: error: file "E:/BoLiu/03-????/pgsql/share/postgres.bki" does not exist
```

而且它会把自己的**可执行文件位置**作为基准去推 `share/` 目录，所以你没法用 `-D` 绕开。

**结论**：放程序 / 运行时的目录用 ASCII（`03-env`），放文档的目录用中文没问题（`01-学习路线`）。
同一规则适用于任何内置字符串路径的本地程序。

## 免安装解压版 + 启停脚本

- 默认形态：官方 zip 解压到 `03-env\<组件>\`，**不写系统 PATH、不注册系统服务、不动 Program Files**。
- 每个组件一份 `start-*.bat` / `stop-*.bat` 放 `03-env\`，项目根再放一个 `启动所有服务.bat`
  依次拉起全部服务并打印状态检查（他要「入口放最显眼处」）。
- `.bat` 用 **GBK + `chcp 936`**（cmd 默认码页是 cp936，UTF-8 中文批处理会乱码）；
  `.sh` 用 UTF-8。这一条会让 `read_file` 把 .bat 认成二进制，属正常。
- **自己写 .bat：编码三件套 = GBK + `chcp 936` + CRLF，缺一不可。** 纯 ASCII 正文也完全可以，最皮实。
  写成 UTF-8 + 中文时，cmd 按 GBK 去解 UTF-8 字节 → 中文全变乱码 → **cmd 把乱码当命令去执行**，刷出一片：
  ```
  '鐗堬級' 不是内部或外部命令，也不是可运行的程序
  '瀵嗙爜' 不是内部或外部命令，也不是可运行的程序
  ```
  （`鐗堬級`=「版）」、`瀵嗙爜`=「密码」。）另一种表现是中间几段 `echo` **整段吞掉而 exit code 仍是 0**。
  UTF-8 + `chcp 65001` 是**不靠谱**的第三条路 —— cmd 常在 `chcp` 生效前就用旧码页解析完了。
  **任何用 UTF-8 写文件工具（`write_file`）产出的 .bat 都必须过一遍转码**，别自信「我这次写的是 ASCII」。
  写完断言：含 `\r\n`、且非 ASCII 行数为预期值。转码脚本见 `windows-local-service-ops` 的
  `scripts/to_gbk_crlf.py`。
- **等待循环：`timeout.exe` 写全路径也不行，要用全路径 `ping`。**
  `%SystemRoot%\System32\timeout.exe /t 1 /nobreak` 在 **stdin 被重定向**时（`< /dev/null`、管道、
  被别的脚本 `call`）直接报 `错误: 不支持输入重新定向，立即退出此进程`，于是循环**瞬间空转完 N 次**、
  报出「N 秒内没等到 PONG」这种**假失败**（服务其实正在启动）。正解：
  ```bat
  %SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul
  ```
  裸 `timeout` / 裸 `ping` 都不能用 —— 从 git-bash 调用时 MSYS 的同名程序会抢先命中 PATH
  （裸 `timeout` 报 `invalid time interval '/t'`）。同理适用于 `find` / `sort` 这类 MSYS 也有实现的命令。
- `05-环境清单与安装记录.md` 里每个组件都给：版本、位置、**真实启动日志**、以及「怎么算装好了」。
  没有真实输出的写「未验证」，**不许写成「已装好」**。

## 起后台服务：正确形态与几个坑

**不要把服务起成 agent 自己的受管后台进程（`background=true`）。** 它们会随 agent 进程一起被回收 ——
实测四个服务（PostgreSQL / NATS / Redis / Docker Desktop）在一段时间后**全部静默死掉**，
而用户完全看不出来。

**正确形态是「脱离 agent 的原生启动」**：`cmd /c` 调一个 ASCII+CRLF 的 `.bat`，里面用 `pg_ctl`
和 cmd 自己的 `start` 把进程 detach 出去；再给用户一个项目根下可双击的 `启动所有服务.bat`。

**启动器 .bat 的三条硬规则**（更细的实测表在 `windows-local-service-ops`，这里给要点）：

- **MSYS 编译的原生程序（`redis-server.exe` 日志里是 `os=MINGW64_NT-...`）只传相对文件名**，
  工作目录先 `cd /d` 切过去。给它传绝对路径会被吃掉反斜杠，**打不开配置就直接退出、一个字报错都不打**。
  实测：`start "t" /D "%RH%" "%RH%\redis-server.exe" "%RH%\redis.conf"` ❌  vs
  `cd /d "%RH%"` + `start "t" "%RH%\redis-server.exe" redis.conf` ✅。
  「启动脚本跑完却什么都没发生」先查这条。
- **启动前先查端口，已在跑就跳过**：`netstat -ano | findstr ":<port>" | findstr LISTENING >nul`
  再 `if %errorlevel%==0 (跳过)`。既避免重复启动，也避免 `pg_ctl` 在实例已运行时去开被占用的
  `pg.log` 而报 `Permission denied`。
- **「服务起了但一个字都不打」的第二嫌疑：厂商在配置里写死了它构建机上的日志绝对路径。**
  实测 `redis.conf` 的 `logfile` 指向 `F:\redis\...\bin\redis.log`（那个盘不存在），Redis 就在 bin 目录里
  建了一个**文件名就叫这个路径**的怪文件，stdout 全空。查「看不到任何输出」时先 `grep -i logfile <conf>`。

**验收 .bat：真跑，并让 `pause` 自动返回。**

```bash
cmd /c "E:\<路径>\启动脚本.bat" < /dev/null > out.txt 2>&1     # 再用 GBK 解码看 out.txt
```

`< /dev/null` 让结尾的 `pause` 立刻返回，代价是同时暴露上面那条 timeout 缺陷（正好）。
**别接管道**（服务会继承管道写端，见下条），也**别只靠「双击试试」** —— 双击时 stdin 是控制台，
好几类缺陷只在重定向或被 `call` 时才暴露。改完 .bat 要「重定向跑一遍 + 客户端往返」两样都做。

**查「用户新开的终端能认哪些命令」要读注册表里的持久 PATH**，不要用 agent 自己 shell 的 PATH ——
那里面被注入了 Hermes 自带的 node/python，于是你会把「node 可用」报给用户，而他双击新终端时没这个命令。

```bash
py -3.12 -c "
import winreg
for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
                  (winreg.HKEY_CURRENT_USER, r'Environment')):
    try:
        v, _ = winreg.QueryValueEx(winreg.OpenKey(hive, sub), 'Path'); print(sub, '->', v)
    except FileNotFoundError: pass
"
```

再拿那个 PATH 逐名 `shutil.which(name, path=...)` 确认。这就是「未全局注册程序完整路径」那张表的依据。

1. **别把 `pg_ctl start` 的输出接管道。** 服务进程会继承管道的写端，`| tail` 永远等不到 EOF →
   工具挂到超时；而工具超时会连带杀掉那个 shell 里的服务，于是下次连接直接 `Connection refused`。
   改成重定向到文件（`cmd /c "<start>.bat" > out.txt 2>&1`）—— 文件重定向不会卡。
2. **工具报「超时」不等于进程已结束。** 一次 `unzip` 被工具判超时，但 `unzip.exe` 还活着、持续往
   旧目录写文件，结果 `mv` 完目录又「自己长出来」。看到超时后先 `tasklist | grep <程序名>` 确认。
3. **验证服务活着必须用服务自己的客户端 —— `netstat` 会骗你。** 端口还 `LISTENING` 但服务其实已经
   崩了（postmaster 残留、或子进程初始化失败），此时一连就是 `server closed the connection unexpectedly`。

   | 服务 | 权威验证 |
   |---|---|
   | PostgreSQL | `PGPASSWORD=<pw> psql -h 127.0.0.1 -U <u> -tAc "select 1"` |
   | Redis | `redis-cli ping` |
   | NATS | `curl -sS http://127.0.0.1:8222/healthz` |

   核验脚本里的正则**也不要跨行**：`re.search(r":5432\b[\s\S]{0,60}LISTENING", out)` 会跨行匹配，
   于是四个全死的服务被报成「都在跑 ✓」—— 假阳性比不检查更糟。
4. **子进程异常退出码的诊断**：`pg.log` 里若出现
   `server process (PID x) was terminated by exception 0xC0000142`，
   `0xC0000142` = `STATUS_DLL_INIT_FAILED`（新进程加载 DLL 失败），诱因是**内存 / commit 压力**
   （实测物理内存只剩 2 GB / 负载 87% 时发生，机器上还跑着十几个容器）。
   处理：先腾内存再重启，**数据不会丢** —— 日志里会看到 `automatic recovery in progress`。
   同类症状值得先怀疑资源而非配置：端口活着、日志里没有配置错误、但一连就断。

另外：`npm run dev` 这类要杀的是 `netstat` 里**真正的监听 PID**（子进程），不是 npm 自己的 PID。

## 全库核验脚本该长什么样

留一份 `final_check.py` 并重跑，输出六块：讲义（份数/字节/讲数/六段计数/URL 数）、资料（文件数与字节 +
来源清单是否存在）、demo（README、expected-output）、顶层文件、服务端口、**待处理问题汇总**
（一个空列表才算完，这也是报告给用户的材料）。

五个固定坑：

- **讲义文件名前缀 / 资料目录名 / demo 目录名是三个不同的东西**（显示名可能是中文，目录名可能是短英文），
  分开传参。忘了这条就会出现「资料目录缺失」这种假报 —— 实测：按显示名 `PostgreSQL图数据库-AGE` 去找，
  而磁盘上叫 `PostgreSQL-AGE`。
- **「缺来源清单 / 缺 README」是快照**，报告前重跑一次，子Agent 可能刚写完。
- **netstat 在本机是 GBK 输出**，`subprocess.run(..., text=True)` 会 `UnicodeDecodeError`，
  要用 `capture_output=True` + `decode('gbk', errors='replace')`。
- **六段计数容忍附录多出 1-2 条**，否则格式正确的讲义被判成缺陷。
- **慢命令要限时**：`du -sh` 在带 `node_modules` / `.venv` 的目录上会跑到超时；全库 grep 也要排除
  `node_modules` / `.venv` / `.git`，否则几千个文件拖死循环。

## 凭据自查（每次跑过 LLM 服务后都做）

Key 从用户的 `.env` 读进 shell 变量，只打印长度自检（`echo "key loaded: ${#KEY} chars"`），
绝不写进任何讲义 / README / 日志 / 来源清单。收尾用真实值全库扫一遍：

```bash
K=$(grep -m1 '^<VAR>=' "<env-file>" | cut -d= -f2- | tr -d '\r"'"'"' ')
cd <root> && grep -rlF --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=.git "$K" . | head
```

命中 0 个才算过。这条要写进子Agent 的 brief（常有子Agent 主动做了，但父Agent 仍要自己复验）。
