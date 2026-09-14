#!/usr/bin/env bash
# Idempotent host setup for the msksd appliance (#25).
#
# Everything the appliance needs before its VM can boot: artifacts,
# bridge/tap, state disk, bootstrap token. Safe to run on every
# start; appliance-run.sh calls it under the devenv process manager.
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

# Egress uplink for the appliance's own subnet (#52): workspace
# traffic leaves the appliance masqueraded as 192.168.77.2, and the
# host routes it the rest of the way — forwarding on, plus NAT and
# forward rules for the bridge subnet out the host's default route.
# Idempotent (check-then-add), same as the bridge above; the iptables
# compatibility layer speaks for nftables-backed hosts too.
sudo -n sysctl -qw net.ipv4.ip_forward=1
# The table comes BEFORE -C/-A: iptables-nft (≥1.8.13) rejects a
# table option after the command ("Bad argument `nat'").
ipt_rule() { # ipt_rule <table> <chain> <rule args...>: -C if present, else -A
  local table="$1"
  shift
  sudo -n iptables -t "$table" -C "$@" >/dev/null 2>&1 ||
    sudo -n iptables -t "$table" -A "$@"
}
ipt_rule filter FORWARD -i "$bridge" -m conntrack --ctstate NEW,ESTABLISHED,RELATED -j ACCEPT
ipt_rule filter FORWARD -o "$bridge" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ipt_rule nat POSTROUTING -s "${host_ip}/24" ! -o "$bridge" -j MASQUERADE

# --- persistent state ---------------------------------------------------
# MSKSD_APPLIANCE_STATE can relocate the state disk (e.g. /run for
# ephemeral dev state); the template seeds it once per install.
state_disk="${MSKSD_APPLIANCE_STATE:-$app_dir/state.ext4}"
mkdir -p "$(dirname "$state_disk")"
if [ ! -f "$state_disk" ]; then
  cp -L "$app_dir/image/state.ext4" "$state_disk"
  chmod 0644 "$state_disk"
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
