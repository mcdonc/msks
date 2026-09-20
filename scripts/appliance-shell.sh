#!/usr/bin/env bash
# The one-command root shell on the appliance (#189).
#
# The manual ritual it replaces: stop the appliance, seed the
# debug-shell marker onto the state disk with debugfs, hand-edit
# scripts/appliance-run.sh to switch the serial console from File
# mode to a pty, boot, find the pty path, socat into it — and the
# same ritual in reverse to get back to a normal boot.
#
# What this does instead:
#   1. stops the appliance (the process manager first, then any
#      hand-run instance — the run script's TERM trap owns the ACPI
#      teardown),
#   2. seeds the /state/debug-shell marker onto the state disk with
#      debugfs (writes to a mounted ext4 corrupt it, so the stop is
#      a prerequisite, not a courtesy),
#   3. boots the appliance with MSKS_APPLIANCE_CONSOLE=pty: the run
#      script asks cloud-hypervisor for a host pty as the serial
#      backend, and the guest's msks-debug-shell.service (gated on
#      the same marker, running as root) serves the interactive
#      shell on it,
#   4. finds the pty path through ch-remote info (cloud-hypervisor
#      writes it into config.serial.file) and attaches with socat.
#
# Detaching (Ctrl-]) stops the appliance and removes the marker, so
# the next `msks-appliance-up` is a normal boot — File-mode serial,
# serial.log, no debug unit. `--off` performs just that teardown
# (stop + marker removal) without a console session.
#
# While a session runs, THIS script owns the appliance: the run
# script runs as its child, outside the process manager. Do not
# `devenv processes up` concurrently.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
app_dir="${MSKS_APPLIANCE_DIR:-$root/.devenv/state/appliance}"
# The same relative-path anchoring every appliance script applies.
case "$app_dir" in
/*) ;;
*) app_dir="$root/$app_dir" ;;
esac
state_disk="${MSKSD_APPLIANCE_STATE:-$app_dir/state.ext4}"

usage() {
  cat >&2 <<'EOF'
usage: msks-appliance-shell [--off]
  (default) stop the appliance, seed the debug-shell marker, boot
             with the console on a pty, attach (detach: Ctrl-])
  --off      stop the appliance and remove the marker (no session)
EOF
}

# --- stop whatever is running --------------------------------------------
stop_appliance() {
  # The manager first (it would restart a TERM'd run script); the
  # 1-exit "no manager is running" is the common case, not failure.
  devenv processes down >/dev/null 2>&1 || true
  # A hand-run instance (this script's own, or a manual one): TERM
  # runs its graceful trap — ACPI poweroff inside its 60s window,
  # then the hard stop. The run.pid the EXIT trap removes is the
  # liveness signal: gone means the run script (and its VMM) is down.
  if [ -f "$app_dir/run.pid" ]; then
    local pid
    pid=$(cat "$app_dir/run.pid")
    if kill -0 "$pid" 2>/dev/null; then
      echo "msks: stopping the running appliance (graceful ACPI)"
      kill -TERM "$pid" 2>/dev/null || true
      for _ in $(seq 1 475); do
        [ "$(cat "$app_dir/run.pid" 2>/dev/null || true)" != "$pid" ] && break
        sleep 0.2
      done
    fi
  fi
  # A still-present api.sock names a VMM that refused to die (a
  # SIGKILL'd run script leaves one): refuse to seed a disk a live
  # guest has mounted — the AGENTS.md recovery recipe is the fix.
  if [ -S "$app_dir/api.sock" ]; then
    echo "msks: $app_dir/api.sock still exists — the VMM never stopped; recover with the pkill recipe in AGENTS.md before seeding" >&2
    return 1
  fi
}

# --- the marker -----------------------------------------------------------
marker_remove() {
  debugfs -w -R "rm /debug-shell" "$state_disk" >/dev/null 2>&1 || true
}

marker_seed() {
  local tmp
  tmp=$(mktemp)
  echo "seeded $(date -u +%Y-%m-%dT%H:%M:%SZ) by msks-appliance-shell" >"$tmp"
  # rm first: debugfs write refuses an existing name, and a re-run
  # of this script is exactly that case.
  marker_remove
  debugfs -w -R "write $tmp debug-shell" "$state_disk" >/dev/null
  rm -f "$tmp"
}

# --- the session teardown -------------------------------------------------
# Deliberately after every variable it reads exists; INT/TERM/EXIT
# all funnel here. unset guards keep a pre-boot failure (runpid
# empty) from killing the wrong pid.
session_teardown() {
  trap - INT TERM EXIT
  if [ -n "${runpid:-}" ]; then
    kill -TERM "$runpid" 2>/dev/null || true
    for _ in $(seq 1 475); do
      [ "$(cat "$app_dir/run.pid" 2>/dev/null || true)" != "$runpid" ] && break
      sleep 0.2
    done
    wait "$runpid" 2>/dev/null || true
  fi
  marker_remove
  echo "msks: appliance stopped, debug-shell marker removed — the next start (msks-appliance-up) is a normal boot"
}

# --- argument parsing -----------------------------------------------------
case "${1:-}" in
"") ;;
--off) ;;
*)
  usage
  exit 2
  ;;
esac

# --- prerequisites --------------------------------------------------------
if [ ! -f "$state_disk" ]; then
  echo "msks: no state disk at $state_disk; build the appliance first: msks-appliance-build" >&2
  exit 1
fi
if [ ! -e "$app_dir/image" ]; then
  echo "msks: $app_dir/image is missing; build the appliance first: msks-appliance-build" >&2
  exit 1
fi

# --- --off: teardown only -------------------------------------------------
if [ "${1:-}" = "--off" ]; then
  stop_appliance
  marker_remove
  echo "msks: debug-shell marker removed (if present); the next boot is a normal one"
  exit 0
fi

# --- boot with the console on a pty --------------------------------------
stop_appliance
marker_seed

# The run script's own output (build markers, boot chorography,
# failure causes) lands here; the console itself rides the pty.
console_out="$app_dir/console.out"
echo "msks: booting the appliance with the console on a pty (debug-shell marker seeded)"
MSKS_APPLIANCE_CONSOLE=pty bash "$root/scripts/appliance-run.sh" \
  >"$console_out" 2>&1 &
runpid=$!
trap session_teardown INT TERM EXIT

# --- find the pty ---------------------------------------------------------
# cloud-hypervisor writes the pty path into the VM config's
# serial.file when the mode is Pty (vmm/src/console_devices.rs), and
# ch-remote info reports the config — the only supported way to
# learn it. The socket appears ~1s after the VMM starts; the config
# carries the path from vm.create on.
pty=""
saw_sock=""
for _ in $(seq 1 600); do
  if [ -S "$app_dir/api.sock" ]; then
    saw_sock=1
    info=$(ch-remote --api-socket "$app_dir/api.sock" info 2>/dev/null || true)
    pty=$(printf '%s' "$info" | python3 -c '
import json, sys
try:
    serial = json.load(sys.stdin)["config"]["serial"]
except Exception:
    sys.exit(1)
if serial.get("mode") == "Pty":
    print(serial.get("file") or "")
' 2>/dev/null || true)
    if [ -n "$pty" ] && [ -e "$pty" ]; then
      break
    fi
  elif [ -n "$saw_sock" ]; then
    # The socket came and went: the VMM died mid-boot.
    break
  elif [ "$(cat "$app_dir/run.pid" 2>/dev/null || true)" != "$runpid" ]; then
    # The run script exited before the VMM ever started.
    break
  fi
  sleep 0.2
done

if [ -z "$pty" ] || [ ! -e "$pty" ]; then
  echo "msks: the appliance never exposed a console pty — run-script output:" >&2
  tail -n 25 "$console_out" >&2 || true
  exit 1
fi

# --- attach ---------------------------------------------------------------
echo "msks: root shell on $pty (the DIAG block prints at multi-user; detach with Ctrl-])"
socat -,raw,echo=0,escape=0x1d "$pty" || true
