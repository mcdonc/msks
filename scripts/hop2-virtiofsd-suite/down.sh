#!/usr/bin/env bash
# Stop the rig: cloud-hypervisor first (SIGTERM, then KILL), then
# virtiofsd. Safe when nothing is running.
set -u
RIG=/var/lib/hop2
for name in ch virtiofsd; do
  pidfile="$RIG/run/$name.pid"
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
done
rm -f "$RIG/run/vfsd.sock"
echo "rig down"
