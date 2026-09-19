#!/usr/bin/env bash
# Idempotent prerequisites for the msksd appliance (#25).
#
# Everything the appliance needs before its VM can boot: artifacts,
# state disk, bootstrap token — and the host network, which this
# script only VERIFIES (#101): the one-time privileged setup lives
# in appliance-host-setup.sh (bridge, tap, forwarding via sysctl.d,
# NAT rules, all persistent — installed once as root, re-armed at
# every host boot by a systemd unit), so starting the appliance
# needs no sudo. Safe to run on every start; appliance-run.sh calls
# it under the devenv process manager.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
app_dir="${MSKS_APPLIANCE_DIR:-$root/.devenv/state/appliance}"
# A relative MSKS_APPLIANCE_DIR resolves below the repo root,
# matching the Python-side resolution: a CWD-relative read would
# depend on where the shell was opened.
case "$app_dir" in
/*) ;;
*) app_dir="$root/$app_dir" ;;
esac

for f in vmlinux initrd rootfs.ext4; do
  [ -f "$app_dir/$f" ] || {
    # The plain build task run may SKIP (execIfModified keys
    # unchanged — the up task's four-artifact guard in devenv.nix
    # heals this by building directly; that is the command to name).
    echo "msks: $app_dir/$f missing — run: MSKS_APPLIANCE_DIR=$app_dir devenv processes up -d (it rebuilds missing artifacts)" >&2
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

# --- host network (verified, never mutated — #101) ---------------------
# The appliance sits L2-adjacent on a private bridge; its guests'
# egress rides the host's forwarding and NAT. Installing those is
# the one-time root step (appliance-host-setup.sh); here we check
# what an unprivileged process can check and name the fix when it
# is missing. The firewall rules themselves need CAP_NET_ADMIN even
# to list — the installer owns them, and guest connectivity is the
# end-to-end proof (re-run the installer if egress ever fails).
missing=""
ip link show dev "$bridge" >/dev/null 2>&1 || missing="$missing $bridge"
ip link show dev "$tap" >/dev/null 2>&1 || missing="$missing $tap"
if [ "$(cat /proc/sys/net/ipv4/ip_forward)" != 1 ]; then
  missing="$missing net.ipv4.ip_forward"
fi
if [ -n "$missing" ]; then
  echo "msks: host network missing:$missing — run once as root:" >&2
  echo "  sudo bash $root/scripts/appliance-host-setup.sh" >&2
  exit 1
fi

# --- persistent state ---------------------------------------------------
# MSKSD_APPLIANCE_STATE can relocate the state disk (e.g. /run for
# ephemeral dev state); the template seeds it once per install.
state_disk="${MSKSD_APPLIANCE_STATE:-$app_dir/state.ext4}"
mkdir -p "$(dirname "$state_disk")"
if [ ! -f "$state_disk" ]; then
  cp -L "$app_dir/image/state.ext4" "$state_disk"
  chmod 0644 "$state_disk"
fi
# A template growth reaches existing installs (#180): the seed above
# covers only a missing disk, so a smaller existing one grows to the
# template's size here. The comparison only ever grows a disk — one
# already larger than the template keeps its size — and the truncate
# costs metadata on a sparse file; the guest's writes, not this
# step, spend the host disk. The guest's state preparation then runs
# resize2fs to grow the ext4 into the device.
template="$app_dir/image/state.ext4"
if [ -f "$template" ]; then
  target="$(stat -c %s "$template")"
  current="$(stat -c %s "$state_disk")"
  if [ "$current" -lt "$target" ]; then
    truncate -s "$target" "$state_disk"
  fi
fi

# --- the bootstrap token ------------------------------------------------
if [ ! -f "$app_dir/bootstrap-token" ]; then
  umask 077
  # od, not `tr | head`: under pipefail the classic pipeline dies on
  # SIGPIPE when head closes early (tr gets 141, errexit kills the
  # script mid-first-boot).
  od -An -N16 -tx1 /dev/urandom | tr -d ' \n' >"$app_dir/bootstrap-token"
  chmod 0600 "$app_dir/bootstrap-token"
  umask 022
fi
# The token rides the kernel cmdline (see appliance-run.sh): the host
# file is the single source of truth; rotation means editing it and
# restarting the appliance processes.
