#!/bin/sh
# The dev-workspace bootstrap seed (#77): the user_data payload that
# turns a pristine Debian workspace into an msks development
# environment — over the workspace's own egress NIC, at first boot.
#
#   msks create dev --egress --user-data scripts/dev-workspace.sh
#
# cloud-init runs this script once per overlay lifetime (a factory
# reset re-provisions from the same seed). Every step checks before
# doing, so re-running the script is a no-op.
#
# The toolchain is uv-first: uv fetches its own Python 3.14, and
# `uv sync` builds the same venv the `unit-tests` task runs from.
# devenv and nix stay an optional developer comfort (see the README
# dogfood section) off this path — building them inside the guest
# exercises upstream toolchains for tens of minutes and gigabytes,
# and tests nothing msks owns.
#
# Progress is guest-observable in /root/.msks-bootstrap/state — the
# step name while running, "done" at the end — so a host-side
# poller (the smoke test, or a human's watch loop) can tell setup
# from stall. Everything lands on the persistent root overlay and
# survives stop/start; nothing re-runs on later boots.
set -eu

REPO_URL="https://github.com/mcdonc/msks"
REPO_REF="main"
CHECKOUT="/root/msks"
STATE_DIR="/root/.msks-bootstrap"

state() {
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$1" >"$STATE_DIR/state"
}

log() {
  echo "[msks-bootstrap] $*"
}

# uv installs under /root/.local: make every later login shell (the
# console autologin is a login shell) see it, and pick it up for
# this run. The $PATH stays literal: the line is appended to
# /root/.profile verbatim, for the login shells to expand.
# shellcheck disable=SC2016
profile_line='export PATH="/root/.local/bin:$PATH"'
touch /root/.profile
if ! grep -qxF "$profile_line" /root/.profile; then
  printf '%s\n' "$profile_line" >>/root/.profile
fi
export PATH="/root/.local/bin:$PATH"

if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  state apt
  log "installing build prerequisites (curl, git)"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    ca-certificates curl git
fi

# The guard runs uv, not just names it: an interrupted installer
# can leave a truncated executable that exists but cannot run, and
# "command -v" alone would honor it forever. POSIX sh has no
# pipefail, so the installer downloads to a file first — a dead
# upstream then fails the download instead of hiding behind sh's
# exit status.
if ! uv --version >/dev/null 2>&1; then
  state uv
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
  sh /tmp/uv-install.sh
  rm -f /tmp/uv-install.sh
fi

# The guard demands a healthy checkout, not a directory: a clone
# interrupted mid-transfer leaves a broken .git behind, and a bare
# existence check would skip re-cloning forever after — wedging
# the workspace until a factory reset.
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

state "done"
log "bootstrap complete: $CHECKOUT — run the suite with:"
log "  uv run python -m pytest src/msks/tests -v -n auto"
