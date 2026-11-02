#!/usr/bin/env bash
# The dev-mode daemon, foreground (#231): msksd runs FIRST-LEVEL on
# this host — cloud-hypervisor on the real /dev/kvm, per-VM taps,
# the egress consent stack in this kernel. Two
# equivalent ways to run it: `devenv processes up` (the managed
# foreground process in devenv.nix, whose exec IS this script) or
# `msks-dev` (the same script by hand in a kept-open terminal).
# Either way Ctrl-C stops it (msksd's own graceful path —
# workspaces get their stop cycle). The detached manager mode
# (`processes up -d`) also works, but the nohup+pidfile variant was
# tried and dropped: backgrounding is machinery for a workflow that
# runs in the foreground anyway.
#
# Run inside the devenv shell (the wrapper and every tool the daemon
# execs by bare name live on its PATH). Environment:
#   MSKSD_STATE_DIR   state dir (default .devenv/state/msksd) — the
#                     same documented var msks-dev-ready and the
#                     image builder honor
#   MSKSD_CONFIG_DIR  config-tree root for the daemon's msksd.yaml
#                     (default the devenv root; the file is
#                     generated as the commented template on first
#                     start, gitignored — edit it freely)
#   DEV_HOST          bind address (default 127.0.0.1)
#   DEV_UPLINK        egress NAT uplink (default the host's
#                     default-route interface — the original dev
#                     host's eno4, detected rather than named, so
#                     a dev tree on any host picks its own)
#   DEV_EGRESS        egress enabled: true|false (default true)
#   DEV_EGRESS_SUBNET overrides the port-derived subnet
set -euo pipefail

# The dev daemon is Linux-only by nature — it drives cloud-hypervisor
# on /dev/kvm through the capability wrapper — and its first
# dependency on a macOS host is an opaque `flock: command not found`.
# Name the platform requirement instead (#665), and point at the
# macOS chapter that owns the two ways a Mac reaches a live daemon
# (#707): the client over a tunnel to the Linux dev host, or the
# product Lima host installed from a built wheel.
case "$(uname -s)" in
Linux) ;;
*)
  echo "msks-dev: the dev daemon runs on Linux (cloud-hypervisor on /dev/kvm); on macOS, see docs/development.md 'Developing on a macOS host' — point the client at a remote daemon, or run msks-setup-macos-host --wheel with a build in hand" >&2
  exit 1
  ;;
esac

