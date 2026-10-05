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
    from scholarscout.__main__ import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
