#!/usr/bin/env bash
# The appliance in the FOREGROUND (#164): Ctrl-C (SIGINT) or SIGTERM
# stops the appliance through the same graceful path `devenv processes
# down` takes — the manager TERMs the appliance process, the run
# script's ACPI-first trap poweroffs the guest inside the 90s grace —
# and the prompt returns with the manager, virtiofsd, the VMM, and
# the guest all gone.
#
# Why a wrapper at all: a bare foreground `devenv processes up
# --no-tui` takes one of two shapes, and only one of them stops on
# Ctrl-C (both observed live on devenv 2.3.1):
#
#   * cold start (no manager running): the command IS the in-process
#     manager, and its own SIGINT handling stops every process —
#     already correct without this wrapper;
#   * attach (a detached manager already runs — an earlier `up -d`,
#     `msks:appliance-up`, or the host's systemd unit): the command
#     is only a VIEW over that manager, and Ctrl-C detaches by
#     design, leaving the manager and the appliance serving.
#
# The wrapper owns the signal in both shapes. It runs the devenv
# child in its own process group, so a terminal Ctrl-C — which the
# terminal delivers to the whole foreground process group — reaches
# this script alone and the child is signaled from here, exactly
# once: devenv force-exits on a second signal, hard-killing the
# managed tree instead of running the graceful stop. Which child it
# starts is decided up front: `processes up` for a cold start, plain
# `processes attach` when a manager already runs (`up` refuses that
# case from a background process group — its attach path wants the
# terminal's foreground group). On the signal the child's own exit
# runs first — the cold-start child stops everything as the manager,
# the attach child detaches — and the wrapper then converges the
# attach case with `devenv processes down`.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
app_dir="${MSKS_APPLIANCE_DIR:-$root/.devenv/state/appliance}"
# A relative MSKS_APPLIANCE_DIR resolves below the repo root,
# matching the run script's own resolution.
case "$app_dir" in
/*) ;;
*) app_dir="$root/$app_dir" ;;
esac

# Job control is ON for one purpose: the devenv child gets its own
# process group, so the terminal's Ctrl-C cannot reach it directly.
set -m
# stdin from /dev/null: a background process group that reads the
# terminal would stop on SIGTTIN, and a closed stdin keeps the child
# in its plain console renderer (no TUI wants this input).
if devenv processes list >/dev/null 2>&1; then
  view=(devenv processes attach)
else
  view=(devenv processes up --no-tui)
fi
"${view[@]}" </dev/null &
up=$!

# shellcheck disable=SC2317 # reached through the traps below
stop() {
  sig="$1"
  # Forward once per received signal. A second Ctrl-C while the
  # graceful stop runs forwards a second signal — devenv's own
  # impatient escape hatch (force-exit, hard kill of the tree).
  if [ -n "${up:-}" ]; then
    kill -s "$sig" "$up" 2>/dev/null || true
    wait "$up" 2>/dev/null || true
  fi
  # Converge the attach shape: a detached manager survives the view's
  # exit. `list` fails when nothing is running — the cold-start
  # shape, where the child already stopped everything — so the noisy
  # no-op `down` is skipped there.
  if devenv processes list >/dev/null 2>&1; then
    echo "msks: stopping the appliance (detached manager)"
    devenv processes down
  fi
  exit 0
}
trap 'stop INT' INT
trap 'stop TERM' TERM
trap 'stop HUP' HUP

# The child's own exit (a failed start, a boot that never serves,
# a manager that died under an attach view) propagates as this
# script's exit status; no signal fired, so no convergence is
# needed — a dead child is a stopped or never-started appliance.
wait "$up"
