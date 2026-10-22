---
name: windows-local-service-ops
description: "Start, stop, and verify local dev services on Windows."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [windows, msys, services, bat, ports, postgres, redis, nats, daemon]
    category: software-development
---

# Windows 本地开发服务的启停与守护

适用：本机的 PostgreSQL / Redis / NATS / ollama / 任意 dev server，以及围绕它们写的 `.bat` / `.sh`。
不覆盖「这个未知进程是什么」——那是 `windows-process-forensics`。

## 铁律一：服务的生命周期不能挂在 agent 的会话上

**不要用 agent 的后台进程能力去起用户要长期使用的服务。** 它随会话被回收，服务会在几小时后静默
消失 —— 而中间所有检查还都是绿的，用户拿着「都在跑」的报告去用，全部连不上。

要让它活下来，用这两种起法：

- **原生自守护**：`pg_ctl start`（fork 出 postgres 后自己退出，服务独立于 shell 存活）
- **cmd 的 `start` 把它 detach**：先 `cd /d "<工作目录>"`，再 `start "标题" <程序绝对路径> <相对参数>`
  —— 为什么要 `cd /d` 而不是给 `start` 传 `/D` + 绝对参数，见下面「.bat 编写规则」的实测表

然后给用户一个**双击即用**的入口脚本（起 + 停各一个），放在项目根目录最显眼处。

## 铁律二：验证用权威客户端，不要用 netstat

「端口在 LISTEN」**不等于服务可用**。服务半死（进程还在、接受连接后立刻关闭）时端口照样 LISTEN。
真实症状可以是同一个终端里上一行 `netstat` 显示 LISTENING、下一行 `psql` 报
`server closed the connection unexpectedly`。

用每种服务自己的客户端做一次真实往返：

| 服务 | 权威验证 |
|---|---|
| PostgreSQL | `PGPASSWORD=... psql -h 127.0.0.1 -U <user> -tAc "select 1"` → 期望 `1` |
| Redis | `redis-cli ping` → 期望 `PONG` |
| NATS | `curl -sS http://127.0.0.1:8222/healthz` → 期望 `{"status":"ok"}` |
| HTTP 服务 | `curl -sS -o /dev/null -w "%{http_code}\n" <url>` |

**正则写松了会给出跨行假阳性。** `netstat | grep -P ":5432[\s\S]{0,60}LISTENING"` 这类写法会跨行
匹配到别的端口，把已挂掉的服务报成「在跑」。要 grep 就按行匹配，且宁可多跑一次客户端往返。

## 铁律三：把已启动服务的输出接管道 = 永久挂起

```bash
cmd /c 启动脚本.bat | tail        # ❌ 挂到工具超时
cmd /c 启动脚本.bat > out.txt     # ✅ 重定向到文件
```

机制：被启动的 `postgres.exe` / `nats-server.exe` 会**继承管道的写端**，`tail` 永远等不到 EOF。
更坏的是工具超时会**连带杀掉那个 shell 里的服务**（它成了那个 shell 的子进程），于是表现为「起了又没了」。

**工具报超时 ≠ 进程结束。** 超时后进程常常还在跑 —— 一个 `unzip` 超时后继续往旧目录解压，
导致 `mv` 走的目录「又长出来」、删了又出现。看到超时先 `tasklist | grep <程序名>` 确认，再动那个目录。

## 铁律四：杀进程要杀 netstat 里那个 PID

`npm run dev`、`cmd /c start.bat` 都是**再起一个子进程**，监听端口的是子进程。杀启动器的 PID 端口不会释放：

```bash
netstat -ano | grep ":<port>" | grep LISTENING     # 最后一列才是要杀的 PID
taskkill /F /PID <PID>
```

本机 MSYS 路径转换是**关**的，所以写**单斜杠** `/F`；写 `//F` 会被原样传给 taskkill 并报「无效参数」。

## 一堆在跑的容器说不清来源时：读 compose 标签，别猜

用户直接贴 `docker ps` 问「这是你起的吗、干什么用的」，按这个顺序取证：

