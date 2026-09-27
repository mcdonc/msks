#!/usr/bin/env bash
# Debian deployment-host setup: the host-side pieces msksd needs to
# run first-level the way the NixOS deployment host (nix/module.nix)
# runs it — the capability wrapper and the routing kernel settings.
# The daemon itself (the venv or package install, msksd.yaml, the
# state dir) stays whatever deployment brought it; this script owns
# the host only.
#
#   1. The capability wrapper, the Debian equivalent of the NixOS
#      host's security.wrappers.msks-caps (#231): a root-owned copy
#      of util-linux setpriv at /usr/local/bin/msks-caps carrying
#      file capabilities CAP_NET_ADMIN and CAP_NET_BIND_SERVICE —
#      net_admin for per-VM taps and nftables chains, net_bind_service
#      for the egress stack's DHCP 67 and DNS 53 (#101). The wrapper
#      keeps setpriv's own CLI, so the dev daemon's exec line runs
#      unchanged (DEV_CAPS=/usr/local/bin/msks-caps relocates it):
#
#        msks-caps --inh-caps=+net_admin,+net_bind_service \
#                  --ambient-caps=+net_admin,+net_bind_service -- msksd
#
#      The caps arrive ambient, so they flow to the daemon and every
#      tool it execs. The grant surface is the wrapper's execute
#      permission: any local user able to run it holds the two
#      capabilities through it — the same grant the NixOS dev host's
#      wrapper makes.
#
#   2. The routing kernel settings egress verifies or needs
#      (docs/networking.md): net.ipv4.ip_forward=1 as a boot-time
#      sysctl.d setting — the daemon reads the sysctl at startup and
#      refuses every egress workspace with the cause named when it
#      reads 0 — and the netfilter/tun modules at boot, mirroring
#      nix/module.nix's boot.kernelModules and adding the REJECT and
#      NFQUEUE modules the consent chain's rulesets reference (the
#      deny RST and the interactive hold queue).
#
# The plumbing tools the daemon execs (ip, nft, conntrack) install
# through apt when missing. /dev/kvm access arrives through the kvm
# group on the daemon's user, the same supplementary-group grant the
# NixOS unit makes — this script prints the command when the group
# is present.
#
# Idempotent: re-run any time. Re-run after a util-linux upgrade —
# the setpriv copy under /usr/local keeps running the old binary
# until refreshed. Run as root on the Debian host itself:
#
#   sudo bash scripts/setup-debian-host.sh
set -euo pipefail
umask 022

# Root's login PATH carries /usr/sbin on Debian; a sudo invocation
# can arrive with a stripped PATH, and setcap/nft/conntrack/modprobe
# live there.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

wrapper=/usr/local/bin/msks-caps
caps="cap_net_admin,cap_net_bind_service"
sysctl_file=/etc/sysctl.d/msks-egress.conf
modules_file=/etc/modules-load.d/msks.conf
# nix/module.nix's boot.kernelModules, plus the reject and NFQUEUE
# pieces its list leaves to autoload (the deny RST's
# `reject with tcp reset`, the interactive queue's `queue num`).
modules=(
  tun
  nf_tables
  nft_chain_nat
  nft_masq
  nft_ct
  nft_reject
  nf_reject_ipv4
  nf_nat
  nf_conntrack
  nfnetlink_queue
)

die() {
  echo "setup-debian-host: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "run me as root (sudo bash scripts/setup-debian-host.sh)"
[ -r /etc/os-release ] || die "no /etc/os-release — this script targets Debian hosts"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
debian) ;;
*) die "this script targets Debian hosts (os-release says '${ID:-unknown}')" ;;
esac

# --- plumbing packages -------------------------------------------------

# command → package for every tool the wrapper path or the daemon's
# egress rulesets exec by bare name.
declare -A tool_pkg=(
  [setpriv]=util-linux
  [setcap]=libcap2-bin
  [ip]=iproute2
  [nft]=nftables
  [conntrack]=conntrack
  [modprobe]=kmod
)
missing=()
for tool in setpriv setcap ip nft conntrack modprobe; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("${tool_pkg[$tool]}")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "setup-debian-host: installing ${missing[*]}"
  apt-get -qq update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
