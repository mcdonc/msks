#!/bin/sh
# The L3 recursion seed (#82): turns a dev workspace (#77's
# bootstrap) into a workspace that RUNS msksd — the full recursion
# host -> appliance -> workspace -> inner workspace.
#
#   msks create l3 --egress --mem-mib 4096 --home-mib 30720 \
#       --user-data scripts/l3-recursion.sh
#
# Everything #77's script does (uv, the checkout, uv sync) is this
# script's stage one, written the same way: every step checks before
# doing, so re-running is a no-op. What L3 adds:
#
#   - cloud-hypervisor's static binary (pinned URL + sha256), the
#     same v52.0 the appliance pins, under /root/.local/bin
#   - the workspace-side tools the daemon execs (qemu-img, mkisofs,
#     nft) from Debian's own packages over egress
#   - net.ipv4.ip_forward=1 — the daemon verifies, never writes it
#   - a systemd unit running msksd from the checkout's venv: the
#     state on the persistent /home volume (inner workspaces,
#     catalog, database), egress armed behind the workspace's own
#     NIC, and the nested-virt timeouts the recursion demands
#     (vsock_wait_timeout_s above the default: an inner guest boots
#     much slower than its L2 parent)
#
# The inner workspace's boot artifacts are the one thing this seed
# cannot fetch: they are host-side build products, so the operator
# rsyncs them in over the forward plane (docs/networking.md) —
# sparse, so the mostly-zero rootfs crosses as its ~250M of real
# blocks — and creates the inner workspace over them directly (the
# catalog import would copy, hash, and densely re-extract the 1.5G
# archive for a rootfs the daemon can boot as-is):
#
#   msks key l3 --out ~/.cache/msks/l3.key
#   msks forward l3 22 --local 2201 &
#   rsync -e 'ssh -i ~/.cache/msks/l3.key -p 2201' -aPS \
#       .devenv/state/guest/vmlinux .devenv/state/guest/initrd \
#       .devenv/state/guest/rootfs.ext4 \
#       root@127.0.0.1:/root/inner-artifacts/
#   # then, inside the workspace:
#   MSKSC_URL=http://127.0.0.1:8660 MSKSC_TOKEN=$(cat /root/.msks-inner/token) \
#       /root/msks/.venv/bin/msks create inner1 \
#         --kernel /root/inner-artifacts/vmlinux \
#         --initrd /root/inner-artifacts/initrd \
#         --rootfs /root/inner-artifacts/rootfs.ext4 \
#         --cmdline 'console=ttyS0 root=/dev/vda rootfstype=ext4 rw'
#   ... msks start inner1; msks console inner1
#
# The bootstrap token is generated once into /root/.msks-inner/token
# (mode 0600); the daemon's unit reads it from there.
#
# Progress is guest-observable in /root/.msks-bootstrap/state (the
# same trail #77's script leaves): the step name while running,
# "done" at the end.
set -eu

REPO_URL="https://github.com/mcdonc/msks"
REPO_REF="main"
CHECKOUT="/root/msks"
STATE_DIR="/root/.msks-bootstrap"
INNER_DIR="/root/.msks-inner"

# The inner daemon's home (#82): inner workspaces, their volumes,
# and the image catalog live on the persistent /home volume, not the
# root overlay — the overlay stays lean and the inner state survives
# a factory reset of the root.
INNER_STATE="/home/msks-inner"

CH_URL="https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/v52.0/cloud-hypervisor-static"
CH_SHA256="829af01ff075bb96c4f183905134c453a88d68cbabdc6b87df21098842581ee9"

state() {
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$1" >"$STATE_DIR/state"
}

log() {
  echo "[msks-l3] $*"
}

# uv installs under /root/.local: make every later login shell (the
# console autologin is a login shell) see it, and pick it up for
# this run (same line #77's script writes).
# shellcheck disable=SC2016
profile_line='export PATH="/root/.local/bin:$PATH"'
touch /root/.profile
if ! grep -qxF "$profile_line" /root/.profile; then
  printf '%s\n' "$profile_line" >>/root/.profile
fi
export PATH="/root/.local/bin:$PATH"

if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  state apt-base
  log "installing build prerequisites (curl, git)"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    ca-certificates curl git
fi

if ! uv --version >/dev/null 2>&1; then
  state uv
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
  sh /tmp/uv-install.sh
  rm -f /tmp/uv-install.sh
fi

if ! git -C "$CHECKOUT" rev-parse --verify HEAD >/dev/null 2>&1; then
  state clone
  log "cloning $REPO_URL ($REPO_REF)"
  rm -rf "$CHECKOUT"
  git clone --branch "$REPO_REF" "$REPO_URL" "$CHECKOUT"
fi

state uv-sync
log "uv sync (the dev venv, Python included)"
cd "$CHECKOUT"
uv sync

# --- the L3 additions (#82) -----------------------------------------------

# The workspace-side tools the inner daemon execs at create (#14's
# overlay and volume builders, #41's seed builder) and when it arms
# egress (#52's per-VM taps and nftables rules): Debian's own
# packages, fetched over the workspace's egress NIC. mkfs.ext4 and
# ip ship with the base image.
state apt-inner
log "installing the inner daemon's tools (qemu-utils, genisoimage, nftables)"
if ! command -v qemu-img >/dev/null 2>&1 ||
  ! command -v mkisofs >/dev/null 2>&1 ||
  ! command -v nft >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    qemu-utils genisoimage nftables