```bash
docker inspect <name> --format '{{json .Config.Labels}}'          # compose project / config_files / working_dir 全在里面
docker inspect <name> --format 'Created={{.Created}} Started={{.State.StartedAt}}'
docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' <name> ...
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' <name> ...
docker volume ls --format '{{.Name}}' | grep -i <project>
```

- `com.docker.compose.project.working_dir` + `config_files` 直接说出这套东西属于哪个目录，看一眼那个目录的 README/compose 就知道是谁、为什么起的；镜像标签 `org.opencontainers.image.description` 一句话说清项目是干什么的。再配一次会话检索（项目名）就能确认是哪次任务起的。
- **`Up 2 minutes` 不等于刚被人启动**：`restart=unless-stopped` 的容器在 Docker Desktop 一开机就全部起来。真实寿命看 `Created` 与 `State.StartedAt`。
- **回答「留不留」之前先看里面有没有数据**：进容器数一遍业务表（`docker exec <db> psql -U <user> -d <db> -c "select count(*) from <table>"`）。有数据就是跑过的成果，`down` 与 `down -v` 要分开给他，别让他一句话丢东西。
- **同一镜像多个 mirror tag 会重复占盘**：`docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}'` 里同尺寸同版本的几行就是冗余，`docker rmi` 掉多余 tag（留正在用的那个）。几十 GB 往往是这么攒出来的。
- 交付形状：一句话结论（谁起的、什么时候、为什么）+ 每个容器一行职责 + 当前健康证据（`/health`、前端 HTTP 码）+ 三条命令（保留 / `stop` 保数据 / `down -v` 连数据删）+ 内存与磁盘占用数字。

## 日志里的异常码先查含义，再决定要不要重启

`pg.log` 里的 `terminated by exception 0xC0000142` = **STATUS_DLL_INIT_FAILED**
（Windows 起新进程时 DLL 初始化失败），诱因通常是**内存 / commit 压力**，不是数据损坏。
处置顺序：先腾内存（停掉当时不用的容器 / 大程序），再启动；重启会走 `automatic recovery`，**数据不丢**。

诊断内存：`wmic` 在新版 Windows 上已被移除，用 Python 直接读：

```python
import ctypes
class M(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
m = M(); m.dwLength = ctypes.sizeof(M)
ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
print(f"物理可用 {m.ullAvailPhys/2**30:.1f} GB / {m.ullTotalPhys/2**30:.1f} GB  负载 {m.dwMemoryLoad}%")
```

## 运行时目录用 ASCII 名

