"""CLI 启动器：不依赖把本包装进 venv。

**为什么需要它**：项目路径含中文（``D:\\AI+教育毕设毕设\\...``），而 CPython 的
``site.py`` 用 locale 编码（中文 Windows 上是 GBK）读取 ``.pth`` 文件，uv/pip 的
editable 安装却把路径写成 UTF-8 —— 结果是解释器启动就崩，连 ``print(1)`` 都跑不了。
因此本项目**不做 editable 安装**，改用本启动器把 ``src`` 显式插进 ``sys.path``
（路径由文件系统 API 取得，是 Python 字符串，不受 locale 解码影响）。

用法：
    python scripts/scout.py --version
    python scripts/scout.py --initdb
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def main() -> int:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    # 编码统一**必须排在最前**：打包版（PyInstaller）的管道默认是系统 ANSI 代码页
    # （中文 Windows 上是 cp936），`PYTHONUTF8`/`PYTHONIOENCODING` 对冻结进程不生效，
    # 于是管道里一来中文就 UnicodeEncodeError（stdout 崩在写结果、stdin 的孤代理崩在请求体）。
    # 这里是源码与打包共用的唯一入口，所以改这一处两条运行方式一起生效。
    from scholarscout.stdio import force_utf8_stdio

    force_utf8_stdio()

    # 日志落盘**必须在任何业务 import/日志之前**：本启动器是打包版与脚本调用的统一入口，
    # 起不来的时候没有终端，`run/logs/scout.log` 是唯一的现场。setup_logging 幂等，
    # 所以 `scholarscout.__main__.main()` 里那次调用不会写出两份。
    from scholarscout.logging_setup import setup_logging

    setup_logging()

    from scholarscout.__main__ import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
