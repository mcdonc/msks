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
#     rewrite, over every Python file in the tree, tracked or untracked
#   - every deferred import scripts/check_deferred_imports.py finds
#   - every import cycle scripts/check_import_cycles.py names (#387)
#   - every import-graph gate violation scripts/check_import_graph.py
#     names over import-graph.toml (package DAG, ranks, snapshot
#     counts, fan-in ceiling, reachability, facades)
#   - every xenon offender in the graded set (scripts/xenon-gate.sh)
#   - the jscpd clone report (scripts/jscpd-gate.sh)
#   - a --collect-only pass before the suite (#690): a module that
#     cannot import is named in seconds instead of a long suite run
#     that fails the same way at the end
#   - when anything under src/msks/ differs from the fork point on
#     origin/main (committed or working tree): the gated suite run —
#     `unit-tests` under the same coverage gate, -q output — then
#     every missing coverage line and branch arc for the changed
#     backend sources (scripts/covgaps.py)
#
# Every section is timed (#690) and the summary carries a duration
# column plus a total, so the slow phase of a run is visible without
# reaching for a stopwatch.
#
# Usage: preflight.sh [--fast]
#   --fast skips the suite run: an instant lint/complexity pass.
#
# The intended loop: run this before the FIRST commit attempt, fix
# everything it names in one editing pass, re-run, then commit. A
# green pre-flight means the Python gates hold at commit time; the
# doc, nix, shell, and yaml hooks still run there, and their failures
# name one file and one rule apiece.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

fast=0
if [ "${1:-}" = "--fast" ]; then
  fast=1
  shift
fi

fail=0
summary=()
gaps_out=""
trap '[ -z "$gaps_out" ] || rm -f "$gaps_out"' EXIT

note() { printf '\n== %s ==\n' "$1"; }

# Wall-clock seconds since the script started (bash's SECONDS ticks
# from shell startup; capture an explicit zero point so the total
# describes this script, not the enclosing shell's age).
start_seconds=$SECONDS

fmt_duration() {
  s=$1
  if [ "$s" -ge 60 ]; then
    printf '%dm%02ds' $((s / 60)) $((s % 60))
  else
    printf '%ds' "$s"
  fi
}

record() {
  # name|status|seconds — the seconds feed the summary's timing
  # column (#690): the slow section is visible in every run, not
  # only when someone thinks to reach for a stopwatch.
  summary+=("$1|$2|${3:-0}")
  if [ "$2" = FAIL ]; then
    fail=1
  fi
}

run_section() {
  name=$1
  shift
  note "$name"
  section_start=$SECONDS
  if "$@"; then
    status=PASS
  else
    status=FAIL
  fi
  record "$name" "$status" "$((SECONDS - section_start))"
}

# Tracked-or-untracked Python files: the set the ruff and
# deferred-imports hooks grade. --cached --others --exclude-standard
# includes new files git ls-files alone would miss until staging (the
# commit-time hook grades them) and keeps gitignored scratch out.
pyfiles=()
while IFS= read -r f; do
  pyfiles+=("$f")
done < <(git ls-files --cached --others --exclude-standard '*.py')

# The gated suite and covgaps need the venv's pytest/coverage:
# resolve the interpreter explicitly so the run works however this
# script is invoked (inside `devenv shell` plain `python` is the venv
# interpreter; a bare nix python ships neither package), keeping
# plain `python` as the fallback.
venv_python=.devenv/state/venv/bin/python
if [ ! -x "$venv_python" ]; then
  venv_python=python
fi

run_section "ruff check" ruff check ${pyfiles[@]+"${pyfiles[@]}"}
run_section "ruff format" ruff format --check ${pyfiles[@]+"${pyfiles[@]}"}
run_section "deferred-imports" "$venv_python" scripts/check_deferred_imports.py ${pyfiles[@]+"${pyfiles[@]}"}
run_section "import-cycles" "$venv_python" scripts/check_import_cycles.py
run_section "import-graph" "$venv_python" scripts/check_import_graph.py
run_section "xenon" bash scripts/xenon-gate.sh
run_section "jscpd" bash scripts/jscpd-gate.sh

# Everything under src/msks/ that differs from the fork point on
# origin/main, plus staged, unstaged, and untracked files (new files
# count: an unmeasured module fails the coverage gate). Without
# origin/main the base is HEAD: the diff side then sees only
# working-tree changes, so committed changes escape detection (the
# warning below says so). Deletions drop out on both sides — a file
# that no longer exists has no gaps to fix. -uall lists untracked
# files individually instead of collapsed directories.
if base=$(git merge-base HEAD origin/main 2>/dev/null); then
  :
else
  base=$(git rev-parse HEAD)
  echo "preflight: origin/main not found — comparing against HEAD;" \
    "committed changes escape the coverage trigger" >&2
fi
changed_all=$(
  {
    git diff --name-only --diff-filter=ACMR "$base" -- src/msks
    git status --porcelain -uall -- src/msks |
      grep -Ev '^(D|.D)' |
      sed -e 's/^...//' -e 's/.* -> //'
  } | sort -u
)

