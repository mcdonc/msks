#!/usr/bin/env bash
# Boot one demo microvm from the built guest assets (#5).
#
# The guest's serial console is attached to this terminal (Debian
# with root autologin). Ctrl-C here stops the VM from the host; While it runs, ch-remote
# in another terminal controls the VM through the API socket printed
# below.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
guest_dir="${MSKS_GUEST_DIR:-$root/.devenv/state/guest}"
# A relative MSKS_GUEST_DIR resolves below the repo root, matching
# the Python-side resolution (guestassets.guest_dir): a CWD-relative
# read would depend on where the shell was opened.
case "$guest_dir" in
/*) ;;
*) guest_dir="$root/$guest_dir" ;;
esac
manifest="$guest_dir/guest-manifest.json"

if [ ! -f "$manifest" ]; then
  echo "msks: no guest assets in $guest_dir — build them first:" >&2
  echo "  msks-build-guest" >&2
  exit 1
fi

run_dir="$guest_dir/demo"
rm -rf "$run_dir"
mkdir -p "$run_dir"
api_socket="$run_dir/api.sock"

cmdline="$(
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cmdline"])' \
    "$manifest"
)"

echo "msks: booting the demo VM (serial console on this terminal)"
echo "msks: control it from another terminal with:"
echo "  ch-remote --api-socket $api_socket info"
echo "msks: Ctrl-C here stops the VM"

cloud-hypervisor \
  --api-socket "$api_socket" \
  --kernel "$guest_dir/vmlinux" \
  --initramfs "$guest_dir/initrd" \
  --disk "path=$guest_dir/rootfs.ext4,readonly=on" \
  --cmdline "$cmdline" \
  --cpus boot=2 \
  --memory size=512M \
  --serial tty \
  --console off &
ch_pid=$!

stop_vm() {
  # Graceful first: the guest's acpid turns the ACPI signal into a
  # poweroff. Give the VMM a short window to exit on its own (CH can
  # keep the process alive after the VM exits in serial tty mode),
  # then SIGTERM it, which CH handles by shutting down and exiting.
  ch-remote --api-socket "$api_socket" shutdown >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    kill -0 "$ch_pid" 2>/dev/null || return 0
    sleep 1
  done
  kill "$ch_pid" 2>/dev/null || true
}
trap stop_vm INT TERM

wait "$ch_pid"