原生 Windows 程序在**含中文的路径**下会崩：PostgreSQL 的 `initdb` 在 `E:\...\03-环境\` 下直接报
`share/postgres.bki does not exist`（路径被按 ANSI 码页转码成乱码）。而 MSYS 自带工具
（`ls` / `mv` / `unzip` / `curl`）读写中文路径是**正常**的。

结论：**放程序/运行时的目录用 ASCII 名**（`03-env`），纯文档目录可以继续用中文（`01-学习路线`）。

## .bat 编写规则

**编码三件套：GBK + `chcp 936` + CRLF，缺一不可。** 写成 UTF-8 正文 + 中文时，cmd 按 GBK 去解
UTF-8 字节 → 中文全变乱码 → **cmd 把乱码当命令去执行**，终端刷出一整片
`'鐗堬級' 不是内部或外部命令`（`鐗堬級`=「版）」、`瀵嗙爜`=「密码」）。另一种表现是中间几段 `echo`
整段消失、而 **exit code 仍是 0**。两条路都堵：**要中文就存成 GBK**；正文纯 ASCII 也完全可以，最皮实。
UTF-8 + `chcp 65001` 是**不靠谱**的第三条路 —— cmd 常在 `chcp` 生效前就用旧码页解析完了。
行尾必须 **CRLF**，LF 会让 cmd 逐行解析错乱。

**等待循环不要用 `timeout.exe`（写全路径也不行），用全路径 `ping`。**
`%SystemRoot%\System32\timeout.exe /t 1` 在 **stdin 被重定向**时（`< /dev/null`、管道、被别的脚本
`call` 进来）直接报 `错误: 不支持输入重新定向，立即退出此进程`，于是循环**瞬间空转完 N 次**、
报出「N 秒内没等到 PONG」这种**假失败**（服务其实正在启动）。正解：

```bat
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul
```

裸 `timeout` 和裸 `ping` 都不能用 —— 从 git-bash 调用时 MSYS 的同名程序会抢先命中 PATH
（裸 `timeout` 报 `invalid time interval '/t'`）。一律全路径。

**MSYS 编译的程序：配置只传相对文件名，工作目录先 `cd /d` 切过去。**
实测三种写法（`redis-server.exe` 是 MSYS 编译的，日志里 `os=MINGW64_NT-...`）：

| 写法 | 结果 |
|---|---|
| `start "t" /D "%RH%" "%RH%\redis-server.exe" "%RH%\redis.conf"` | ❌ 失败 |
| `cd /d "%RH%"` 然后 `start "t" "%RH%\redis-server.exe" redis.conf` | ✅ 成功 |
| `start /B ...` | ✅ 成功 |

它会把绝对路径里的反斜杠吃掉，打不开配置就**直接退出，一个字的报错都不打**。
「启动脚本跑完却什么都没发生」时先怀疑这条。

**启动前先查端口，已在跑就跳过。**

```bat
netstat -ano | findstr ":<port>" | findstr LISTENING >nul
if %errorlevel%==0 (echo 已经在跑，跳过启动) else (<启动命令>)
```

既避免重复启动，也避免 `pg_ctl` 在实例已运行时去开那个被占用的日志文件而报
`could not open log file "...pg.log": Permission denied`。

**「服务起了但一个字都不打」的第二个嫌疑：厂商在配置文件里写死了它构建机上的日志绝对路径。**
本机 `redis.conf` 的 `logfile` 指向 `F:\redis\...\bin\redis.log`，那个盘根本不存在 —— Redis 于是在
bin 目录里建了一个**文件名就叫这个路径**的怪文件，stdout 全空。查「看不到任何输出」时先
`grep -i logfile <conf>` 看日志实际被写去哪了。

**验收：真跑，并让 `pause` 自动返回。**

```bash
cmd /c "E:\<路径>\启动脚本.bat" < /dev/null > out.txt 2>&1     # 再用 GBK 解码看 out.txt
```

`< /dev/null` 让结尾的 `pause` 立刻返回；代价是它同时暴露上面那条 timeout 缺陷（正好）。
**别接管道**（见铁律三），也**别只靠「双击试试」** —— 双击时 stdin 是控制台，好几类缺陷只在
重定向/被 `call` 时才暴露。改完 `.bat` 用上面这条命令跑一遍 + 客户端往返验证，两样都做才算完。

## 宿主配置文件被工具拒绝写入时（JSONC）

VS Code 的 `settings.json` 是 **JSONC**（允许尾随逗号），而 `patch` / `write_file` 按**严格 JSON** 校验，
会以 `candidate content fails .json syntax validation` 拒绝写入。

做法：**先用脚本清掉尾随逗号，再改** —— 顺带把文件变成严格合法 JSON（两种解析器都认），是净收益。

```python
import json, re
src = open(P, encoding="utf-8").read()
src = re.sub(r",(\s*[}\]])", r"\1", src)     # 删尾随逗号
cfg = json.loads(src)                            # 这时才解析得动
cfg.update(NEW_KEYS)
open(P, "w", encoding="utf-8", newline="\n").write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
```

**改前先备份；改完和备份做一次逐键对比**（原有键一个没丢、一个没改，再确认新键在），别只看新键加进去了。

## Files

- `scripts/to_gbk_crlf.py` — 把一个（或一批）`.bat` / `.cmd` / `.ps1` 转成中文 Windows 能正确解析的
 形式：GBK + CRLF + `chcp 936`（纯 ASCII 的只补 CRLF）。**任何用 UTF-8 写文件工具产出的 `.bat` 都要
  过一遍它**，否则就是上面那条乱码当命令执行。含时间戳备份与 GBK 往返断言。
