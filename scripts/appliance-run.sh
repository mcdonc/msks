#!/usr/bin/env bash
# The appliance as ONE supervised process (#25): run under the devenv
# process manager (processes.appliance), which owns restart and
# teardown. No daemonizing, no pidfiles — the supervisor supervises.
#
# The store-share daemon is a CHILD of this script, not its own
# process: virtiofsd is vhost-user 1:1 with the VM — it exits when
# its client disconnects — so its correct owner is the same lifecycle
# as the VM: a crash-restart of the appliance brings both back
# together, and neither outlives the other.
#
# Graceful stop is a TERM/INT trap: ACPI poweroff through the CH API
# socket first (the guest's acpid turns the button event into a clean
# shutdown), then SIGTERM to the VMM after a bounded wait.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
app_dir="$root/.appliance"
guest_ip="192.168.77.2"

# Idempotent prerequisites (artifacts, bridge/tap, state, token) —
# MUST run before the token read below: on a fresh checkout the token
# does not exist until setup creates it.
bash "$root/scripts/appliance-setup.sh"

state_disk="${MSKSD_APPLIANCE_STATE:-$app_dir/state.ext4}"
bootstrap_token="$(cat "$app_dir/bootstrap-token")"

# --- the store share (virtiofsd, unprivileged) --------------------------
rm -f "$app_dir/vmm-sock"
virtiofsd \
  --socket-path "$app_dir/vmm-sock" \
  --shared-dir /nix/store \
  --readonly \
  --sandbox none \
  --cache auto \
  >"$app_dir/virtiofsd.log" 2>&1 &
vfpid=$!
for _ in $(seq 1 100); do
  [ -S "$app_dir/vmm-sock" ] && break
  kill -0 "$vfpid" 2>/dev/null || {
    echo "msks: virtiofsd exited before serving (see .appliance/virtiofsd.log)" >&2
    exit 1
  }
  sleep 0.1
done
[ -S "$app_dir/vmm-sock" ] || {
  echo "msks: virtiofsd socket never appeared" >&2
  exit 1
}

# --- the appliance VM ----------------------------------------------------
rm -f "$app_dir/api.sock"

# The VM is created and booted through the REST API rather than CLI
# payload flags: vm.create accepts the full disk schema (image_type),
# while the CLI parser rejects several of its fields. Every disk
# carries an explicit image_type: v52's autodetection "disables
# sector 0 writes" on raw images that do not declare one — a
# QCOW2-misdetection guard that breaks any guest writing an ext4
# superblock (sector 0).
boot_vm() {
  for _ in $(seq 1 100); do
    [ -S "$app_dir/api.sock" ] && break
    sleep 0.1
  done
  [ -S "$app_dir/api.sock" ] || {
    echo "msks: cloud-hypervisor API socket never appeared" >&2
    return 1
  }
  api() {
    # --fail-with-body: an HTTP 4xx/5xx from vm.create/vm.boot must
    # fail loudly, not leave a dead VM with no message.
    curl -sS --fail-with-body --unix-socket "$app_dir/api.sock" -X PUT \
      -H 'content-type: application/json' \
      -d "@-" "http://localhost/api/v1/$1"
  }
  cat <<JSON | api vm.create
{
  "cpus": {"boot_vcpus": 2, "max_vcpus": 2},
  "memory": {"size": 2147483648, "shared": true},
  "payload": {
    "kernel": "$app_dir/vmlinux",
    "initramfs": "$app_dir/initrd",
    "cmdline": "console=ttyS0 root=/dev/vda rootfstype=ext4 ro msksd.bootstrap_token=$bootstrap_token"
  },
  "disks": [
    {"path": "$app_dir/rootfs.ext4", "readonly": true, "image_type": "Raw"},
    {"path": "$state_disk", "image_type": "Raw"}
  ],
  "fs": [
    {"tag": "store", "socket": "$app_dir/vmm-sock",
     "num_queues": 1, "queue_size": 1024}
  ],
  "net": [
    {"tap": "mskstap0", "mac": "52:54:00:00:00:01"}
  ],
  "serial": {"mode": "File", "file": "$app_dir/serial.log"},
  "console": {"mode": "Off"}
}
JSON
  api vm.boot || return 1
  echo "msks: appliance booting — https://$guest_ip:8660 (TOFU fingerprint in $app_dir/serial.log)"
}
# The booter races the VMM's own startup — it exits as soon as
# vm.boot is accepted.
boot_vm &
booter=$!

cleanup() {
  kill "$booter" 2>/dev/null || true
  kill "$vfpid" 2>/dev/null || true
  # virtiofsd leaves its pidfile behind even on graceful exit.
  rm -f "$app_dir/api.sock" "$app_dir/vmm-sock" "$app_dir/vmm-sock.pid"
}
trap cleanup EXIT

graceful() {
  echo "msks: stopping the appliance (ACPI, then SIGTERM)"
  # The guest's acpid turns the ACPI power button into a clean
  # shutdown; bounded wait, then the hard stop.
  curl -sS --unix-socket "$app_dir/api.sock" -X PUT \
    http://localhost/api/v1/vm.shutdown >/dev/null 2>&1 || true
  for _ in $(seq 1 50); do
    kill -0 "$chpid" 2>/dev/null || break
    sleep 0.2
  done
  kill "$chpid" 2>/dev/null || true
}
trap graceful TERM INT

cloud-hypervisor \
  --api-socket "$app_dir/api.sock" \
  >"$app_dir/cloud-hypervisor.log" 2>&1 &
chpid=$!

# errexit-safe: a nonzero wait (crash, SIGKILL, SIGTERM) must not
# kill the script before the booter is reaped and the diagnostic
# prints — the exit status still reaches the supervisor either way.
rc=0
wait "$chpid" || rc=$?
wait "$booter" 2>/dev/null || true
echo "msks: appliance VMM exited (rc=$rc); the supervisor decides what happens next"
exit "$rc"
