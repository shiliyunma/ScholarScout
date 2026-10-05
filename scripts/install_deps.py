"""按 pyproject.toml 装依赖，但**不装本包本身**。

**为什么不用 ``uv pip install -e .``**：项目路径含中文，editable 安装会在
site-packages 里写一个 UTF-8 的 ``.pth``，而 CPython 的 ``site.py`` 用 locale 编码
（中文 Windows 上是 GBK）读它 —— 解释器直接启动失败。详见 docs/03 §9。

依赖清单仍以 pyproject.toml 为唯一来源（本脚本读它，不另维护一份）。
CLI 入口用 ``python scripts/scout.py``（见该文件的说明）。

用法：
    python scripts/install_deps.py            # 运行期 + dev
    python scripts/install_deps.py --runtime  # 只装运行期
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def read_dependencies(with_dev: bool) -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    deps = list(project["dependencies"])
    if with_dev:
        deps += list(project.get("optional-dependencies", {}).get("dev", []))
    return deps


def venv_python() -> Path:
    for rel in ("Scripts/python.exe", "bin/python"):
        candidate = ROOT / ".venv" / rel
        if candidate.exists():
            return candidate
    raise SystemExit("找不到 .venv；先跑 `uv venv --python 3.11`")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="装依赖（不装本包本身）")
    parser.add_argument("--runtime", action="store_true", help="只装运行期依赖")
    args = parser.parse_args(argv)

    deps = read_dependencies(with_dev=not args.runtime)
    python = venv_python()
    cmd = ["uv", "pip", "install", "--python", str(python), *deps]
    print("执行：" + " ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
