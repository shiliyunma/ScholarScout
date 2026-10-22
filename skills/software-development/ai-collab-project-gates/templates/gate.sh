#!/usr/bin/env bash
# 门禁脚本模板：复制到项目的 scripts/check.sh，按项目改「工具清单」即可。
#
#   bash scripts/check.sh           全跑（缺工具则 SKIP，最后汇总）
#   bash scripts/check.sh --fast    只跑快检查（提交前自检）
#   bash scripts/check.sh --strict  缺工具即算失败（CI 用）
#
# 设计要点：新克隆的仓库还没装依赖，缺工具默认 SKIP 并计数，
# 不要直接失败；但汇总里必须写明跳过几项，跳过不为 0 就不算全绿。
set -uo pipefail

MODE="${1:-full}"
FAILED=0
SKIPPED=0

have() { command -v "$1" >/dev/null 2>&1; }

step() {
  local name="$1"; shift
  echo "==> ${name}"
  if "$@"; then
    echo "    [OK] ${name}"
  else
    echo "    [FAIL] ${name}"
    FAILED=$((FAILED + 1))
  fi
}

need() {
  local tool="$1"; shift
  if have "$tool"; then
    step "$*" "$@"
  elif [ "$MODE" = "--strict" ]; then
    echo "==> $*"
    echo "    [FAIL] 缺少工具：${tool}（--strict 模式下算失败）"
    FAILED=$((FAILED + 1))
  else
    echo "==> $*"
    echo "    [SKIP] 未安装 ${tool}；先跑 scripts/bootstrap.sh"
    SKIPPED=$((SKIPPED + 1))
  fi
}

cd "$(dirname "$0")/.." || exit 1

# ---- 工具清单：按项目改这三行 ----
need ruff   "lint"   ruff check src tests
need ruff   "format" ruff format --check src tests
if [ "$MODE" != "--fast" ]; then
  need mypy   "types" mypy src
  need pytest "tests" pytest -q
fi
# --------------------------------

if have python; then
  step "rules-sync"    python scripts/check_rules_sync.py
  step "deps-approval" python scripts/check_new_deps.py
else
  echo "==> rules / deps"; echo "    [FAIL] 找不到 python"
  FAILED=$((FAILED + 1))
fi

echo
echo "======== 汇总：失败 ${FAILED} 项，跳过 ${SKIPPED} 项 ========"
if [ "$FAILED" -gt 0 ]; then
  echo "结果：不合"
  exit 1
fi
if [ "$SKIPPED" -gt 0 ]; then
  echo "结果：通过，但有 ${SKIPPED} 项被跳过，不等于全绿"
  exit 0
fi
echo "结果：全绿"
