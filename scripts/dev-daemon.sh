#!/usr/bin/env bash
# The dev-mode daemon, foreground (#231): msksd runs FIRST-LEVEL on
# this host — cloud-hypervisor on the real /dev/kvm, per-VM taps,
# the egress consent stack in this kernel — no appliance VM. Two
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
#   MSKS_DEV_HOST     bind address (default 127.0.0.1)
#   MSKS_DEV_UPLINK   egress NAT uplink (default eno4 — this dev
#                     host's default route; unlike the NixOS module,
#                     which refuses an uplink default because a wrong
#                     name installs cleanly and NAT matches nothing,
#                     the dev script targets this one host)
#   MSKS_DEV_EGRESS   egress enabled: true|false (default true)
#   MSKS_DEV_EGRESS_SUBNET  overrides the port-derived subnet
set -euo pipefail

root="${DEVENV_ROOT:?run me from the devenv shell (msks-dev)}"
state="${MSKSD_STATE_DIR:-$root/.devenv/state/msksd}"
case "$state" in
/*) ;;
*) state="$root/$state" ;;
esac

caps="${MSKS_DEV_CAPS:-/run/wrappers/bin/msks-caps}"
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
  echo "msks-dev: capability wrapper $caps not executable — the host's NixOS config installs it (MSKS_DEV_CAPS relocates)" >&2
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

egress="${MSKS_DEV_EGRESS:-true}"
case "$egress" in
true | false) ;;
*)
  echo "msks-dev: MSKS_DEV_EGRESS must be true or false, got '$egress'" >&2
  exit 1
  ;;
esac

export MSKSD_STATE_DIR="$state"
MSKSD_BOOTSTRAP_TOKEN="$(cat "$state/bootstrap-token")"
export MSKSD_BOOTSTRAP_TOKEN
export MSKSD_HOST="${MSKS_DEV_HOST:-127.0.0.1}"
export MSKSD_PORT="$p"
export MSKSD_EGRESS_ENABLED="$egress"
export MSKSD_EGRESS_UPLINK="${MSKS_DEV_UPLINK:-eno4}"
# The egress subnet derives from the port: concurrent instances
# (distinct ports) allocate disjoint /30 pools, all inside the
# private 10.0.0.0/8 block (172.16/12 ends at 172.31 — a 172-derived
# span would leave private space after the first port).
export MSKSD_EGRESS_SUBNET="${MSKS_DEV_EGRESS_SUBNET:-10.$((p - 8660)).0.0/16}"
# The image the converged state dir points at (msks-dev-ready /
# msks-build-guest-archive maintain $state/default-image): a bare
# `msks create` works with no manual import.
if [ -e "$state/default-image" ]; then
  export MSKSD_DEFAULT_IMAGE="$state/default-image"
fi

echo "msks-dev: serving https://127.0.0.1:$p (state $state; Ctrl-C stops)"
# Foreground exec: the wrapper raises the two caps ambient (both
# flags required — ambient needs the caps inheritable first), and
# they flow to the daemon and every tool it spawns.
exec "$caps" \
  --inh-caps=+net_admin,+net_bind_service \
  --ambient-caps=+net_admin,+net_bind_service \
  -- "$msksd_bin" --config=none
