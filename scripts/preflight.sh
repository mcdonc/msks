#!/usr/bin/env bash
# Pre-commit gate feedback in one pass, BEFORE the commit attempt (#43).
#
# The hook suite runs the same gates this script runs; the difference
# is timing and what you get to read. A hook rejection arrives after a
# full edit -> test -> commit round and shows one gate's output at a
# time, which grows a one-offender-per-round habit (fix the first xenon
# block or the first missing branch, re-run, meet the next one). This
# script prints, in a single output:
#
#   - every ruff check violation and every file ruff format would
#     rewrite, over all tracked Python files
#   - every deferred import scripts/check_deferred_imports.py finds
#   - every xenon offender in the graded set (scripts/xenon-gate.sh)
#   - the jscpd clone report (scripts/jscpd-gate.sh)
#   - when anything under src/msks/ differs from the fork point on
#     origin/main (committed or working tree): the gated suite run —
#     `unit-tests` under the same coverage gate, -q output — then
#     every missing coverage line and branch arc for the changed
#     backend sources (scripts/covgaps.py)
#
# Usage: preflight.sh [--fast]
#   --fast skips the suite run: an instant lint/complexity pass.
#
# The intended loop: run this before the FIRST commit attempt, fix
# everything it names in one editing pass, re-run, then commit. A
# green preflight passes the hooks on the first attempt.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

fast=0
if [ "${1:-}" = "--fast" ]; then
  fast=1
  shift
fi

fail=0
summary=()

note() { printf '\n== %s ==\n' "$1"; }

record() {
  summary+=("$1|$2")
  if [ "$2" = FAIL ]; then
    fail=1
  fi
}

run_section() {
  name=$1
  shift
  note "$name"
  if "$@"; then
    record "$name" PASS
  else
    record "$name" FAIL
  fi
}

# Tracked-or-untracked Python files: the set the ruff and
# deferred-imports hooks grade. --cached --others --exclude-standard
# includes new files git ls-files alone would miss until staging (the
# commit-time hook grades them) and keeps gitignored scratch out.
pyfiles=()
while IFS= read -r f; do
  pyfiles+=("$f")
done < <(git ls-files --cached --others --exclude-standard '*.py')

# The gated suite and covgaps need the venv's pytest/coverage: inside
# `devenv shell` plain `python` is the venv interpreter, but `devenv
# tasks run` executes tasks without the venv on PATH (the nix python
# ships neither package), so resolve it explicitly and keep plain
# `python` as the fallback for direct runs.
venv_python=.devenv/state/venv/bin/python
if [ ! -x "$venv_python" ]; then
  venv_python=python
fi

run_section "ruff check" ruff check "${pyfiles[@]}"
run_section "ruff format" ruff format --check "${pyfiles[@]}"
run_section "deferred-imports" python scripts/check_deferred_imports.py "${pyfiles[@]}"
run_section "xenon" bash scripts/xenon-gate.sh
run_section "jscpd" bash scripts/jscpd-gate.sh

# Everything under src/msks/ that differs from the fork point on
# origin/main, plus staged, unstaged, and untracked files (new files
# count: an unmeasured module fails the coverage gate). Falls back to
# HEAD when origin/main is absent (a clone without the remote).
base=$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD)
changed_all=$(
  {
    git diff --name-only --diff-filter=ACMR "$base" -- src/msks
    git status --porcelain -- src/msks | sed -e 's/^...//' -e 's/.* -> //'
  } | sort -u
)

backend=()
while IFS= read -r f; do
  backend+=("$f")
done < <(printf '%s\n' "$changed_all" | grep '^src/msks/msks/' || true)

note "coverage"
if [ "$fast" -eq 1 ]; then
  echo "skipped (--fast: lint and complexity only)"
  record "coverage" SKIP
elif [ -z "$changed_all" ]; then
  echo "nothing changed under src/msks — skipping the suite run"
  record "coverage" SKIP
else
  echo "changed under src/msks (vs $base and the working tree):"
  while IFS= read -r f; do
    echo "  $f"
  done < <(printf '%s\n' "$changed_all")
  # The suite writes .coverage fresh: a stale file from an earlier run
  # would let covgaps report gaps that no longer exist (or hide new
  # ones) — delete first so the gap list describes THIS run.
  rm -f .coverage
  echo
  if "$venv_python" -m pytest src/msks/tests -q -n auto; then
    suite=PASS
  else
    suite=FAIL
  fi
  gaps_out=$(mktemp)
  if "$venv_python" scripts/covgaps.py ${backend[@]+"${backend[@]}"} >"$gaps_out" 2>&1; then
    gaps=PASS
  else
    gaps=FAIL
  fi
  cat "$gaps_out"
  rm -f "$gaps_out"
  if [ "$suite" = PASS ] && [ "$gaps" = PASS ]; then
    record "coverage" PASS
  else
    record "coverage" FAIL
  fi
fi

note "preflight summary"
for item in "${summary[@]}"; do
  printf '  %-16s %s\n' "${item%%|*}" "${item##*|}"
done
if [ "$fail" -eq 0 ]; then
  echo "preflight: green — the hooks run these same gates; ready to commit."
  exit 0
fi
echo "preflight: fix every item above in one editing pass, then re-run."
exit 1
