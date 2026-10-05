#!/usr/bin/env bash
# 建虚拟环境 + 装依赖（铁律 #6：依赖已在白名单内才装）
#
# 注意：**不做 editable 安装**（`uv pip install -e .`）。项目路径含中文时，
# editable 安装写的 .pth 会让解释器启动失败，原因见 docs/03 §9。
# CLI 入口用 `python scripts/scout.py`。
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "[FAIL] 需要 uv。安装：https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

echo "==> 建虚拟环境（Python 3.11）"
uv venv --python 3.11

echo "==> 装依赖（运行期 + dev，按 pyproject.toml）"
python scripts/install_deps.py

echo "==> 依赖审批复核"
python scripts/check_new_deps.py

echo "==> 自检：解释器能起、包能导入、库能建"
.venv/Scripts/python.exe scripts/scout.py --version || python .venv/bin/python scripts/scout.py --version
.venv/Scripts/python.exe scripts/scout.py --initdb || python .venv/bin/python scripts/scout.py --initdb

echo "==> 完成。跑门槛： bash scripts/check.sh"
