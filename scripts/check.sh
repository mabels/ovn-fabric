#!/usr/bin/env bash
# Full local verification matching .github/workflows/ci.yaml:
# Deno fmt/lint/check/test/smoke -> schema codegen -> Python black/ruff/mypy/unittest.
#
# Usage:
#   ./scripts/check.sh                 # full check (including schema codegen)
#   ./scripts/check.sh --skip-codegen  # skip regenerating *_generated.py
#   ./scripts/check.sh --ts-only       # Deno checks only
#   ./scripts/check.sh --py-only       # Python checks only (plus codegen unless --skip-codegen)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SKIP_CODEGEN=0
RUN_TS=1
RUN_PY=1

for arg in "$@"; do
  case "$arg" in
    --skip-codegen) SKIP_CODEGEN=1 ;;
    --ts-only)      RUN_PY=0 ;;
    --py-only)      RUN_TS=0 ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

if command -v deno >/dev/null 2>&1; then
  DENO=(deno)
else
  DENO=(npx deno)
fi

VENV="${VENV:-$ROOT/.venv-build}"
PY_DIRS=(ladops/ protocol/ raw_reconciler/ runtime/ fabric/ primate/)

step() {
  printf '\n==> %s\n' "$1"
}

if [[ "$RUN_TS" -eq 1 ]]; then
  step "Deno fmt --check"
  "${DENO[@]}" fmt --check

  step "Deno lint"
  "${DENO[@]}" lint

  step "Deno type-check"
  "${DENO[@]}" check src/*.ts examples/*.ts

  step "Deno unit tests"
  "${DENO[@]}" test --allow-read --allow-env src/*_test.ts

  step "Deno smoke tests (examples/minimal-topology.ts)"
  "${DENO[@]}" run --allow-read --allow-env src/cli.ts generate examples/minimal-topology.ts >/dev/null
  "${DENO[@]}" run --allow-read --allow-env src/cli.ts generate-ir examples/minimal-topology.ts >/dev/null
fi

if [[ "$RUN_PY" -eq 1 ]]; then
  if [[ ! -x "$VENV/bin/python" ]]; then
    echo "Missing Python build venv at $VENV." >&2
    echo "Create it with: python3 -m venv .venv-build && .venv-build/bin/pip install black ruff mypy -r requirements.txt" >&2
    exit 1
  fi

  step "Python format check (black)"
  "$VENV/bin/black" --check "${PY_DIRS[@]}"

  step "Python lint (ruff)"
  "$VENV/bin/ruff" check "${PY_DIRS[@]}"

  if [[ "$SKIP_CODEGEN" -eq 0 ]]; then
    step "Codegen: protocol/generated.py"
    "${DENO[@]}" run --allow-read --allow-write --allow-run --allow-net --allow-env src/cli.ts generate-pytypes

    step "Codegen: runtime/generated.py & fabric/generated.py"
    "${DENO[@]}" run --allow-read --allow-write --allow-run --allow-net --allow-env runtime/schema/generate.ts
    "${DENO[@]}" run --allow-read --allow-write --allow-run --allow-net --allow-env fabric/schema/generate.ts
  fi

  step "Python type-check (mypy --strict)"
  "$VENV/bin/mypy"

  step "Python unit tests"
  "$VENV/bin/python" -m unittest discover -s . -p "test_*.py"
fi

printf '\nAll checks passed.\n'
