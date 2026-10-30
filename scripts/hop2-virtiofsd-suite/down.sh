#!/usr/bin/env bash
# Stop the rig: cloud-hypervisor first (SIGTERM, then KILL), then
# virtiofsd. Safe when nothing is running.
set -u
RIG=/var/lib/hop2
for name in ch virtiofsd; do
  pidfile="$RIG/run/$name.pid"
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile")
  # Only kill a pid whose executable still is our process: a recycled
  # pid must never receive our signals (comm is truncated to 15 chars,
  # so check the exe link, not the name).
  exe=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
  case "$exe" in
  /usr/local/bin/cloud-hypervisor | /usr/libexec/virtiofsd)
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$pid" 2>/dev/null || true
    ;;
  esac
  rm -f "$pidfile"
done
rm -f "$RIG/run/vfsd.sock"
echo "rig down"
