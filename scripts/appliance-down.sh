#!/usr/bin/env bash
# Stop the running msksd appliance cleanly (#10): graceful guest
# shutdown through the CH API socket, then a hard stop if needed, then
# the virtiofsd for the store share.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
app_dir="$root/.appliance"
sock="$app_dir/api.sock"

vmm_pid() {
  pgrep -f "cloud-hypervisor --api-socket $sock" | head -1 || true
}

if [ -S "$sock" ] && [ -n "$(vmm_pid)" ]; then
  echo "msks: requesting guest poweroff (ACPI) via ch-remote"
  ch-remote --api-socket "$sock" shutdown || true
  pid="$(vmm_pid)"
  for _ in $(seq 1 150); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "msks: graceful window elapsed; SIGTERM to the VMM"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 25); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
else
  echo "msks: no running appliance found (no api socket / process)"
fi

# Sweep any orphaned VMMs on this socket (repeated up cycles during
# development can overwrite vmm.pid and strand an older process).
pkill -f "cloud-hypervisor --api-socket $sock" 2>/dev/null || true
sleep 0.3
pkill -9 -f "cloud-hypervisor --api-socket $sock" 2>/dev/null || true

# The store-share daemon outlives the VM; stop it too.
pkill -f "virtiofsd --socket-path $app_dir/vmm-sock" 2>/dev/null || true
rm -f "$sock" "$app_dir/vmm-sock"
echo "msks: appliance stopped"
