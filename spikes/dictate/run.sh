#!/bin/sh
# Run the dictate spike with the ambient python (standard library only;
# pw-record, whisper-cli, wl-copy and pw-cat come from the system).
#
#   ./run.sh                  record until silence, copy the transcript
#   ./run.sh toggle           finish a running capture, else start one
#   ./run.sh smoke            offscreen checks
set -eu
cd "$(dirname "$0")"

if [ "${1:-}" = smoke ]; then
  shift
  exec python3 smoke.py "$@"
fi
exec python3 dictate.py "$@"
