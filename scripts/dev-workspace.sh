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

# The gui extra's Qt runtime (#928): the synced PySide6 wheels
# link the FHS sonames the base image does not carry, and the
# suite's offscreen window test runs (not skips) once the extra
# is in the venv — without these the in-guest pytest the script
# itself prints below reddens on the import. libxcb-shape.so.0 is
# the marker: the leaf nothing else pulls, so its presence means
# this set ran (libGL alone can be there without the xcb
# leaves). The xcb leaves are named even where mesa's chains
# would pull some today — the set says what the closure needs,
# not what another package happens to drag in.
if ! ldconfig -p 2>/dev/null | grep -q 'libxcb-shape\.so\.0'; then
  state qt-libs
  log "installing the Qt runtime libraries (the gui extra's wheels link them)"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    libgl1 libegl1 libfontconfig1 libxkbcommon0 libxkbcommon-x11-0 \
    libx11-6 libxext6 libxfixes3 libxrandr2 libxcb1 libxcb-cursor0 \
    libxcb-icccm4 libxcb-util1 libxcb-image0 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-randr0 libxcb-render0 libxcb-shm0 \
    libxcb-shape0 libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 \
    libwayland-client0 libwayland-cursor0 libwayland-egl1
fi

state uv-sync
log "uv sync (the dev venv, Python included; the gui extra so the offscreen GUI suite runs)"
cd "$CHECKOUT"
uv sync --extra gui

state "done"
log "bootstrap complete: $CHECKOUT — run the suite with:"
log "  uv run python -m pytest src/msks/tests -v -n auto"
