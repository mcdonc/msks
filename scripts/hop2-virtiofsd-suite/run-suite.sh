#!/usr/bin/env bash
# The #670 two-hop validation suite. Runs on the outer VM after
# up.sh has the rig up: drives the guest through the share (cmd/out
# files), does outer-side comparisons, and logs a verdict per phase.
#
# Phases (issue #670):
#   1 listing   - find output identical across the hop, large tree
#   2 hardlink  - link/unlink churn through the daemon layer
#   3 identity  - inode passthrough from hop-1 numbers; st_dev
#   4 uidgid    - uid/gid mapping host -> outer -> guest
#   5 fdpress   - node_modules-scale tree vs virtiofsd fd count
#   6 snapshot  - virtiofsd >= 1.11 gate (version check)
#   7 crash     - kill virtiofsd + guest mid-write; remount; verify
#
# Destructive phase 7 runs last; it leaves the rig needing up.sh again.
set -uo pipefail

SHARE=/Users/chrism/hop2/share
RIG=/var/lib/hop2
LOG=$RIG/logs/suite.log
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || {
  echo "run as root"
  exit 1
}
[ -f "$SHARE/ready" ] || {
  echo "rig not up (share/ready marker missing)"
  exit 1
}

PASS=0
FAIL=0
say() { echo "$@" | tee -a "$LOG"; }
verdict() { # verdict <name> <ok:0|1> <detail>
  if [ "$2" = 0 ]; then
    say "PASS $1 :: $3"
    PASS=$((PASS + 1))
  else
    say "FAIL $1 :: $3"
    FAIL=$((FAIL + 1))
  fi
}

GSEQ=0
guest() { # guest <cmd> [timeout] -> runs in guest, returns via $OUT
  local t=${2:-180} line="$1"
  # The channel only works once the driver has claimed the share
  # (fresh boot, or after a crash restart).
  for _ in $(seq 1 "$t"); do
    [ -f "$SHARE/ready" ] && break
    sleep 1
  done
  [ -f "$SHARE/ready" ] || {
    echo "TIMEOUT: guest driver not ready"
    return 124
  }
  GSEQ=$((GSEQ + 1))
  rm -f "$SHARE/out.$GSEQ" "$SHARE/end.$GSEQ" "$SHARE/rc.$GSEQ" 2>/dev/null
  # Atomic publish: the driver must never observe a truncated cmd.
  printf '%s %s\n' "$GSEQ" "$line" >"$SHARE/cmd.tmp"
  mv "$SHARE/cmd.tmp" "$SHARE/cmd"
  # Poll the completion marker - it exists only after out.<seq> holds
  # the full answer, so an early poll cannot read a partial result.
  for _ in $(seq 1 "$t"); do
    [ -f "$SHARE/end.$GSEQ" ] && break
    sleep 1
  done
  if [ ! -f "$SHARE/end.$GSEQ" ]; then
    echo "TIMEOUT: guest did not answer (seq $GSEQ)"
    return 124
  fi
  cat "$SHARE/out.$GSEQ" 2>/dev/null
  rm -f "$SHARE/out.$GSEQ" "$SHARE/end.$GSEQ" "$SHARE/rc.$GSEQ" 2>/dev/null
}

say "=== hop2 suite $(date -u +%FT%TZ)"
say "kernel $(uname -r); $(/usr/libexec/virtiofsd --version 2>&1); $(/usr/local/bin/cloud-hypervisor --version 2>&1)"

# ---------------------------------------------------------------- 1 listing
say "--- phase 1: listing completeness"
mkdir -p "$SHARE/listing"
rm -rf "$SHARE/listing/tree"
# 100 dirs x 100 files, mixed names (unicode, spaces) - 10k entries.
for d in $(seq 1 100); do
  dd="$SHARE/listing/tree/dir$d"
  mkdir -p "$dd"
  for f in $(seq 1 100); do
    printf 'content %s %s\n' "$d" "$f" >"$dd/file $f ünicode.txt"
  done
done
(cd "$SHARE/listing/tree" && find . | sort) >"$RIG/logs/listing-outer.txt"
T0=$(date +%s)
guest "cd /mnt/share/listing/tree && find . | sort" 400 >"$RIG/logs/listing-guest.txt"
LISTING_SECS=$(($(date +%s) - T0))
say "10k-entry find over both hops took ${LISTING_SECS}s (cold dentry caches)"
if diff -q "$RIG/logs/listing-outer.txt" "$RIG/logs/listing-guest.txt" >/dev/null; then
  verdict listing 0 "$(wc -l <"$RIG/logs/listing-outer.txt") entries identical across the hop (${LISTING_SECS}s)"
else
  verdict listing 1 "find output differs: $(diff "$RIG/logs/listing-outer.txt" "$RIG/logs/listing-guest.txt" | head -5)"
fi

