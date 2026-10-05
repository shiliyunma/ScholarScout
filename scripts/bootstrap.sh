#!/usr/bin/env bash
# 建虚拟环境 + 装依赖（铁律 #6：依赖已在白名单内才装）
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "[FAIL] 需要 uv。安装：https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

echo "==> 建虚拟环境（Python 3.11）"
uv venv --python 3.11

echo "==> 装依赖（运行期 + dev）"
# uv pip 在无激活环境时用 .venv 自动探测；显式指定更稳
if [ -d .venv/Scripts ]; then
  uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
  echo "==> 完成。激活： source .venv/Scripts/activate"
else
  uv pip install --python .venv/bin/python -e ".[dev]"
  echo "==> 完成。激活： source .venv/bin/activate"
fi

echo "==> 依赖审批复核"
python scripts/check_new_deps.py
