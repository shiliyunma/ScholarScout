"""把标准流钉成 UTF-8 —— 打包版 exe 的管道默认是系统 ANSI 代码页（中文 Windows 上 cp936）。

**为什么必须有这一层**（真机复现，不是推测）：

- **源码运行**：`PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` 由解释器读环境变量生效，
  管道里的中文一切正常 —— 所以这个缺陷在开发机上永远看不到；
- **发布包（PyInstaller onedir）**：解释器配置由引导器自己构造，这两个环境变量
  **对冻结进程不生效**。实测：显式 `PYTHONIOENCODING=utf-8 ./ScholarScout.exe --rpc`
  打出来的仍是 cp936 字节。于是 `rpc/server.py` 开头声明的"UTF-8，无 BOM"成了空话：
  - **stdout 侧**：`print(f"助手> {text}")` 遇到 emoji 直接
    `UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f44b'`，整轮崩在写结果那一步；
  - **stdin 侧**：UTF-8 字节被按 GBK 解（非法字节走 `surrogateescape` 变成 U+DCxx），
    这些孤立代理一旦进 JSON 请求体 / UTF-8 日志文件就是
    `UnicodeEncodeError: '\udcb1' ... surrogates not allowed`，用户输入既没发出去也没入库。

**谁来调**：`scripts/scout.py`（源码与打包共用的唯一入口）、`scholarscout.__main__`
各调一次 `force_utf8_stdio()`；`rpc.server.serve()` 再对传进来的两条流补一次
`force_utf8_stream()` —— 别的调用方（测试、将来的传输层）绕过入口时也不至于回到 cp936。

**为什么不改 `-X utf8` 或给 exe 加启动参数**：PyInstaller 的引导器不接受这种透传，
而且发布包是双击启动的，没有地方塞参数。入口里改一次是唯一位于两种运行方式**共同路径**上的位置。
"""

from __future__ import annotations

import io
import sys
from typing import Any

#: 标准流的统一编码：与 `rpc/server.py` 的传输约定、`logging_setup` 的日志文件编码一致
STDIO_ENCODING = "utf-8"

#: 解不出来的字节写成替换符而不是抛异常 —— **崩一轮对话**比丢一个字符严重得多
STDIO_ERRORS = "replace"


def force_utf8_stream(stream: Any) -> bool:
    """把一条**真正的**文本标准流重设成 UTF-8；返回是否真的改了编码。

    只认 `io.TextIOWrapper`，其余一律原样放过：

    - 打包版 windowed 进程在没有控制台时 `sys.stdout is None`；
    - pytest 的捕获替身、`io.StringIO`、`serve()` 收进来的 `iter(lines)` 都没有
      `reconfigure`，硬调就是 AttributeError；
    - 只碰解释器开出来的真流，测试替身与第三方包装（如渠道层的重定向）不被改写。

    `errors` 的兼容处理：`TextIOWrapper.reconfigure` 在 3.7+ 同时接受
    ``encoding`` 与 ``errors``，但替身实现/更老的运行时可能只认前者 —— 这时退化成
    只改编码（不抛），编码统一这一半仍然生效。
    """
    if stream is None or not isinstance(stream, io.TextIOWrapper):
        return False
    try:
        stream.reconfigure(encoding=STDIO_ENCODING, errors=STDIO_ERRORS)
    except TypeError:
        try:
            stream.reconfigure(encoding=STDIO_ENCODING)
        except (AttributeError, ValueError, OSError):
            return False
    except (AttributeError, ValueError, OSError):
        # 已 detach / 已关闭 / 不支持重设：按原样用，绝不因为改编码拦住启动
        return False
    return True


def force_utf8_stdio() -> None:
    """三条标准流一起重设。入口最先调它：之后的任何 print/input/日志都在 UTF-8 上。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        force_utf8_stream(stream)


def stdio_encodings() -> dict[str, str]:
    """当前三条流用的是不是 UTF-8（排障与回归用：打包版里一行就能看出真假）。"""
    return {
        name: (getattr(stream, "encoding", None) or "none")
        for name, stream in (("stdin", sys.stdin), ("stdout", sys.stdout), ("stderr", sys.stderr))
    }
