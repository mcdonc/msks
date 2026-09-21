#!/usr/bin/env bash
# Spike 210-2 for #210/#205: boot the deployed store shape and collect
# the overlay-store probes. Boots the NixOS system from
# nix/spike-210-2.nix with two disks: the immutable erofs base store
# (read-only) and the ext4 store volume (persistent across runs — its
# upper layer and database ARE the appliance's store state).
#
# Run from a devenv shell:  bash scripts/spike-210-2.sh
#
# Idempotent: the image/lower/volume artifacts build once (GC-rooted
# symlinks in the state dir), the store volume persists across runs so
# a second invocation demonstrates the persistence probe, and every
# run starts from a fresh run dir. A second simultaneous invocation is
# refused while this one's cloud-hypervisor lives.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"
state_dir="${MSKS_SPIKE210_DIR:-$root/.devenv/state/spike210-2}"
case "$state_dir" in
/*) ;;
*) state_dir="$root/$state_dir" ;;
esac
mkdir -p "$state_dir"

timeout_s="${MSKS_SPIKE210_TIMEOUT_S:-180}"
case "$timeout_s" in
'' | *[!0-9]*)
  echo "spike210: MSKS_SPIKE210_TIMEOUT_S must be a positive integer (got '$timeout_s')" >&2
  exit 1
  ;;
esac

# --- build (cached no-op when unchanged) --------------------------------
echo "spike210: building the NixOS evaluation (first build substitutes the NixOS closure)"
# No --no-out-link: the -o symlinks ARE the GC roots keeping the
# closure, the erofs base, and the pristine volume alive between runs.
for attr in spike lowerImage upperVolume; do
  nix-build -I nixpkgs="$nixpkgs" "$root/nix/spike-210-2.nix" -A "$attr" \
    -o "$state_dir/$attr" >/dev/null
done
image="$(readlink -f "$state_dir/spike")"
lower="$(readlink -f "$state_dir/lowerImage")"
volume_pristine="$(readlink -f "$state_dir/upperVolume")"
echo "spike210: artifacts at $image"

# The store volume persists across runs; first run copies the pristine.
volume="$state_dir/store-volume.img"
if [ ! -e "$volume" ]; then
  # cp preserves the store path's read-only mode; the volume is the
  # appliance's WRITABLE store state. Copy to a temp name and move
  # into place so a killed run cannot leave a truncated or read-only
  # volume behind for every later boot.
  cp --reflink=auto "$volume_pristine" "$volume.tmp"
  chmod u+w "$volume.tmp"
  mv -f "$volume.tmp" "$volume"
fi

run_dir="$state_dir/run"
# Double-up guard (appliance-run.sh's pattern): a live cloud-hypervisor
# from a previous invocation still holds its pidfile — wiping the run
# dir out from under it would orphan it.
if [ -f "$run_dir/ch.pid" ]; then
  old_pid=$(cat "$run_dir/ch.pid" 2>/dev/null || true)
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "spike210: another spike run is live (cloud-hypervisor pid $old_pid) — $run_dir" >&2
    exit 1
  fi
fi
rm -rf "$run_dir"
mkdir -p "$run_dir"

# --- boot through the REST API (same payload style as appliance-run) ------
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

cmdline="$(cat "$image/cmdline")"

# Heredoc-interpolated JSON, the appliance-run.sh tradeoff: a double
# quote or backslash in state_dir fails the request loudly (curl
# --fail-with-body) rather than corrupting the payload silently.
boot_payload="$(
  cat <<JSON
{
  "cpus": {"boot_vcpus": 2, "max_vcpus": 2},
  "memory": {"size": 1073741824},
  "payload": {
    "kernel": "$image/vmlinux",
    "initramfs": "$image/initrd",
    "cmdline": "$cmdline"
  },
  "disks": [
    {"path": "$lower", "readonly": true, "image_type": "Raw"},
    {"path": "$volume", "readonly": false, "image_type": "Raw"}
  ],
  "serial": {"mode": "File", "file": "$run_dir/serial.log"},
  "console": {"mode": "Off"}
}
JSON
)"

cloud-hypervisor \
  --api-socket "$run_dir/api.sock" \
  >"$run_dir/cloud-hypervisor.log" 2>&1 &
ch_pid=$!
echo "$ch_pid" >"$run_dir/ch.pid"

for _ in $(seq 1 100); do
  [ -S "$run_dir/api.sock" ] && break
  kill -0 "$ch_pid" 2>/dev/null || {
    echo "spike210: cloud-hypervisor exited early (see $run_dir/cloud-hypervisor.log)" >&2
    exit 1
  }
  sleep 0.1
done
[ -S "$run_dir/api.sock" ] || {
  echo "spike210: cloud-hypervisor api socket never appeared" >&2
  exit 1
}

curl -sS --fail-with-body --unix-socket "$run_dir/api.sock" -X PUT \
  -H 'content-type: application/json' \
  -d "$boot_payload" http://localhost/api/v1/vm.create
boot_t0=$(date +%s.%N)
curl -sS --fail-with-body --unix-socket "$run_dir/api.sock" -X PUT \
  http://localhost/api/v1/vm.boot >/dev/null
echo "spike210: VM booted, waiting for SPIKE2-READY (timeout ${timeout_s}s)"

ok=0
for _ in $(seq 1 $((timeout_s * 10))); do
  if grep -q "SPIKE2-READY" "$run_dir/serial.log" 2>/dev/null; then
    ok=1
    break
  fi
  if grep -q "SPIKE2-FAILED" "$run_dir/serial.log" 2>/dev/null; then
    # The probes themselves reported a failure; fall to the tail below.
    break
  fi
  if ! kill -0 "$ch_pid" 2>/dev/null; then
    # A deliberate stop (on_signal) exits the script here and now; a
    # VM death this loop observes is a real crash.
    [ "$stopping" = 1 ] && exit 130
    echo "spike210: VM exited before the probes completed" >&2
    break
  fi
  sleep 0.1
done

boot_t1=$(date +%s.%N)
# The probe lines reach the serial log through journald's kmsg
# forwarding, so each carries a "[time] comm[pid]:" prefix — grep for
# the message, not a line start.
probe_results=$(grep -E "spike2-probe-start\[[0-9]+\]: PROBE " "$run_dir/serial.log" || true)
if [ "$ok" = 1 ] && ! printf '%s\n' "$probe_results" | grep -qE "FAILED|MISSING|VIOLATION"; then
  echo "spike210: boot-to-probes-done: $(awk -v a="$boot_t1" -v b="$boot_t0" 'BEGIN { printf "%.1f", a - b }')s (vm.boot to SPIKE2-READY; the probe service holds multi-user.target, so this is boot-to-multi-user)"
  echo "spike210: probe results:"
  printf '%s\n' "$probe_results" | sed 's/^\[[^]]*\] //' || true
  echo "spike210: run again to exercise the persistence probe (the store volume is kept at $volume)"
else
  echo "spike210: FAILED — probe output / serial tail:" >&2
  printf '%s\n' "$probe_results" >&2 || true
  tail -40 "$run_dir/serial.log" >&2 || true
  exit 1
fi

cleanup
