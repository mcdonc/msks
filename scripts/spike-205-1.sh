#!/usr/bin/env bash
# Spike 1 for #205: boot stock NixOS on cloud-hypervisor with the
# host's /nix/store served read-only over virtiofs (the dev-mode store
# shape decided on the issue), and measure boot-to-ready.
#
# Builds nix/spike-205-1.nix against the devenv-pinned nixpkgs, starts
# one virtiofsd (store share, tag ro-store), boots cloud-hypervisor
# with no disks at all (tmpfs root; every store path resolves through
# the share), and waits for the SPIKE1-READY marker the guest's
# spike-ready.service writes to the serial console after
# multi-user.target. Prints host-side elapsed boot-to-marker, then
# ACPI-stops the VM and reports systemd's own startup breakdown from
# the serial log.
#
# Idempotent: rebuilds are cached no-ops; every run starts from a
# fresh run dir. Artifacts land in .devenv/state/spike205-1.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"
state_dir="${MSKS_SPIKE205_DIR:-$root/.devenv/state/spike205-1}"
case "$state_dir" in
/*) ;;
*) state_dir="$root/$state_dir" ;;
esac
mkdir -p "$state_dir"

timeout_s="${MSKS_SPIKE205_TIMEOUT_S:-180}"
case "$timeout_s" in
'' | *[!0-9]*)
  echo "spike205: MSKS_SPIKE205_TIMEOUT_S must be a positive integer (got '$timeout_s')" >&2
  exit 1
  ;;
esac

# --- build (cached no-op when unchanged) --------------------------------
echo "spike205: building the NixOS evaluation (first build substitutes the NixOS closure)"
# No --no-out-link: the -o symlink IS the GC root keeping the closure
# alive between runs (same rationale as build-appliance.sh).
out="$(
  nix-build -I nixpkgs="$nixpkgs" \
    "$root/nix/spike-205-1.nix" -A spike \
    -o "$state_dir/image"
)"
echo "spike205: artifacts at $out"

run_dir="$state_dir/run"
# Double-up guard (appliance-run.sh's pattern): a live virtiofsd from a
# previous invocation still holds its pidfile — wiping the run dir out
# from under it would leave it orphaned and let this run's cleanup later
# ACPI-off the OTHER invocation's VM through the recreated api.sock.
if [ -f "$run_dir/vfs.sock.pid" ]; then
  old_pid=$(cat "$run_dir/vfs.sock.pid" 2>/dev/null || true)
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "spike205: another spike run is live (virtiofsd pid $old_pid) — $run_dir" >&2
    exit 1
  fi
fi
rm -rf "$run_dir"
mkdir -p "$run_dir"

# --- the store share -----------------------------------------------------
virtiofsd \
  --socket-path "$run_dir/vfs.sock" \
  --shared-dir /nix/store \
  --readonly \
  --sandbox none \
  --cache auto \
  >"$run_dir/virtiofsd.log" 2>&1 &
vfpid=$!
ch_pid=""
stopping=0
cleanup() {
  if [ -n "$ch_pid" ] && kill -0 "$ch_pid" 2>/dev/null; then
    curl -sS --unix-socket "$run_dir/api.sock" -X PUT \
      http://localhost/api/v1/vm.power-button >/dev/null 2>&1 || true
    for _ in $(seq 1 50); do
      kill -0 "$ch_pid" 2>/dev/null || break
      sleep 0.2
    done
    kill "$ch_pid" 2>/dev/null || true
    wait "$ch_pid" 2>/dev/null || true
    ch_pid="" # close the recycled-pid window (appliance-run's pattern)
  fi
  kill "$vfpid" 2>/dev/null || true
}
on_signal() {
  # A requested stop must not read as a crash below: set the flag
  # BEFORE anything the wait loop could misinterpret.
  stopping=1
  cleanup
  exit 130
}
trap on_signal INT TERM
trap cleanup EXIT
for _ in $(seq 1 100); do
  [ -S "$run_dir/vfs.sock" ] && break
  kill -0 "$vfpid" 2>/dev/null || {
    echo "spike205: virtiofsd exited before serving (see $run_dir/virtiofsd.log)" >&2
    exit 1
  }
  sleep 0.1
done
[ -S "$run_dir/vfs.sock" ] || {
  echo "spike205: virtiofsd socket never appeared" >&2
  exit 1
}

# --- boot through the REST API (same payload style as appliance-run) ------
rm -f "$run_dir/api.sock" "$run_dir/serial.log"
cmdline="$(cat "$state_dir/image/cmdline")"

cloud-hypervisor \
  --api-socket "$run_dir/api.sock" \
  >"$run_dir/cloud-hypervisor.log" 2>&1 &
ch_pid=$!
for _ in $(seq 1 100); do
  [ -S "$run_dir/api.sock" ] && break
  kill -0 "$ch_pid" 2>/dev/null || {
    echo "spike205: cloud-hypervisor exited early (see $run_dir/cloud-hypervisor.log)" >&2
    exit 1
  }
  sleep 0.1
done

boot_payload="$(
  # Heredoc-interpolated JSON, the appliance-run.sh tradeoff: a double
  # quote or backslash in state_dir fails the request loudly (curl
  # --fail-with-body) rather than corrupting the payload silently.
  cat <<JSON
{
  "cpus": {"boot_vcpus": 2, "max_vcpus": 2},
  "memory": {"size": 1073741824, "shared": true},
  "payload": {
    "kernel": "$state_dir/image/vmlinux",
    "initramfs": "$state_dir/image/initrd",
    "cmdline": "$cmdline"
  },
  "fs": [
    {"tag": "ro-store", "socket": "$run_dir/vfs.sock",
     "num_queues": 1, "queue_size": 1024}
  ],
  "serial": {"mode": "File", "file": "$run_dir/serial.log"},
  "console": {"mode": "Off"}
}
JSON
)"

curl -sS --fail-with-body --unix-socket "$run_dir/api.sock" -X PUT \
  -H 'content-type: application/json' \
  -d "$boot_payload" http://localhost/api/v1/vm.create
boot_t0=$(date +%s.%N)
curl -sS --fail-with-body --unix-socket "$run_dir/api.sock" -X PUT \
  http://localhost/api/v1/vm.boot >/dev/null
echo "spike205: VM booted, waiting for SPIKE1-READY (timeout ${timeout_s}s)"

ok=0
for _ in $(seq 1 $((timeout_s * 10))); do
  if grep -q "SPIKE1-READY" "$run_dir/serial.log" 2>/dev/null; then
    ok=1
    break
  fi
  if ! kill -0 "$ch_pid" 2>/dev/null; then
    # A deliberate stop (on_signal) exits the script here and now; a
    # VM death this loop observes is a real crash.
    [ "$stopping" = 1 ] && exit 130
    echo "spike205: VM exited before reaching multi-user.target" >&2
    break
  fi
  sleep 0.1
done

boot_t1=$(date +%s.%N)
if [ "$ok" = 1 ]; then
  echo "spike205: boot-to-ready: $(awk -v a="$boot_t1" -v b="$boot_t0" 'BEGIN { printf "%.1f", a - b }')s (vm.boot to SPIKE1-READY)"
  echo "spike205: systemd's own breakdown:"
  grep -E "Startup finished" "$run_dir/serial.log" || true
else
  echo "spike205: FAILED — serial log tail:" >&2
  tail -40 "$run_dir/serial.log" 2>/dev/null >&2 || true
  exit 1
fi

echo "spike205: serial log: $run_dir/serial.log"
