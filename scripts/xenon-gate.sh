#!/usr/bin/env bash
# Strict wrapper for the xenon complexity gate (klangk #3415 pattern).
#
# xenon exits 0 — success — even when its parser cannot read a graded
# file: it logs a "cannot parse" WARNING and silently drops that file
# from grading. This wrapper makes any skip a hard failure and owns the
# gate invocation: the pre-commit hook and the ``msks-xenon`` devenv
# script both run this script, so the thresholds and the graded file set
# have exactly one definition and cannot drift apart.
#
# Usage: xenon-gate.sh [FILE...]
#   No arguments: grade the full gate file set (tracked + untracked
#   .py files, run from the repo root). With arguments: grade exactly
#   those files.
set -euo pipefail

thresholds=(--max-absolute A --max-modules A --max-average A)

if [ "$#" -gt 0 ]; then
  set -- "${thresholds[@]}" "$@"
else
  # Graded set: the msks package and the repo scripts, tracked OR
  # untracked-but-present — a new file that escapes grading makes the
  # task run pass vacuously while the commit hook (staged tree)
  # fails it. bash 3.2 compatible (no mapfile).
  files=()
  while IFS= read -r f; do
    files+=("$f")
  done < <(git ls-files --cached --others --exclude-standard 'src/msks/msks/*.py' 'scripts/*.py')
  if [ "${#files[@]}" -eq 0 ]; then
    echo "xenon-gate: no graded .py files found — run from the repo root" >&2
    exit 1
  fi
  set -- "${thresholds[@]}" "${files[@]}"
fi

log=$(mktemp)
trap 'rm -f "$log"' EXIT

status=0
xenon "$@" >"$log" 2>&1 || status=$?
cat "$log"

# xenon's skip warning (logger "xenon", level WARNING): the file was
# dropped from grading while the exit code stays 0.
if grep -q 'WARNING:xenon:cannot parse' "$log"; then
  cat >&2 <<'MSG'
xenon-gate: FAIL — xenon skipped at least one graded file it cannot
parse (on its own this exits 0). The file(s) above left the complexity
gate un-graded; the gate's parser is probably older than the code's
syntax.
MSG
  exit 1
fi

exit "$status"
