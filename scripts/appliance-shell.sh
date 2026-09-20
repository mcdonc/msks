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
#      a prerequisite, not a courtesy — and e2fsck runs first: a
#      hard-killed guest leaves a dirty journal, and a debugfs write
#      into journaled metadata can be undone by the next replay),
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
# `devenv processes up` concurrently. A lock file in the appliance
# state dir refuses a second concurrent session outright: two
# sessions race for the same state disk, and the loser's teardown
# would un-mark the winner's boot.
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

# --- VMM liveness ---------------------------------------------------------
# The run script's EXIT trap unlinks api.sock even on paths where
# the VMM process lives on (a serve-gate failure exits 1 with the
# guest still running), so the socket alone is not a liveness
# oracle. The process table is: the VMM's cmdline names this
# appliance's api-socket path forever — the same pattern AGENTS.md's
# recovery recipe matches. (pgrep excludes itself; this script's own
# cmdline never contains the pattern.) rc 1 (no match) is the good
# case; anything above it — a missing pgrep, a regex the relocated
# MSKS_APPLIANCE_DIR broke — must NOT read as "no VMM": that would
# green-light a debugfs write into a disk a live guest still holds.
vmm_pids() {
  local out rc
  rc=0
  out=$(pgrep -f "cloud-hypervisor --api-socket $app_dir/api.sock" 2>/dev/null) || rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "msks: pgrep failed (rc=$rc) — cannot verify the VMM is down; refusing to touch $state_disk" >&2
    return 1
  fi
  [ -n "$out" ] && printf '%s\n' "$out"
  return 0
}

# Bounded wait for the VMM to die; TERM then KILL escalation for a
# survivor (a guest that ignored ACPI and its own TERM).
wait_vmm_dead() {
  local phase pid pids
  for phase in term kill; do
    pids=$(vmm_pids) || return 1
    [ -z "$pids" ] && return 0
    for pid in $pids; do
      if [ "$phase" = term ]; then
        kill -TERM "$pid" 2>/dev/null || true
      else
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
    for _ in $(seq 1 150); do
      pids=$(vmm_pids) || return 1
      [ -z "$pids" ] && return 0
      sleep 0.2
    done
  done
  echo "msks: a cloud-hypervisor for $app_dir survived TERM and KILL; recover with the pkill recipe in AGENTS.md" >&2
  return 1
}

# --- stop whatever is running --------------------------------------------
stop_appliance() {
  # The manager first (it would restart a TERM'd run script); the
  # 1-exit "no process manager is running" is the common case, not
  # failure.
  devenv processes down >/dev/null 2>&1 || true
  # A hand-run instance (this script's own, or a manual one): TERM
  # runs its graceful trap — ACPI poweroff inside its 60s window,
  # then the hard stop. The run.pid the EXIT trap removes is the
  # liveness signal: gone means the run script is down. The cmdline
  # check refuses to TERM a recycled pid that happens to hold the
  # number (run.pid can outlive its process by arbitrary time).
  if [ -f "$app_dir/run.pid" ]; then
    local pid
    pid=$(cat "$app_dir/run.pid")
    if kill -0 "$pid" 2>/dev/null &&
      grep -aq "appliance-run.sh" "/proc/$pid/cmdline" 2>/dev/null; then
      echo "msks: stopping the running appliance (graceful ACPI)"
      kill -TERM "$pid" 2>/dev/null || true
      for _ in $(seq 1 475); do
        [ "$(cat "$app_dir/run.pid" 2>/dev/null || true)" != "$pid" ] && break
        sleep 0.2
      done
    fi
  fi
  # A VMM that outlived its run script (a serve-gate exit, a SIGKILL'd
  # script): TERM/KILL it before anything touches the state disk.
  wait_vmm_dead || return 1
  # And its socket file: a SIGKILL'd session leaves one behind (no
  # trap fires to remove it), and the pty-discovery loop below
  # watches this path — a stale socket would latch its "socket came
  # and went" branch when the fresh run script removes it, reading a
  # healthy boot as a dead VMM. Nothing owns it now (verified
  # above), so removing it is safe.
  rm -f "$app_dir/api.sock"
}

# --- the marker -----------------------------------------------------------
# Both debugfs writes (seed and rm) are metadata writes into the
# ext4: refuse when the journal is unreplayable, replay it when it
# is merely dirty — a hard-killed guest leaves the second case, and
# a journal replay on the NEXT boot could otherwise undo this
# script's dirent (0/1/2 = clean/fixed/fixed-reboot; 4 = refused).
disk_replay_journal() {
  # rc declared BEFORE the call: `local rc` is itself a command
  # and resets $?, so a declaration between the call and the capture
  # would read 0 unconditionally — the gate could never refuse
  # (caught in the #191 second review; shellcheck has no rule for
  # the pattern).
  local rc
  rc=0
  set +e
  e2fsck -fp "$state_disk" >/dev/null 2>&1
  rc=$?
  set -e
  if [ "$rc" -ge 4 ]; then
    echo "msks: e2fsck cannot clean $state_disk (rc=$rc); fix the state disk before seeding" >&2
    return 1
  fi
  return 0
}

