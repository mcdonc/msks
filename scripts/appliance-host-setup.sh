#!/usr/bin/env bash
# One-time HOST setup for the msksd appliance (#101): installs the
# persistent network the appliance VM sits on, so starting the
# appliance later needs no sudo at all.
#
#   sudo bash scripts/appliance-host-setup.sh
#
# Idempotent and re-runnable: every step converges. It arms the live
# state (bridge, tap, forwarding, NAT) AND installs the persistence
# that re-arms it at every host boot:
#
#   /etc/sysctl.d/90-msks-appliance.conf   host ip_forward (machine
#                                          identity — the same
#                                          contract as inside the
#                                          appliance, #101)
#   /etc/msks/host-net.sh                  the idempotent bring-up
#   /etc/systemd/system/msks-host-net.service   runs it at boot
#
# The tap is owned by the invoking user (SUDO_USER), so the
# unprivileged cloud-hypervisor opens it without CAP_NET_ADMIN.
# Run this as the user who runs the appliance; re-run it after a
# firewall reload or to change the owner.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "msks: run me as root: sudo bash scripts/appliance-host-setup.sh" >&2
  exit 1
fi

bridge="msksbr0"
tap="mskstap0"
host_ip="192.168.77.1"
owner="${SUDO_USER:?run me under sudo, as the user who runs the appliance}"

for tool in ip iptables systemctl install; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "msks: '$tool' not found on this host" >&2
    exit 1
  }
done

# The bring-up script the boot unit runs — the same check-then-add
# logic this installer applies immediately below, so a host boot and
# a re-run of the installer land in the same state.
mkdir -p /etc/msks
cat >/etc/msks/host-net.sh <<EOF
#!/bin/sh
# Installed by msks appliance-host-setup.sh: re-arm the appliance's
# host-side network (bridge, tap, forwarding, NAT) at boot. Check-
# then-add everywhere; a second run changes nothing.
set -e
if ! ip link show dev $bridge >/dev/null 2>&1; then
  ip link add name $bridge type bridge
  ip addr add $host_ip/24 dev $bridge
  ip link set $bridge up
fi
if ! ip link show dev $tap >/dev/null 2>&1; then
  ip tuntap add mode tap user $owner $tap
  ip link set $tap master $bridge
  ip link set $tap up
fi
ipt_rule() { # ipt_rule <table> <chain> <rule args...>: -C if present, else -A
  table="\$1"
  shift
  iptables -t "\$table" -C "\$@" >/dev/null 2>&1 ||
    iptables -t "\$table" -A "\$@"
}
ipt_rule filter FORWARD -i $bridge -m conntrack --ctstate NEW,ESTABLISHED,RELATED -j ACCEPT
ipt_rule filter FORWARD -o $bridge -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ipt_rule nat POSTROUTING -s $host_ip/24 ! -o $bridge -j MASQUERADE
EOF
chmod 0755 /etc/msks/host-net.sh

# Forwarding persists through sysctl.d — never a runtime write.
printf '%s\n' \
  '# msks: the appliance VM forwards its workspace traffic out through' \
  '# this host (installed by scripts/appliance-host-setup.sh).' \
  'net.ipv4.ip_forward = 1' \
  >/etc/sysctl.d/90-msks-appliance.conf

# The boot unit: re-arms bridge/tap/rules after a host reboot.
cat >/etc/systemd/system/msks-host-net.service <<'EOF'
[Unit]
Description=msks appliance host network (bridge, tap, forwarding, NAT)

[Service]
Type=oneshot
ExecStart=/etc/msks/host-net.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable msks-host-net.service

# Arm the live state now, so no reboot is needed.
/etc/msks/host-net.sh

echo "msks: host network installed and armed"
echo "msks: the appliance now starts without sudo (devenv processes up)"