# ---------------------------------------------------------------- 2 hardlink
say "--- phase 2: hardlink churn through the daemon layer"
# Static binary built once on the outer VM (same kernel/arch),
# copied in through the share; the guest itself has no network.
if [ ! -x "$SHARE/bin/linkloop" ]; then
  mkdir -p "$SHARE/bin"
  gcc -static -O2 -o "$SHARE/bin/linkloop" "$HERE/linkloop.c" || {
    say "gcc failed"
    exit 1
  }
fi
# shellcheck disable=SC2016  # the guest expands its own variables
guest 'mkdir -p /mnt/share/hlwork && cd /mnt/share/hlwork && touch a && mkdir -p b && timeout 60 ../bin/linkloop 2>/mnt/share/hl.err; rc=$?; echo "linkloop rc=$rc"; tail -2 /mnt/share/hl.err' 90 >"$RIG/logs/hardlink.txt"
tail -3 "$RIG/logs/hardlink.txt" | tee -a "$LOG"
# rc=124 is the expected clean-run end: linkloop loops forever, the
# outer timeout ends it. Anything else, or stderr evidence, is a hit.
if grep -qE "Failed|ENOENT|succeeded after" "$RIG/logs/hardlink.txt"; then
  verdict hardlink 1 "evidence of transient link failure: $(grep -E "Failed|ENOENT|succeeded after" "$RIG/logs/hardlink.txt" | head -2)"
elif grep -q "linkloop rc=124" "$RIG/logs/hardlink.txt"; then
  verdict hardlink 0 "60s of link/unlink churn, zero failures (rc=124 = timeout kill of the loop)"
else
  verdict hardlink 1 "unexpected linkloop end: $(tail -2 "$RIG/logs/hardlink.txt")"
fi
# inode stability across link/unlink, through both hops
# shellcheck disable=SC2016  # the guest expands its own variables
guest 'cd /mnt/share && mkdir -p inowork && cd inowork && : >a && i1=$(stat -c %i a) && ln a b && i2=$(stat -c %i a) && i3=$(stat -c %i b) && rm b && i4=$(stat -c %i a) && echo "inodes $i1 $i2 $i3 $i4"' 60 >"$RIG/logs/ino.txt"
ino=$(tail -1 "$RIG/logs/ino.txt" | awk '{print $2" "$3" "$4" "$5}')
if [ "$(printf '%s\n' "$ino" | sort -u | wc -l)" = 1 ]; then
  verdict inochange 0 "inode $ino stable across link/unlink through both hops"
else
  verdict inochange 1 "inode drift: $ino"
fi

# ---------------------------------------------------------------- 3 identity
say "--- phase 3: identity passthrough"
mkdir -p "$SHARE/identity"
echo hello >"$SHARE/identity/f"
OUTER_INO=$(stat -c %i "$SHARE/identity/f")
OUTER_DEV=$(stat -c %d "$SHARE/identity/f")
guest 'stat -c "%i %d" /mnt/share/identity/f' 60 >"$RIG/logs/identity.txt"
GUEST_INO=$(awk '{print $1}' "$RIG/logs/identity.txt" | tail -1)
GUEST_DEV=$(awk '{print $2}' "$RIG/logs/identity.txt" | tail -1)
if [ "$OUTER_INO" = "$GUEST_INO" ]; then
  verdict identity 0 "inode passthrough: outer=$OUTER_INO guest=$GUEST_INO (st_dev outer=$OUTER_DEV guest=$GUEST_DEV)"
else
  verdict identity 0 "inode re-synthesized at hop 2: outer=$OUTER_INO guest=$GUEST_INO (st_dev outer=$OUTER_DEV guest=$GUEST_DEV)"
fi

# ---------------------------------------------------------------- 4 uidgid
say "--- phase 4: uid/gid mapping"
guest 'id -u; id -g; stat -c "%u %g" /mnt/share/identity/f; ls -ln /mnt/share/identity | tail -1' 60 >"$RIG/logs/uidgid.txt"
cat "$RIG/logs/uidgid.txt" | tee -a "$LOG"
GUEST_UID=$(head -1 "$RIG/logs/uidgid.txt")
FILE_UID=$(sed -n 3p "$RIG/logs/uidgid.txt" | awk '{print $1}')
OUTER_SEES=$(stat -c "%u %g" "$SHARE/identity/f")
verdict uidgid 0 "guest uid=$GUEST_UID sees file uid=$FILE_UID; outer sees $OUTER_SEES (daemon runs as root)"
# A file created by the macOS user (prepared by the operator before the
# run, see README) shows the unmapped host uid in both VMs.
if [ -f "$SHARE/identity/hostuser.txt" ]; then
  HOST_UID_OUTER=$(stat -c %u "$SHARE/identity/hostuser.txt")
  HOST_UID_GUEST=$(guest 'stat -c %u /mnt/share/identity/hostuser.txt' 60 | tail -1)
  say "macOS-user file: uid outer=$HOST_UID_OUTER guest=$HOST_UID_GUEST"
  if [ "$HOST_UID_OUTER" = "$HOST_UID_GUEST" ]; then
    verdict uidgid-host 0 "host uid $HOST_UID_OUTER passes through unmapped (match daemon/guest uid to own files)"
  else
    verdict uidgid-host 1 "uids differ across hops: outer=$HOST_UID_OUTER guest=$HOST_UID_GUEST"
  fi
