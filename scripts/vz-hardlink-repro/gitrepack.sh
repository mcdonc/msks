#!/bin/bash
# gitrepack.sh — hardlink-heavy git workload on an rw share.
#
# Git's local-path clone hardlinks every object from the source
# repository's object store into the clone's, and `git repack`/`gc`
# then rewrite and re-link packs.  The loop drives clone/repack/gc
# until the time deadline; any nonzero exit or stderr line naming
# "No such file" counts as the transient-link-failure signature
# from mcdonc/msks#669.
#
# The source repository must sit on the same rw virtiofs share as
# the working directory: `git clone --local` hardlinks across the
# object store, and a source on any other filesystem fails every
# link with EXDEV ("failed to create link ... Invalid cross-device
# link") — a loud failure, but not the landmine.
#
# Usage: ./gitrepack.sh <src-repo-on-the-share> [minutes]
set -eu
[ $# -ge 1 ] || {
  echo "usage: $0 <src-repo-on-the-share> [minutes]" >&2
  exit 2
}
SRC=$(realpath "$1")
MINS=${2:-5}
deadline=$(($(date +%s) + MINS * 60))
n=0
mkdir -p gitwork && cd gitwork || exit 1
while [ "$(date +%s)" -lt "$deadline" ]; do
  n=$((n + 1))
  rm -rf clone
  err=$(git clone --local --quiet "$SRC" clone 2>&1) || {
    echo "FAIL(clone) iter $n: $err"
    exit 1
  }
  if echo "$err" | grep -qi "no such file"; then
    echo "FAIL(clone-enoent) iter $n: $err"
    exit 1
  fi
  err=$(git -C clone repack -adf --quiet 2>&1) || {
    echo "FAIL(repack) iter $n: $err"
    exit 1
  }
  if echo "$err" | grep -qi "no such file"; then
    echo "FAIL(repack-enoent) iter $n: $err"
    exit 1
  fi
  err=$(git -C clone gc --aggressive --quiet 2>&1) || {
    echo "FAIL(gc) iter $n: $err"
    exit 1
  }
  if echo "$err" | grep -qi "no such file"; then
    echo "FAIL(gc-enoent) iter $n: $err"
    exit 1
  fi
  echo -n "."
done
echo
echo "OK: $n clone/repack/gc cycles in ${MINS}min, no failures"
