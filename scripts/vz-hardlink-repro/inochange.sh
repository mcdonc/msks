#!/bin/bash
# inochange.sh — direct probe of the VZ virtiofs inode-stability rule.
#
# POSIX: creating a hardlink must not change the inode of the
# original file.  Apple's VZ virtiofs server has historically
# assigned a fresh synthetic inode to the (single) underlying file
# when a second link appears (mcdonc/msks#669), which leaves the
# Linux guest's dentry cache stale.  This loop makes the violation
# directly observable: it records `a`'s inode before and after
# `ln a b` and after `rm b`, and counts any change as a failure.
# The `b` comparison is the workhorse: `b`'s dentry is built from
# the link reply itself, so a renumbered inode shows there even
# while `a`'s cached dentry still serves the stale number.
#
# Run on the virtiofs mount under test:  ./inochange.sh [iterations]
set -u
N=${1:-20000}
fail=0
rm -f b
for i in $(seq 1 "$N"); do
  : >a
  i1=$(stat -c %i a) || {
    echo "FAIL: stat a at iter $i"
    fail=1
    break
  }
  ln a b 2>/dev/null || {
    echo "FAIL: ln at iter $i"
    fail=1
    break
  }
  i2=$(stat -c %i a) || {
    echo "FAIL: stat a after link at iter $i"
    fail=1
    break
  }
  i2b=$(stat -c %i b) || {
    echo "FAIL: stat b at iter $i"
    fail=1
    break
  }
  rm b 2>/dev/null || {
    echo "FAIL: rm at iter $i"
    fail=1
    break
  }
  i3=$(stat -c %i a) || {
    echo "FAIL: stat a after rm at iter $i"
    fail=1
    break
  }
  if [ "$i1" != "$i2" ] || [ "$i1" != "$i2b" ] || [ "$i1" != "$i3" ]; then
    echo "FAIL at iter $i: before=$i1 after-link(a)=$i2 after-link(b)=$i2b after-rm=$i3"
    fail=1
    break
  fi
done
if [ "$fail" = 0 ]; then echo "OK: $N iterations, inode stable across link/unlink"; fi
exit "$fail"
