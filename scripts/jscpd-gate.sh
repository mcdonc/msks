#!/usr/bin/env bash
# Token-clone gate (#71, ported from klangk #2904's advisory scan):
# jscpd exits 1 when it finds any exact clone of at least 70 tokens in
# the backend package. This script owns the gate invocation — the
# pre-commit hook and the ``msks:jscpd`` devenv task both run it, so
# the threshold and the scanned tree have exactly one definition and
# cannot drift apart. ``--min-tokens 70`` matches the invocation
# klangk's consolidation issues used, so clone reports are comparable
# across both repos.
#
# The scanned set is the backend package only (src/msks/msks); tests
# are out of scope by decision (#71). Deliberate residuals, should any
# appear, get recorded in #71 and stay out of reports by scope — not by
# suppression.
#
# Usage: jscpd-gate.sh [jscpd flags...]
#   Extra flags append to the fixed gate invocation.
set -euo pipefail

cd "$(dirname "$0")/.."

exec jscpd src/msks/msks --min-tokens 70 --exit-code --no-tips "$@"