marker_remove() {
  disk_replay_journal || return 1
  debugfs -w -R "rm /debug-shell" "$state_disk" >/dev/null 2>&1 || true
}

marker_seed() {
  disk_replay_journal || return 1
  local tmp
  tmp=$(mktemp)
  echo "seeded $(date -u +%Y-%m-%dT%H:%M:%SZ) by msks-appliance-shell" >"$tmp"
  # rm first: debugfs write refuses an existing name, and a re-run
  # of this script is exactly that case.
  debugfs -w -R "rm /debug-shell" "$state_disk" >/dev/null 2>&1 || true
  debugfs -w -R "write $tmp debug-shell" "$state_disk" >/dev/null 2>&1 || {
    echo "msks: seeding the debug-shell marker onto $state_disk failed" >&2
    rm -f "$tmp"
    return 1
  }
  rm -f "$tmp"
}

# --- the session teardown -------------------------------------------------
# INT/TERM/EXIT all funnel here. The run script's own exit is NOT
# proof the VMM died (its trap TERMs the VMM but does not wait for
# it), so teardown finishes with the same process-table check the
# stop path uses — and the marker removal replays the journal for
# the same reason the seed does.
session_teardown() {
  trap - INT TERM HUP EXIT
  if [ -n "${runpid:-}" ]; then
    kill -TERM "$runpid" 2>/dev/null || true
    # A zombie child (exited, unreaped) shows stat Z; anything else
    # after the bounded window gets KILLed — the run script's TERM
    # trap always exits, but this script must never block on a
    # wedged child (a bare `wait` would, indefinitely — seen in the
    # stub harness: a TERM handler that returned without exiting).
    for _ in $(seq 1 475); do
      case "$(ps -p "$runpid" -o stat= 2>/dev/null)" in
      "" | Z*) break ;;
      esac
      sleep 0.2
    done
    kill -KILL "$runpid" 2>/dev/null || true
    wait "$runpid" 2>/dev/null || true
  fi
  wait_vmm_dead || true
  # The teardown reports the marker honestly: a refusal from
  # e2fsck leaves it in place, and a false "removed" would hide the
  # one thing the next boot warns about.
  if marker_remove; then
    echo "msks: appliance stopped, debug-shell marker removed — the next start (msks-appliance-up) is a normal boot" || true
  else
    echo "msks: appliance stopped, but the marker REMOVAL FAILED (e2fsck refused the state disk) — the next boot warns; msks-appliance-shell --off retries" >&2 || true
    exit 1
  fi
}

# --- argument parsing -----------------------------------------------------
if [ "$#" -gt 1 ]; then
  usage
  exit 2
fi
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

# --- one session at a time ------------------------------------------------
# The whole stop -> seed -> boot -> attach -> teardown flow sits
# behind the lock: two sessions race for the same state disk (two
# seeds, two VMMs), and the loser's teardown un-marks the winner.
exec 9>>"$app_dir/.shell.lock"
if ! flock -n 9; then
  echo "msks: another msks-appliance-shell owns $app_dir" >&2
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

# The run script's own output (build markers, boot choreography,
# failure causes) lands here; the console itself rides the pty.
console_out="$app_dir/console.out"
echo "msks: booting the appliance with the console on a pty (debug-shell marker seeded)"
MSKS_APPLIANCE_CONSOLE=pty bash "$root/scripts/appliance-run.sh" \
  >"$console_out" 2>&1 &
runpid=$!
trap session_teardown INT TERM HUP EXIT

# --- find the pty ---------------------------------------------------------
# cloud-hypervisor writes the pty path into the VM config's
# serial.file when the mode is Pty (vmm/src/console_devices.rs), and
# ch-remote info reports the config — the only supported way to
# learn it. Two latches keep the loop honest about who is alive:
#   - saw_pid: run.pid equals our run script's pid once its setup
#     finished (setup runs BEFORE the pidfile write, so the first
#     polls legitimately see no pidfile at all — an unlatched
#     comparison would false-break on iteration one). Only a
#     divergence AFTER the latch means the run script died.
#   - saw_sock: the api socket appearing and vanishing means the VMM
#     itself died mid-boot.
pty=""
saw_sock=""
saw_pid=""
for _ in $(seq 1 600); do
  # The run script itself dying exits the loop fast — a setup
  # failure (missing artifacts, a double-up refusal) kills it
  # BEFORE run.pid is ever written, and neither latch below would
  # fire: the loop would burn its full 120s before tailing the
  # correct diagnosis. A zombie child (exited, unreaped) shows
  # stat Z. Once the pty is found the break inside the socket
  # branch wins, so this never races a healthy attach.
  case "$(ps -p "$runpid" -o stat= 2>/dev/null)" in
  "" | Z*) break ;;
  esac
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
  elif [ "$(cat "$app_dir/run.pid" 2>/dev/null || true)" = "$runpid" ]; then
    saw_pid=1
  elif [ -n "$saw_pid" ]; then
    # The run script owned the appliance and exited before the VMM
    # ever served a config.
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
