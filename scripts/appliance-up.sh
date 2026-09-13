#!/usr/bin/env bash
# Boot the msksd appliance (#10): the devenv-supplied host supervisor.
#
# Everything invoked here — cloud-hypervisor, ch-remote, virtiofsd —
# comes from the devenv shell, so this is identical on any Linux
# distro. The one privileged step is the host bridge/tap (one-time,
# sudo -n); everything else runs unprivileged.
#
# Layout under .appliance/:
#   vmlinux initrd rootfs.ext4 appliance-manifest.json  (build task)
#   state.ext4   persistent state disk (copied once from the template)
#   api.sock     the appliance VM's CH API socket (down/status use it)
#   vmm-sock     virtiofsd's socket
#   serial.log   the guest console (msksd prints the TOFU fingerprint)
#   bootstrap-token  seeded onto the state disk on first boot
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
app_dir="$root/.appliance"

for f in vmlinux initrd rootfs.ext4; do
  [ -f "$app_dir/$f" ] || {
    echo "msks: $app_dir/$f missing — run: devenv tasks run msks:appliance-build" >&2
    exit 1
  }
done

# Refuse a double-up before anything is started: a live VMM answers
# vm.info even before vm.create — including our own, so this check
# must run ahead of the launch, not after it.
if [ -S "$app_dir/api.sock" ] && curl -sS --unix-socket "$app_dir/api.sock" \
    -X PUT http://localhost/api/v1/vm.info >/dev/null 2>&1; then
  echo "msks: an appliance is already running (api socket answers)" >&2
  exit 1
fi

bridge="msksbr0"
tap="mskstap0"
host_ip="192.168.77.1"
guest_ip="192.168.77.2"

# --- host networking (the one privileged step) -------------------------
# The appliance sits L2-adjacent on a private bridge: no port
# forwarding, the API is simply reachable at the guest IP.
if ! ip link show dev "$bridge" >/dev/null 2>&1; then
  sudo -n ip link add name "$bridge" type bridge
  sudo -n ip addr add "$host_ip/24" dev "$bridge"
  sudo -n ip link set "$bridge" up
fi
if ! ip link show dev "$tap" >/dev/null 2>&1; then
  sudo -n ip tuntap add mode tap user "$USER" "$tap"
  sudo -n ip link set "$tap" master "$bridge"
  sudo -n ip link set "$tap" up
fi

# --- persistent state ---------------------------------------------------
# Every disk carries an explicit image_type: v52's autodetection
# "disables sector 0 writes" on raw images that do not declare one —
# a QCOW2-misdetection guard that breaks any guest writing an ext4
# superblock (sector 0). MSKSD_APPLIANCE_STATE can still relocate the
# state disk (e.g. /run for ephemeral dev state).
state_disk="${MSKSD_APPLIANCE_STATE:-$app_dir/state.ext4}"
mkdir -p "$(dirname "$state_disk")"
fresh_state=0
if [ ! -f "$state_disk" ]; then
  cp -L "$app_dir/image/state.ext4" "$state_disk"
  chmod 0644 "$state_disk"
  fresh_state=1
fi
if [ ! -f "$app_dir/bootstrap-token" ]; then
  umask 077
  # od, not `tr | head`: under pipefail the classic pipeline dies on
  # SIGPIPE when head closes early (tr gets 141, errexit kills the
  # script mid-first-boot).
  od -An -N16 -tx1 /dev/urandom | tr -d ' \n' > "$app_dir/bootstrap-token"
  chmod 0600 "$app_dir/bootstrap-token"
  umask 022
fi
# Seed the token onto a freshly-copied state disk: the guest init
# reads /state/bootstrap-token into MSKSD_BOOTSTRAP_TOKEN. debugfs
# writes into an unmounted ext4 image (e2fsprogs, from the devenv).
bootstrap_token="$(cat "$app_dir/bootstrap-token")"

# The bootstrap token rides the kernel cmdline, not the state disk:
# debugfs writes bypass the ext4 journal, so an unclean guest
# shutdown replays the journal and reverts them — a seeded token
# flip-flops across boots. The host file is the single source of
# truth; rotation means editing it and rebooting the appliance.

# --- the store share (virtiofsd, unprivileged) --------------------------
rm -f "$app_dir/vmm-sock"
virtiofsd \
  --socket-path "$app_dir/vmm-sock" \
  --shared-dir /nix/store \
  --readonly \
  --sandbox none \
  --cache auto \
  >"$app_dir/virtiofsd.log" 2>&1 &

# --- the appliance VM ----------------------------------------------------
rm -f "$app_dir/api.sock" "$app_dir/serial.log"
# Fully detached: nohup + </dev/null so the VMM survives this script's
# exit (SIGPIPE/SIGHUP otherwise follow the parent's pipes). The VM is
# created and booted through the REST API rather than CLI payload
# flags: vm.create accepts the full disk schema (image_type), while
# the CLI parser rejects several of its fields.
nohup cloud-hypervisor \
  --api-socket "$app_dir/api.sock" \
  >"$app_dir/cloud-hypervisor.log" 2>&1 </dev/null &
echo $! > "$app_dir/vmm.pid"
disown

for _ in $(seq 1 100); do
  [ -S "$app_dir/api.sock" ] && break
  sleep 0.1
done
[ -S "$app_dir/api.sock" ] || {
  echo "msks: cloud-hypervisor API socket never appeared" >&2
  exit 1
}
api() {
  # --fail-with-body: an HTTP 4xx/5xx from vm.create/vm.boot must stop
  # the script, not print "appliance booting" over a dead VM.
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
    {"tap": "$tap", "mac": "52:54:00:00:00:01"}
  ],
  "serial": {"mode": "File", "file": "$app_dir/serial.log"},
  "console": {"mode": "Off"}
}
JSON
api vm.boot

echo "msks: appliance booting — https://$guest_ip:8660"
echo "msks: serial console log: $app_dir/serial.log (TOFU fingerprint appears there)"
echo "msks: bootstrap token: $app_dir/bootstrap-token"
echo "msks: stop it with: devenv tasks run msks:appliance-down"