fi

state cloud-hypervisor
log "installing cloud-hypervisor v52.0 (static)"
ch_bin="/root/.local/bin/cloud-hypervisor"
mkdir -p /root/.local/bin
if ! "$ch_bin" --version >/dev/null 2>&1 ||
  [ "$(sha256sum "$ch_bin" | cut -d' ' -f1)" != "$CH_SHA256" ]; then
  curl -LsSf "$CH_URL" -o "$ch_bin.new"
  echo "$CH_SHA256  $ch_bin.new" | sha256sum -c - >/dev/null
  chmod 0755 "$ch_bin.new"
  mv "$ch_bin.new" "$ch_bin"
fi

# The egress privilege the daemon verifies and never writes (#101's
# contract, one level deeper): without it the daemon refuses to arm
# egress naming the sysctl key.
state ip-forward
log "enabling net.ipv4.ip_forward"
sysctl -qw net.ipv4.ip_forward=1
# Persist it: the live sysctl dies at the workspace's reboot, and the
# daemon (restarted by its unit) verifies the key at every startup —
# a reboot without this file would leave the inner daemon refusing
# egress naming the sysctl key (#101's contract).
printf '%s\n' \
  '# msks (#82): the inner daemon verifies this at startup.' \
  'net.ipv4.ip_forward = 1' \
  >/etc/sysctl.d/90-msks-inner.conf

# The inner daemon's unit. Root: the inner VMM needs /dev/kvm (the
# kvm group's 0660 would do, but the workspace's operator today is
# root and root is simplest to state), and the egress machinery
# needs CAP_NET_ADMIN and CAP_NET_BIND_SERVICE — ambient inheritance
# would carry exactly those two to the VMM under the service-user
# posture the appliance uses, but this unit keeps root's full set:
# it is a development daemon inside a disposable workspace, not the
# production shape. The state on /home survives root-overlay resets.
#
# The recursion's tuning knobs (#82's record): an inner guest boots
# far slower than its L2 parent (nested-in-nested KVM), so the
# console bring-up window stretches fivefold and the VMM API window
# twofold past their defaults.
state unit
log "writing the msks-inner.service unit"
mkdir -p "$INNER_DIR" "$INNER_STATE"
if [ ! -s "$INNER_DIR/token" ]; then
  umask 077
  od -An -N32 -tx1 /dev/urandom | tr -d ' \n' >"$INNER_DIR/token"
fi
# The unit reads the token through an EnvironmentFile (msksd takes it
# as MSKSD_BOOTSTRAP_TOKEN, seeding the first bearer at first boot);
# the token file doubles as the msks client's MSKSC_TOKEN source.
if [ ! -s "$INNER_DIR/env" ]; then
  umask 077
  printf 'MSKSD_BOOTSTRAP_TOKEN=%s\n' "$(cat "$INNER_DIR/token")" \
    >"$INNER_DIR/env"
fi
# The uplink is whatever NIC the workspace got: udev renames it off
# eth0 (ens*-style) on this image, and the daemon's nftables rules
# name the interface the default route rides. cloud-init runs this
# seed in cloud-final, where DHCP is usually settled but not
# guaranteed — a bounded wait beats both a wrong name and a silent
# deferral the seed never re-runs to fix (cloud-init runs it once
# per overlay lifetime).
state uplink
log "waiting for the default route (the inner daemon's uplink)"
uplink=""
i=0
while [ $i -lt 120 ]; do
  uplink=$(ip -o -4 route show default | awk '{print $5; exit}')
  [ -n "$uplink" ] && break
  sleep 1
  i=$((i + 1))
done
if [ -z "$uplink" ]; then
  state "no-route"
  echo "no default route after 120s; the inner daemon has no uplink" >&2
  exit 1
fi
cat >/etc/systemd/system/msks-inner.service <<UNIT
[Unit]
Description=msksd inner daemon (L3 recursion, #82)
Wants=network-online.target
After=network-online.target msks-kvm.service

[Service]
Environment=MSKSD_STATE_DIR=$INNER_STATE
EnvironmentFile=$INNER_DIR/env
Environment=MSKSD_CLOUD_HYPERVISOR=$ch_bin
Environment=MSKSD_EGRESS_ENABLED=true
Environment=MSKSD_EGRESS_UPLINK=$uplink
Environment=MSKSD_VSOCK_WAIT_TIMEOUT_S=75
Environment=MSKSD_CONSOLE_STALL_TIMEOUT_S=30
Environment=MSKSD_SOCKET_WAIT_TIMEOUT_S=20
Environment=MSKSD_SHUTDOWN_TIMEOUT_S=60
ExecStart=$CHECKOUT/.venv/bin/msksd --no-tls --config none
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now msks-inner.service

state "done"
log "L3 recursion ready: token in $INNER_DIR/token; import an image and"
log "  MSKSC_URL=http://127.0.0.1:8660 MSKSC_TOKEN=\$(cat $INNER_DIR/token) \\"
log "  $CHECKOUT/.venv/bin/msks create inner1 --start"
