#!/usr/bin/env bash
# Cross-writer visibility probe for the two-hop share (#670).
#
# One channel use: the guest busy-waits for ping.N files and answers
# each with pong.N; the outer side timestamps on ITS clock only, so
# clock skew cannot contaminate the numbers. RTT = create ping.N ->
# guest sees it -> guest creates pong.N -> outer sees pong.N. Round 1
# (responder startup) counts like any other.
#
# Usage: coherence.sh [rounds]     (rig must be up)
set -uo pipefail
SHARE=/Users/chrism/hop2/share
RIG=/var/lib/hop2
ROUNDS=${1:-30}
LOG=$RIG/logs/coherence.tsv
SEQ=$$ # unique per invocation; the guest command carries it

[ -f "$SHARE/ready" ] || {
  echo "rig not up"
  exit 1
}
[ "$ROUNDS" -ge 1 ] || {
  echo "rounds must be >= 1"
  exit 1
}
rm -f "$SHARE"/{ping.*,pong.*,cmd,out}
: >"$LOG"

# Guest side: answer pings forever (until the QUIT marker). The pong
# watch below has its own timeout, so a dead responder ends the probe
# instead of spinning forever.
# shellcheck disable=SC2016  # the guest expands its own variables
printf '%s\n' "$SEQ"' rm -f /mnt/share/ping.Q; while :; do for f in /mnt/share/ping.*; do [ -e "$f" ] || continue; n=${f##*.}; [ "$n" = Q ] && exit 0; : >"/mnt/share/pong.$n"; rm -f "$f"; break; done; done' >"$SHARE/cmd.tmp"
mv "$SHARE/cmd.tmp" "$SHARE/cmd"

for i in $(seq 1 "$ROUNDS"); do
  t0=$(date +%s.%N)
  : >"$SHARE/ping.$i"
  # 60s per round: visibility spikes are the thing being measured, but
  # a dead responder must not wedge the probe.
  deadline=$(($(date +%s) + 60))
  while :; do
    [ -e "$SHARE/pong.$i" ] && break
    [ "$(date +%s)" -ge "$deadline" ] && {
      echo "TIMEOUT in round $i (60s without a pong)"
      break
    }
  done
  t1=$(date +%s.%N)
  printf "%s\t%s\n" "$i" "$(echo "$t1 $t0" | awk '{printf "%.3f", $1-$2}')" >>"$LOG"
  rm -f "$SHARE/pong.$i"
done
: >"$SHARE/ping.Q"
rm -f "$SHARE"/pong.*

awk -F'\t' '{
    v[NR] = $2
    s += $2
    if (m == "" || $2 > m) m = $2
    if (n == "" || $2 < n) n = $2
  }
  END {
    # insertion sort: the median must be of sorted values, not arrival order
    for (x = 1; x <= NR; x++) {
      y = x
      while (y > 1 && v[y - 1] > v[y]) {
        t = v[y]; v[y] = v[y - 1]; v[y - 1] = t; y--
      }
    }
    if (NR % 2) med = v[(NR + 1) / 2]
    else med = (v[NR / 2] + v[NR / 2 + 1]) / 2
    printf "rounds=%d min=%.3fs median=%.3fs max=%.3fs mean=%.3fs\n", NR, n, med, m, s / NR
  }' "$LOG"
echo "spikes (>1s):"
awk -F'\t' '$2 > 1{print "  round "$1": "$2"s"}' "$LOG"