# Alembic loads the migration tree (env.py and the version files)
# by exec, outside the import system, so the suite's coverage never
# measures them — the same blindness CI's fail-under gate has. They
# stay out of the covgaps list instead of failing as "never measured".
backend=()
while IFS= read -r f; do
  backend+=("$f")
done < <(
  printf '%s\n' "$changed_all" |
    grep '^src/msks/msks/' |
    grep -v '^src/msks/msks/migrations/' || true
)

note "coverage"
coverage_start=$SECONDS
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
  # Collection gate (#690): a module that cannot import — a
  # Linux-only .so on a Mac, a syntax error — fails the suite run
  # minutes in and under xdist the error surfaces only in the final
  # report. A --collect-only pass costs seconds and names the
  # offender up front; the suite result is unchanged (a collection
  # error fails the run either way), only the wait to learn it.
  collect_out=$(mktemp)
  if ! "$venv_python" -m pytest src/msks/tests --collect-only -q >"$collect_out" 2>&1; then
    echo
    echo "collection failed — the suite cannot start; the error names the module:"
    tail -25 "$collect_out"
    rm -f "$collect_out"
    record "coverage" FAIL "$((SECONDS - coverage_start))"
  else
    rm -f "$collect_out"
    # The suite writes .coverage fresh: a stale file from an earlier run
    # would let covgaps report gaps that no longer exist (or hide new
    # ones) — delete first so the gap list describes THIS run.
    rm -f .coverage
    echo
    # A wall-clock budget (#690): a wedged test or a leaked
    # non-daemon thread at session end can hang the suite forever
    # (the macOS wedge this issue chased) — the budget fails the
    # run loudly, named, instead. 40 minutes covers the slowest
    # known healthy host (a cold CI-class Linux box) with margin.
    # GNU coreutils' timeout (the devenv shell provides it); -k
    # follows up a TERM a pytest child ignores with a KILL, so
    # even a stubborn teardown cannot outlast the budget.
    suite_budget=$((40 * 60))
    # Off Linux the suite's 100% total is the wrong bar (#691): the
    # vendored NFQUEUE binding dlopens a Linux library at import and
    # dies mid-module, and the Linux-only surfaces the suite names
    # with skips (SO_PEERCRED, SO_BINDTODEVICE, the ext4 resize
    # pair, the 127/8 live-interceptor replay) keep their lines
    # dark however green the run. The exact gate stays the covgaps
    # pass below over the changed sources; this coarse floor only
    # catches a run that stopped measuring outright. Linux keeps
    # the config's exact 100 as its explicit bar.
    cov_floor=(--cov-fail-under=100)
    if [ "$(uname -s)" != "Linux" ]; then
      cov_floor=(--cov-fail-under=90)
    fi
    timeout -k 30 "$suite_budget" \
      "$venv_python" -m pytest src/msks/tests -q -n auto "${cov_floor[@]}"
    suite_rc=$?
    if [ "$suite_rc" -eq 124 ]; then
      echo
      echo "preflight: the suite exceeded its ${suite_budget}s budget —"
      echo "a test wedged past every per-test timeout, or a session-end"
      echo "leak (a parked non-daemon thread) kept the run from exiting."
      suite=FAIL
    elif [ "$suite_rc" -eq 0 ]; then
      suite=PASS
    else
      suite=FAIL
    fi
    if [ -f .coverage ]; then
      if [ ${#backend[@]} -eq 0 ] && [ "$(uname -s)" != "Linux" ]; then
        # No product source changed (tests, scripts, docs alone) and
        # this host is off Linux: covgaps defaults to every measured
        # file, which here reports the platform-dark surfaces the
        # suite names with skips (#691) — with nothing changed to
        # grade, the pass is empty-handed, not all-files. Linux keeps
        # the all-files grade for a tests-only change: the per-arc
        # report is strictly sharper than the suite's aggregate
        # fail_under and catches a weakened test the aggregate hides.
        echo "no product sources under src/msks/msks changed — nothing for covgaps to grade"
        gaps=PASS
      else
        gaps_out=$(mktemp)
        if "$venv_python" scripts/covgaps.py ${backend[@]+"${backend[@]}"} >"$gaps_out" 2>&1; then
          gaps=PASS
        else
          gaps=FAIL
        fi
        cat "$gaps_out"
        rm -f "$gaps_out"
        gaps_out=""
      fi
    else
      echo "no coverage data written — the suite run above failed before measuring"
      gaps=FAIL
    fi
    if [ "$suite" = PASS ] && [ "$gaps" = PASS ]; then
      record "coverage" PASS "$((SECONDS - coverage_start))"
    else
      record "coverage" FAIL "$((SECONDS - coverage_start))"
    fi
  fi
fi

note "preflight summary"
for item in "${summary[@]}"; do
  name=${item%%|*}
  rest=${item#*|}
  status=${rest%%|*}
  secs=${rest##*|}
  printf '  %-16s %-4s %8s\n' "$name" "$status" "$(fmt_duration "$secs")"
done
printf '  %-16s %-4s %8s\n' "total" "" "$(fmt_duration "$((SECONDS - start_seconds))")"
if [ "$fail" -eq 0 ]; then
  echo "preflight: green — the Python gates hold; ready to commit."
  exit 0
fi
echo "preflight: fix every item above in one editing pass, then re-run."
exit 1