root="${DEVENV_ROOT:?run me from the devenv shell (msks-dev)}"
state="${MSKSD_STATE_DIR:-$root/.devenv/state/msksd}"
case "$state" in
/*) ;;
*) state="$root/$state" ;;
esac

# The capability wrapper: DEV_CAPS names one outright; otherwise
# the NixOS host's security.wrappers install, then the location
# every setup tool lands (msks-setup-debian-host and
# msks-setup-fedora-host: /usr/local/bin/msks-caps) — a dev tree
# on a host NixOS does not manage finds the second without
# ceremony.
caps="${DEV_CAPS:-}"
if [ -z "$caps" ]; then
  for candidate in /run/wrappers/bin/msks-caps /usr/local/bin/msks-caps; do
    if [ -x "$candidate" ]; then
      caps="$candidate"
      break
    fi
  done
fi
mkdir -p "$state"

# One daemon per state dir: the lock (held through the daemon's
# lifetime — it inherits the descriptor) closes the double-start
# window that would otherwise put two daemons on one sqlite catalog.
exec 9>"$state/daemon.lock"
if ! flock -n 9; then
  echo "msks-dev: another daemon holds $state (daemon.lock) — refusing" >&2
  exit 1
fi

# A listening port is "not free" only when ss actually reports it:
# an empty listing is the whole answer, immune to the pipefail+
# SIGPIPE inversion a `! ss | grep -q` chain can hit.
port_free() { [ -z "$(ss -H -ltn "sport = :$1")" ]; }

# A stable port per state dir, seeded once; the pick re-runs only
# when the seeded port is no longer free (another instance took it).
# 8660-8883 spans 224 ports, matching the egress-subnet derive below
# (a distinct private 10.x.0.0/16 per port).
seed_port() {
  p=""
  if [ -s "$state/port" ]; then
    p="$(cat "$state/port")"
    case "$p" in
    '' | *[!0-9]*)
      echo "msks-dev: $state/port is not a port number ($p) — remove the file to re-seed" >&2
      exit 1
      ;;
    esac
    port_free "$p" || p=""
  fi
  if [ -z "${p:-}" ]; then
    for p in $(seq 8660 8883); do
      port_free "$p" && break || p=""
    done
  fi
  if [ -z "${p:-}" ]; then
    echo "msks-dev: no free port in 8660-8883" >&2
    exit 1
  fi
  printf '%s\n' "$p" >"$state/port"
}

# The bearer credential: atomic (temp+rename, so a concurrent reader
# never sees a half-written token) and 0600.
seed_token() {
  [ -s "$state/bootstrap-token" ] && return 0
  (
    umask 077
    head -c 32 /dev/urandom | base64 | tr -d /+= >"$state/bootstrap-token.tmp"
    mv "$state/bootstrap-token.tmp" "$state/bootstrap-token"
  )
}

seed_port
seed_token
p="$(cat "$state/port")"

if [ ! -x "$caps" ]; then
  echo "msks-dev: no executable capability wrapper (looked in /run/wrappers/bin/msks-caps, /usr/local/bin/msks-caps) — DEV_CAPS names one" >&2
  exit 1
fi
msksd_bin="$root/.devenv/state/venv/bin/msksd"
if [ ! -x "$msksd_bin" ]; then
  command -v msksd >/dev/null 2>&1 || {
    echo "msks-dev: no msksd in the venv or on PATH" >&2
    exit 1
  }
  msksd_bin="$(command -v msksd)"
fi

egress="${DEV_EGRESS:-true}"
case "$egress" in
true | false) ;;
*)
  echo "msks-dev: DEV_EGRESS must be true or false, got '$egress'" >&2
  exit 1
  ;;
esac

export MSKSD_STATE_DIR="$state"
# The daemon's config file: bare msksd resolves
# $MSKSD_CONFIG_DIR/msksd.yaml, and the tree root is the devenv
# root — so the dev daemon's durable settings live in
# <repo>/msksd.yaml (generated as the commented template on first
# start; gitignored, since local edits are personal). The exports
# below still win over file values (env > file > defaults), so the
# port/state/egress wiring this script owns stays authoritative.
export MSKSD_CONFIG_DIR="${MSKSD_CONFIG_DIR:-$root}"
MSKSD_BOOTSTRAP_TOKEN="$(cat "$state/bootstrap-token")"
export MSKSD_BOOTSTRAP_TOKEN
export MSKSD_HOST="${DEV_HOST:-127.0.0.1}"
export MSKSD_PORT="$p"
# The local listener (#555) rides the state dir beside the port:
# concurrent dev daemons (distinct state dirs) get distinct
# sockets, the same per-instance allocation the port gives TCP —
# the default path would have the second daemon's bind refuse the
# first's live listener. The shell presets point MSKSC at this
# daemon's TCP surface; a client reaches this socket by naming its
# path (MSKSC_SOCKET, a config alias, --daemon <path>) — an
# unconfigured client looks only at the default candidates.
export MSKSD_SOCKET="${DEV_SOCKET:-$state/msksd.sock}"
export MSKSD_EGRESS_ENABLED="$egress"
# The egress uplink: the host's default-route interface (the
# original fixed default, eno4, is that host's own default route
# — detection picks it there too), eno4 itself the fallback when
# no route answers.
default_uplink() {
  ip route show default 2>/dev/null | sed -n 's/^.* dev \([^ ]*\) .*/\1/p' | head -1 || true
}
uplink="${DEV_UPLINK:-$(default_uplink)}"
export MSKSD_EGRESS_UPLINK="${uplink:-eno4}"
# The egress subnet derives from the port: concurrent instances
# (distinct ports) allocate disjoint /30 pools, all inside the
# private 10.0.0.0/8 block (172.16/12 ends at 172.31 — a 172-derived
# span would leave private space after the first port).
export MSKSD_EGRESS_SUBNET="${DEV_EGRESS_SUBNET:-10.$((p - 8660)).0.0/16}"
# The image the converged state dir points at (msks-dev-ready /
# msks-build-guest-archive maintain $state/default-image): a bare
# `msks create` works with no manual import.
if [ -e "$state/default-image" ]; then
  export MSKSD_DEFAULT_IMAGE="$state/default-image"
fi

# The opt-in NixOS guest's archive (#448): when msks-build-guest
# nixos has left one, the daemon imports it at every start (warm
# when unchanged, never designated) — the same convergence the
# default-image pointer carries. A rebuilt guest therefore becomes
# what `msks create --image nixos` boots: a bare name resolves the
# most recently imported row.
nixos_dir="${GUEST_NIXOS_DIR:-$root/.devenv/state/guest-nixos}"
case "$nixos_dir" in
/*) ;;
*) nixos_dir="$root/$nixos_dir" ;;
esac
seeds=""
for archive in "$nixos_dir"/workspace-*.tar; do
  [ -e "$archive" ] || continue
  seeds="${seeds:+$seeds,}$archive"
done
if [ -n "$seeds" ]; then
  export MSKSD_SEED_IMAGES="$seeds"
fi

echo "msks-dev: serving https://127.0.0.1:$p (state $state; config ${MSKSD_CONFIG_DIR}/msksd.yaml; Ctrl-C stops)"
# Foreground exec: the wrapper raises the two caps ambient (both
# flags required — ambient needs the caps inheritable first), and
# they flow to the daemon and every tool it spawns. Bare msksd (no
# --config): the daemon resolves $MSKSD_CONFIG_DIR/msksd.yaml itself.
exec "$caps" \
  --inh-caps=+net_admin,+net_bind_service \
  --ambient-caps=+net_admin,+net_bind_service \
  -- "$msksd_bin"