fi

# --- the capability wrapper --------------------------------------------

# A fresh copy each run (see the header note on util-linux upgrades);
# the setcap lands after the copy, because cp/install does not carry
# xattrs and a copy of a setcap'd binary arrives with none.
install -o root -g root -m 0755 "$(command -v setpriv)" "$wrapper"
setcap "${caps}+ep" "$wrapper" ||
  die "setcap on $wrapper failed — the filesystem must support capability xattrs"
getcap "$wrapper" 2>/dev/null | grep -q "${caps}=ep" ||
  die "getcap does not read ${caps}=ep back from $wrapper"

# The functional proof: the daemon's exact exec line, with the
# ambient set inspected in the exec'd process. CapAmb must read
# nonzero (0x1400 = net_bind_service | net_admin) — a wrapper whose
# capset fails closes with setpriv's own error before this check.
amb="$("$wrapper" \
  --inh-caps=+net_admin,+net_bind_service \
  --ambient-caps=+net_admin,+net_bind_service \
  -- sh -c "sed -n 's/^CapAmb:[[:space:]]*//p' /proc/self/status")"
case "$amb" in
0000000000000000 | "")
  die "wrapper exec left no ambient capabilities (CapAmb reads '${amb}')"
  ;;
esac
echo "setup-debian-host: wrapper $wrapper carries ${caps}+ep (CapAmb ${amb})"

# --- the routing kernel -------------------------------------------------

# The daemon verifies this sysctl at startup and refuses egress
# workspaces naming the key when it reads 0 (net/manager.py).
cat >"$sysctl_file" <<'EOF'
# msksd workspace egress (docs/networking.md): route packets between
# per-VM taps and the host uplink. The daemon reads this key at
# startup — net.ipv4.ip_forward=1 — and refuses every egress
# workspace with the cause named while it reads 0.
net.ipv4.ip_forward = 1
EOF
sysctl --load "$sysctl_file" >/dev/null
[ "$(cat /proc/sys/net/ipv4/ip_forward)" = "1" ] ||
  die "/proc/sys/net/ipv4/ip_forward still reads 0 after loading $sysctl_file"

# Loaded at boot for determinism, the same posture nix/module.nix
# takes ("deterministic, not on demand"): the taps, the nftables
# rulesets, NAT, conntrack, the deny RST, and the consent queue.
{
  echo "# msksd egress prerequisites (setup-debian-host.sh):"
  echo "# taps, the nftables machinery, NAT, conntrack, the deny"
  echo "# RST, and the interactive consent queue."
  for m in "${modules[@]}"; do
    echo "$m"
  done
} >"$modules_file"
# modprobe accepts a built-in module (modules.builtin) with status 0;
# a name the kernel tree lacks is a real gap — the daemon's ruleset
# load fails closed naming the cause, so say it here.
for m in "${modules[@]}"; do
  modprobe "$m" || echo "setup-debian-host: WARNING modprobe $m failed — check the kernel" >&2
done

# --- summary ------------------------------------------------------------

echo "setup-debian-host: done —"
echo "  wrapper:    $wrapper (setpriv, ${caps}+ep)"
echo "               dev-daemon use: DEV_CAPS=$wrapper msks-dev"
echo "  forwarding: net.ipv4.ip_forward=1 ($sysctl_file, active now)"
echo "  modules:    $modules_file ($(echo "${modules[*]}" | tr ' ' ','))"
echo "  tools:      ip=$(command -v ip) nft=$(command -v nft) conntrack=$(command -v conntrack)"
if getent group kvm >/dev/null 2>&1; then
  echo "  /dev/kvm:   adduser <daemon-user> kvm   (the kvm group gates the node)"
else
  echo "  /dev/kvm:   the kvm group is absent — install the KVM stack (qemu-kvm) first"
fi
echo "  uplink:     point the daemon at this host's default-route interface"
echo "               (MSKSD_EGRESS_UPLINK, or DEV_UPLINK for msks-dev): $(ip route show default 2>/dev/null | sed -n 's/.* dev \([^ ]*\) .*/\1/p' | head -1)"
