#!/usr/bin/env bash
# ScholarScout 门槛脚本（铁律 #4：lint / 类型 / 测试 / 校验，一个不过就不合）
#
#   bash scripts/check.sh           全跑（缺工具则 SKIP，最后汇总）
#   bash scripts/check.sh --fast    只跑 lint + 规则同步 + 依赖审批
#   bash scripts/check.sh --strict  缺工具即算失败（CI 用）
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

# need <可执行文件> <标签> <命令...>
need() {
  local tool="$1"; shift
  local label="$1"; shift
  if have "$tool"; then
    step "$label" "$@"
  elif [ "$MODE" = "--strict" ]; then
    echo "==> ${label}"
    echo "    [FAIL] 缺少工具：${tool}（--strict 模式下算失败）"
    FAILED=$((FAILED + 1))
  else
    echo "==> ${label}"
    echo "    [SKIP] 未安装 ${tool}；先跑 scripts/bootstrap.sh"
    SKIPPED=$((SKIPPED + 1))
  fi
}

cd "$(dirname "$0")/.." || exit 1

need ruff   "lint"   ruff check src tests scripts eval
need ruff   "format" ruff format --check src tests scripts eval

if [ "$MODE" != "--fast" ]; then
  need mypy   "types" mypy src eval
  need pytest "tests" pytest -q
fi

if have python; then
  step "rules-sync"  python scripts/check_rules_sync.py
  step "deps-approval" python scripts/check_new_deps.py
  step "ui-assets"   python scripts/scout.py --check-ui
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
echo "结果：通过（注意：跳过 ${SKIPPED} 项，不等于全绿）"
