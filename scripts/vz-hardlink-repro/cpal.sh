#!/bin/bash
# cpal.sh — dense link() storm via cp -al (archive + hardlink).
#
# `cp -al` turns every file in a tree into a fresh link(2) call,
# the densest simple driver of the code path where VZ virtiofs
# has been observed to renumber the original file's inode.
# Any cp error, or an inode differing between the seed file and
# its link, fails the run — every file's inode is compared each
# cycle, and a stat that returns nothing fails rather than
# passing on two empty strings.
#
# Usage: ./cpal.sh [minutes]
set -u
MINS=${1:-5}
deadline=$(($(date +%s) + MINS * 60))
n=0
mkdir -p cpwork && cd cpwork || exit 1
# seed tree: 2000 files
rm -rf seed
mkdir seed
for i in $(seq 1 2000); do echo "file $i" >"seed/f$i"; done
while [ "$(date +%s)" -lt "$deadline" ]; do
  n=$((n + 1))
  rm -rf copy
  if ! cp -al seed copy 2>cp.err; then
    echo "FAIL(cp) iter $n:"
    cat cp.err
    exit 1
  fi
  if grep -qi "no such file" cp.err; then
    echo "FAIL(cp-enoent) iter $n:"
    cat cp.err
    exit 1
  fi
  # both globs expand in the same lexicographic order, so the two
  # inode lists line up file-for-file
  seedinos=$(stat -c %i seed/* 2>/dev/null)
  copyinos=$(stat -c %i copy/* 2>/dev/null)
  if [ -z "$seedinos" ] || [ -z "$copyinos" ]; then
    echo "FAIL(stat empty) iter $n: a stat returned nothing"
    exit 1
  fi
  if [ "$seedinos" != "$copyinos" ]; then
    echo "FAIL(inode mismatch) iter $n: some seed/copy pair differs"
    diff <(echo "$seedinos") <(echo "$copyinos") | head -3
    exit 1
  fi
  echo -n "."
done
echo
echo "OK: $n cp -al cycles (2000 links each) in ${MINS}min, inodes stable"
