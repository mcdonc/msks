#!/usr/bin/env bash
# The dev-mode daemon, foreground (#231): msksd runs FIRST-LEVEL on
# this host — cloud-hypervisor on the real /dev/kvm, per-VM taps,
# the egress consent stack in this kernel — no appliance VM, no
# backgrounding. Run `msks-dev` in a terminal you keep open; Ctrl-C
# stops it (msksd's own graceful path — workspaces get their stop
# cycle). The devenv process manager and nohup+pidfile variants were
# both tried and dropped before this: the manager's detached daemon
# intermittently never came up on this devenv (empty daemon.log,
# repeated spawns piling up), and a background pidfile is machinery
# for a workflow that runs in the foreground anyway.
#
# Run inside the devenv shell (the wrapper and every tool the daemon
# execs by bare name live on its PATH). Environment:
#   MSKS_DEV_STATE    state dir (default .devenv/state/msksd)
#   MSKS_DEV_HOST     bind address (default 127.0.0.1)
#   MSKS_DEV_UPLINK   egress NAT uplink (default eno4)
#   MSKS_DEV_EGRESS   egress enabled (default true)
#   MSKS_DEV_EGRESS_SUBNET  overrides the port-derived subnet
set -euo pipefail

root="${DEVENV_ROOT:?run me from the devenv shell (msks-dev)}"
state="${MSKS_DEV_STATE:-$root/.devenv/state/msksd}"
case "$state" in
/*) ;;
*) state="$root/$state" ;;
esac

caps="${MSKS_DEV_CAPS:-/run/wrappers/bin/msks-caps}"

port_free() { ! ss -H -ltn "sport = :$1" | grep -q .; }

# Idempotent state seed: the token and API port exist before the
# daemon starts (enterShell presets the client from the same files).
# A stable port per worktree state dir; the pick re-runs only when
# the seeded port is no longer free (another instance took it).
# 8660-8883 is the range the egress-subnet derive below can follow
# (a /16 between 172.31 and 172.254 per port).
mkdir -p "$state"
if [ ! -s "$state/bootstrap-token" ]; then
  head -c 32 /dev/urandom | base64 | tr -d /+= >"$state/bootstrap-token"
fi
p=""
if [ -s "$state/port" ]; then
  p="$(cat "$state/port")"
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
echo "$p" >"$state/port"

if [ ! -x "$caps" ]; then
  echo "msks-dev: capability wrapper $caps not executable — the host's NixOS config installs it (MSKS_DEV_CAPS relocates)" >&2
  exit 1
fi
msksd_bin="$root/.devenv/state/venv/bin/msksd"
[ -x "$msksd_bin" ] || msksd_bin="$(command -v msksd)"

export MSKSD_STATE_DIR="$state"
MSKSD_BOOTSTRAP_TOKEN="$(cat "$state/bootstrap-token")"
export MSKSD_BOOTSTRAP_TOKEN
export MSKSD_HOST="${MSKS_DEV_HOST:-127.0.0.1}"
export MSKSD_PORT="$p"
export MSKSD_EGRESS_ENABLED="${MSKS_DEV_EGRESS:-true}"
export MSKSD_EGRESS_UPLINK="${MSKS_DEV_UPLINK:-eno4}"
# The egress subnet derives from the port: concurrent instances
# (distinct ports) allocate disjoint /30 pools.
export MSKSD_EGRESS_SUBNET="${MSKS_DEV_EGRESS_SUBNET:-172.$((31 + p - 8660)).0.0/16}"

echo "msks-dev: serving https://127.0.0.1:$p (state $state; Ctrl-C stops)"
# Foreground exec: the wrapper raises the two caps ambient (both
# flags required — ambient needs the caps inheritable first), and
# they flow to the daemon and every tool it spawns.
exec "$caps" \
  --inh-caps=+net_admin,+net_bind_service \
  --ambient-caps=+net_admin,+net_bind_service \
  -- "$msksd_bin" --config=none
