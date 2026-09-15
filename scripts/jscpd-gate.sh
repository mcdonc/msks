#!/usr/bin/env bash
# Token-clone gate (#71, ported from klangk #2904's advisory scan):
# jscpd exits 1 when it finds any exact clone of at least 70 tokens in
# the backend package. This script owns the gate invocation — the
# pre-commit hook and the ``msks:jscpd`` devenv task both run it, so
# the threshold and the scanned file set have exactly one definition
# and cannot drift apart. ``--min-tokens 70`` matches the invocation
# klangk's consolidation issues used, so clone reports are comparable
# across both repos.
#
# The scanned set is the backend sources, tracked OR untracked-but-
# present (--cached --others --exclude-standard, the same scoping
# xenon-gate.sh uses, so the two gates grade the same files):
# gitignored scratch stays out, while a new file that is present
# grades immediately — closing the vacuous task-pass window in which
# a new file escaped grading and only the commit hook, over the
# staged tree, failed it. The flip side is intentional: untracked
# scratch under src/msks/msks (never to be committed) now fails a
# commit too, naming the file; delete it or gitignore it. Tests are
# not part of the set (#71 decision). A deliberate in-tree clone
# gets recorded in a tracking issue and committed with
# ``git commit --no-verify`` — the escape hatch is recording, never
# suppression flags.
#
# Usage: jscpd-gate.sh [jscpd flags...]
#   Extra flags append to the fixed gate invocation.
set -euo pipefail

cd "$(dirname "$0")/.."

files=()
while IFS= read -r f; do
  files+=("$f")
done < <(git ls-files --cached --others --exclude-standard 'src/msks/msks/*.py')
if [ "${#files[@]}" -eq 0 ]; then
  echo "jscpd-gate: no backend .py files found — run from the repo root" >&2
  exit 1
fi

exec jscpd "${files[@]}" --min-tokens 70 --exit-code --no-tips "$@"