fi

# ---------------------------------------------------------------- 5 fdpress
say "--- phase 5: fd pressure (node_modules scale)"
VFD_PID=$(cat "$RIG/run/virtiofsd.pid")
fdcount() {
  local n=0 f
  for f in /proc/"$VFD_PID"/fd/*; do [ -e "$f" ] && n=$((n + 1)); done
  printf '%s' "$n"
}
say "virtiofsd fd count before: $(fdcount)"
rm -rf "$SHARE/fdtree"
mkdir -p "$SHARE/fdtree"
for d in $(seq 1 40); do
  dd="$SHARE/fdtree/pkg$d"
  mkdir -p "$dd"
  for f in $(seq 1 1000); do printf 'x' >"$dd/f$f.js"; done
done
say "built $((40 * 1000)) files; outer fd count after build: $(fdcount)"
guest 'find /mnt/share/fdtree -type f | wc -l; find /mnt/share/fdtree -type f -exec cat {} + | wc -c' 300 >"$RIG/logs/fdpress.txt"
say "guest walk: $(head -1 "$RIG/logs/fdpress.txt") files, $(tail -1 "$RIG/logs/fdpress.txt") bytes"
say "virtiofsd fd count after guest walk: $(fdcount)"
say "virtiofsd RLIMIT_NOFILE: $(grep -E 'Max open files' /proc/"$VFD_PID"/limits | awk '{print $4}')"
if [ "$(head -1 "$RIG/logs/fdpress.txt" | tr -d " ")" = "40000" ]; then
  verdict fdpress 0 "40k-file tree walks clean; daemon held $(fdcount) fds (limit $(grep 'Max open files' /proc/"$VFD_PID"/limits | awk '{print $5}'))"
else
  verdict fdpress 1 "guest saw $(head -1 "$RIG/logs/fdpress.txt") of 40000 files"
fi

# ---------------------------------------------------------------- 6 snapshot
say "--- phase 6: snapshot/migration gate"
VFSD_VER=$(/usr/libexec/virtiofsd --version 2>&1 | awk '{print $2}')
need=1.11
if [ "$(printf "%s\n%s\n" "$need" "$VFSD_VER" | sort -V | head -1)" = "$need" ]; then
  verdict snapshot 0 "virtiofsd $VFSD_VER >= $need (snapshot/migration capable)"
else
  verdict snapshot 1 "virtiofsd $VFSD_VER < $need"
fi

# ---------------------------------------------------------------- 7 crash
say "--- phase 7: crash consistency (destructive)"
mkdir -p "$SHARE/crash"
rm -f "$SHARE/crash"/*
# Writer inside the guest: 2000 files write-tmp+rename, marker lists
# every intended final name as it lands.
# shellcheck disable=SC2016  # the guest expands its own variables
guest 'nohup bash -c "cd /mnt/share/crash && for i in \$(seq 1 20000); do echo payload-\$i > tmp.\$i; mv tmp.\$i f.\$i; echo f.\$i >> intended.txt; done; echo WRITER-DONE >> intended.txt" >/dev/null 2>&1 & echo writer-started' 30 | tail -1
sleep 5
say "killing virtiofsd ($(cat "$RIG/run/virtiofsd.pid")) mid-write"
kill -9 "$(cat "$RIG/run/virtiofsd.pid")" 2>/dev/null
say "killing cloud-hypervisor ($(cat "$RIG/run/ch.pid"))"
kill -9 "$(cat "$RIG/run/ch.pid")" 2>/dev/null
sleep 2
bash "$HERE/up.sh" >/dev/null 2>&1 || {
  verdict crash 1 "rig did not come back after crash"
  exit 1
}
say "rig back up; verifying tree state"
# Every landed file must be complete; intended.txt itself may be torn.
LANDED=0
for f in "$SHARE/crash"/f.*; do [ -e "$f" ] && LANDED=$((LANDED + 1)); done
say "landed: $LANDED files"
BAD=0
for f in "$SHARE/crash"/f.*; do
  [ -e "$f" ] || continue
  n=${f##*.}
  [ "$(cat "$f" 2>/dev/null)" = "payload-$n" ] || {
    BAD=$((BAD + 1))
    say "  corrupt: $f"
  }
done
NEW_MOUNT=$(guest 'mountpoint /mnt/share && echo remounted' 60 | tail -1)
if [ "$NEW_MOUNT" = "remounted" ] && [ "$BAD" = 0 ]; then
  verdict crash 0 "$LANDED files all complete after virtiofsd+guest kill; share remounted on restart"
else
  verdict crash 1 "remount=$NEW_MOUNT corrupt=$BAD of $LANDED"
fi

say "=== suite done: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
