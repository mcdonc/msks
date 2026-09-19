#!/usr/bin/env bash
# The appliance as ONE run script (#25): the devenv process manager
# (#146) execs it as the `appliance` process — the build runs first
# inside the process exec — and the TERM/INT trap below owns the
# graceful choreography (the msks:appliance-up/-down tasks wrap the
# same manager). The pidfile it writes (<appliance state>/run.pid,
# .devenv/state/appliance by default — MSKS_APPLIANCE_DIR relocates
# it) is a diagnostic handle for "which run-script instance owns this
# appliance"; teardown is the supervisor's TERM, not a pidfile
# kill.
#
# The store-share daemon is a CHILD of this script, not its own
# process: virtiofsd is vhost-user 1:1 with the VM — it exits when
# its client disconnects — so its correct owner is the same lifecycle
# as the VM, and neither outlives the other.
#
# Graceful stop is a TERM/INT trap: ACPI poweroff through the CH API
# socket first (the guest's logind turns the button event into a clean
# shutdown), then SIGTERM to the VMM after a bounded wait.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
app_dir="${MSKS_APPLIANCE_DIR:-$root/.devenv/state/appliance}"
# A relative MSKS_APPLIANCE_DIR resolves below the repo root,
# matching the Python-side resolution: a CWD-relative read would
# depend on where the shell was opened.
case "$app_dir" in
/*) ;;
*) app_dir="$root/$app_dir" ;;
esac
guest_ip="192.168.77.2"

# Idempotent prerequisites (artifacts, state, token) — MUST run
# before the token read below: on a fresh checkout the token does not
# exist until setup creates it. The HOST network is verified, not
# ensured: appliance-setup.sh names the one-time root installer when
# anything is missing. This also refuses a DOUBLE-UP while the VM is
# already running (api-socket probe) — deliberately BEFORE the
# pidfile write below, so a refused second instance cannot clobber
# the running appliance's pid with its own short-lived one.
bash "$root/scripts/appliance-setup.sh"

# The instance pidfile (#146): a diagnostic handle identifying which
# run-script instance owns this appliance (stale ones are cleaned by
# the EXIT trap). Written only once this instance owns the appliance
# (setup above passed).
echo $$ >"$app_dir/run.pid"

# The EXIT trap registers HERE, not after virtiofsd/VM bring-up: a
# TERM or an early failure (virtiofsd never serving, vm.create
# rejected) must remove the pidfile it just wrote, or the next
# start needs a down/up self-heal first. The kill targets are
# guarded — they exist only from their spawn sites below.
# shellcheck disable=SC2329
appliance_exit() {
  kill "${booter:-}" 2>/dev/null || true
  kill "${vfpid:-}" 2>/dev/null || true
  kill "${devvfpid:-}" 2>/dev/null || true
  # virtiofsd leaves its pidfile behind even on graceful exit; the
  # run pidfile goes too, so a stopped appliance reports stopped.
  rm -f "$app_dir/api.sock" "$app_dir/vmm-sock" "$app_dir/vmm-sock.pid" \
    "$app_dir/dev-sock" "$app_dir/dev-sock.pid" \
    "$app_dir"/.msks-ca.pem.tmp.* "$app_dir/run.pid"
}
trap appliance_exit EXIT

state_disk="${MSKSD_APPLIANCE_STATE:-$app_dir/state.ext4}"
bootstrap_token="$(cat "$app_dir/bootstrap-token")"
# The self-contained default workspace image (#40): the containerDisk
# archive this appliance build roots; the daemon imports it on first
# boot. Optional — an appliance without one starts with an empty
# catalog and images arrive by API.
default_image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("defaultImage", ""))' "$app_dir/appliance-manifest.json")"
# The base cmdline comes from the manifest (nix/appliance-image.nix's
# kernelCmdline) — one source of truth: the appliance's own flags ride
# with the image that needs them. net.ifnames=0 lives there because the
# egress nftables rules name the uplink eth0; a locally-hardcoded base
# here drifted from it once and silently broke forwarded egress (#101).
base_cmdline="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("cmdline", ""))' "$app_dir/appliance-manifest.json")"
# The fallback serves only a stale pre-#101 manifest (no cmdline
# key), and mirrors the manifest's CURRENT value — net.ifnames=0
# included — so the drift this indirection exists to prevent cannot
# sneak back in through the fallback.
: "${base_cmdline:=console=ttyS0 root=/dev/vda rootfstype=ext4 ro net.ifnames=0}"
# The image identity this boot serves (#160): the daemon reports it
# in /health (msksd.image from /proc/cmdline), the drift check below
# and `msks ls` compare it with what the tree builds today, and a
# drifted appliance names itself instead of failing opaquely. A
# missing symlink is a named failure, not a bogus identity: with it
# absent, readlink -f echoes the literal path (rc=0), the daemon
# would report that, and the comparison would silently no-op (#160
# review).
if [ ! -e "$app_dir/image" ]; then
  echo "msks: $app_dir/image is missing; rebuild with: MSKS_APPLIANCE_DIR=$app_dir devenv tasks run msks:appliance-build" >&2
  exit 1
fi
booted_image="$(readlink -f "$app_dir/image")"
# Optional msksd.<name>=<value> pairs the operator wants bridged into
# the daemon's environment (e.g. msksd.vsock_wait_timeout_s=30 on
# slow nested-virt hosts); each becomes MSKSD_<NAME> in the guest.
: "${MSKS_APPLIANCE_CMDLINE_EXTRA:=}"
# The appliance VM's memory, MiB. Nested workspace VMs ride the same
# RAM: inside a 2 GiB appliance a 1 GiB guest beside the daemon and
# the OS OOM-kills the VMM (seen live, #77) — 6 GiB carries a
# workspace with headroom. An operator can shrink it back.
: "${MSKS_APPLIANCE_MEM_MIB:=6144}"
# The live dev-tree share (#144): set to any nonempty value and the
# run script shares this checkout ($DEVENV_ROOT) read-only into the
# guest as a second virtiofs tag (devtree), and tells the guest
# daemon to run from it (msksd.dev_tree on the cmdline). The guest
# mounts it at /run/msks-dev-tree and execs the checkout's venv
# python over the shared sources with --reload: a daemon edit on
# the host restarts the guest daemon in seconds — no appliance
# rebuild, no VM reboot. The share runs with --cache never: the
# reload watcher polls (mtime, size) fingerprints across virtiofs,
# and cached attrs would hide host edits from it. The store share
# keeps --cache auto — it is immutable, the dev tree is not.
: "${MSKS_DEV_TREE:=}"
dev_fs=""
dev_cmdline=""
if [ -n "$MSKS_DEV_TREE" ]; then
  rm -f "$app_dir/dev-sock" "$app_dir/dev-sock.pid"
  virtiofsd \
    --socket-path "$app_dir/dev-sock" \
    --shared-dir "$root" \
    --readonly \
    --sandbox none \
    --cache never \
    >"$app_dir/dev-virtiofsd.log" 2>&1 &
  devvfpid=$!
  for _ in $(seq 1 100); do
    [ -S "$app_dir/dev-sock" ] && break
    kill -0 "$devvfpid" 2>/dev/null || {
      echo "msks: dev-tree virtiofsd exited before serving (see $app_dir/dev-virtiofsd.log)" >&2
      exit 1
    }
    sleep 0.1
  done
  [ -S "$app_dir/dev-sock" ] || {
    echo "msks: dev-tree virtiofsd socket never appeared" >&2
    exit 1
  }
  dev_fs=$(printf ',\n    {"tag": "devtree", "socket": "%s/dev-sock",\n     "num_queues": 1, "queue_size": 1024}' "$app_dir")
  dev_cmdline=" msksd.dev_tree=/run/msks-dev-tree"
fi

# --- the store share (virtiofsd, unprivileged) --------------------------
rm -f "$app_dir/vmm-sock"
virtiofsd \
  --socket-path "$app_dir/vmm-sock" \
  --shared-dir /nix/store \
  --readonly \
  --sandbox none \
  --cache auto \
  >"$app_dir/virtiofsd.log" 2>&1 &
vfpid=$!
for _ in $(seq 1 100); do
  [ -S "$app_dir/vmm-sock" ] && break
  kill -0 "$vfpid" 2>/dev/null || {
    echo "msks: virtiofsd exited before serving (see $app_dir/virtiofsd.log)" >&2
    exit 1
  }
  sleep 0.1
done
[ -S "$app_dir/vmm-sock" ] || {
  echo "msks: virtiofsd socket never appeared" >&2
  exit 1
}

# --- the appliance VM ----------------------------------------------------
rm -f "$app_dir/api.sock"

# The VM is created and booted through the REST API rather than CLI
# payload flags: vm.create accepts the full disk schema (image_type),
# while the CLI parser rejects several of its fields. Every disk
# carries an explicit image_type: v52's autodetection "disables
# sector 0 writes" on raw images that do not declare one — a
# QCOW2-misdetection guard that breaks any guest writing an ext4
# superblock (sector 0).
boot_vm() {
  for _ in $(seq 1 100); do
    [ -S "$app_dir/api.sock" ] && break
    sleep 0.1
  done
  [ -S "$app_dir/api.sock" ] || {
    echo "msks: cloud-hypervisor API socket never appeared" >&2
    return 1
  }
  api() {
    # --fail-with-body: an HTTP 4xx/5xx from vm.create/vm.boot must
    # fail loudly, not leave a dead VM with no message.
    curl -sS --fail-with-body --unix-socket "$app_dir/api.sock" -X PUT \
      -H 'content-type: application/json' \
      -d "@-" "http://localhost/api/v1/$1"
  }
  # The payload is heredoc-interpolated JSON: a double quote in the
  # bootstrap token or MSKS_APPLIANCE_CMDLINE_EXTRA fails the request
  # loudly (curl --fail-with-body) — acceptable for values this
  # host's own files and environment supply.
  cat <<JSON | api vm.create
{
  "cpus": {"boot_vcpus": 2, "max_vcpus": 2},
  "memory": {"size": $((MSKS_APPLIANCE_MEM_MIB * 1024 * 1024)), "shared": true},
  "payload": {
    "kernel": "$app_dir/vmlinux",
    "initramfs": "$app_dir/initrd",
    "cmdline": "$base_cmdline msksd.bootstrap_token=$bootstrap_token msksd.default_image=$default_image msksd.image=$booted_image$dev_cmdline $MSKS_APPLIANCE_CMDLINE_EXTRA"
  },
  "disks": [
    {"path": "$app_dir/rootfs.ext4", "readonly": true, "image_type": "Raw"},
    {"path": "$state_disk", "image_type": "Raw"}
  ],
  "fs": [
    {"tag": "store", "socket": "$app_dir/vmm-sock",
     "num_queues": 1, "queue_size": 1024}$dev_fs
  ],
  "net": [
    {"tap": "mskstap0", "mac": "52:54:00:00:00:01"}
  ],
  "serial": {"mode": "File", "file": "$app_dir/serial.log"},
  "console": {"mode": "Off"}
}
JSON
  api vm.boot || return 1
  echo "msks: appliance booting — https://$guest_ip:8660 (TOFU fingerprint in $app_dir/serial.log)"
}
# The booter races the VMM's own startup — it exits as soon as
# vm.boot is accepted.
boot_vm &
booter=$!

# Invoked by the TERM/INT trap below; same SC2329 false positive
# as cleanup() above.
# shellcheck disable=SC2329
graceful() {
  echo "msks: stopping the appliance (ACPI, then SIGTERM)"
  # The guest's logind turns the ACPI power button into a clean
  # shutdown; bounded wait, then the hard stop. vm.power-button is
  # the ACPI press — vm.shutdown would be the hard stop itself.
  # The window is 60s, not 10s: a workspace running inside the
  # appliance needs its own stop cycle (nested VMM ACPI), and a TERM
  # that lands mid-shutdown loses everything still sitting in the
  # guest's page cache — observed live: a sqlite row committed only
  # to the WAL vanished when the 10s window expired under a running
  # workspace, while artifacts written with fsync survived.
  curl -sS --unix-socket "$app_dir/api.sock" -X PUT \
    http://localhost/api/v1/vm.power-button >/dev/null 2>&1 || true
  for _ in $(seq 1 300); do
    kill -0 "$chpid" 2>/dev/null || break
    sleep 0.2
  done
  kill "$chpid" 2>/dev/null || true
}
trap graceful TERM INT

cloud-hypervisor \
  --api-socket "$app_dir/api.sock" \
  >"$app_dir/cloud-hypervisor.log" 2>&1 &
chpid=$!

# The client's verified-CA preset (#146): once the guest serves, its
# CA cert (minted on the state disk at first boot, path /msksd) is
# extracted read-only from the state disk to
# <appliance state>/msks-ca.pem — a fresh devenv shell then presets MSKSC_CAFILE to it and the client
# verifies the appliance instead of warning. The read retries: on a
# freshly minted CA the cert's data blocks sit in the ext4 JOURNAL
# until the guest checkpoints them, and debugfs (no journal replay)
# reads the checkpointed state only — an immediate read returns
# empty. Best-effort and self-terminating either way: an empty result
# leaves the TOFU fingerprint on the serial log as the fallback, and
# the next boot retries.
(
  # errexit-safe polling: the script runs under set -e, and a bare
  # `curl && break` dies on the first refused connect (the guest is
  # not up yet) — the `if` condition is exempt.
  for _ in $(seq 1 90); do
    if curl -sk "https://$guest_ip:8660/api/v1/health" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  # The tmp name carries this script's pid: a SIGKILL'd instance's
  # subshell lives on up to the window above, and a fixed name would
  # let a replacement instance's extractor race it on the same file.
  # The content check covers the same journal-lag class as the retry:
  # a checkpointed inode with uncheckpointed data blocks reads back
  # zeros — full-size, non-empty, not a certificate.
  tmp="$app_dir/.msks-ca.pem.tmp.$$"
  for _ in $(seq 1 24); do
    if debugfs -R "cat /msksd/msks-ca.pem" "$state_disk" >"$tmp" 2>/dev/null &&
      grep -q "BEGIN CERTIFICATE" "$tmp"; then
      mv "$tmp" "$app_dir/msks-ca.pem"
      exit 0
    fi
    sleep 5
  done
  rm -f "$tmp"
) &

# --- converge on up (#160): the guest must serve, loudly ---------
# `devenv processes up` reporting a ready process while the guest
# never reaches its API is the silent-failure class #158 named: the
# operator's first signal was an msks ssh failure against the wrong
# closure. The gate blocks until the daemon answers /health — then
# names the image it serves — and a boot that never serves within
# the window exits with the named cause (serial log path); the
# supervisor restarts it, and a persistently broken image reaches
# gave_up loudly instead of idling as a "ready" appliance.
: "${MSKS_APPLIANCE_SERVE_TIMEOUT_S:=300}"
served=""
vmm_died=""
for _ in $(seq 1 "$MSKS_APPLIANCE_SERVE_TIMEOUT_S"); do
  if curl -sk --connect-timeout 2 "https://$guest_ip:8660/api/v1/health" >/dev/null 2>&1; then
    served=1
    break
  fi
  if ! kill -0 "$chpid" 2>/dev/null; then
    vmm_died=1
    break
  fi
  sleep 1
done
if [ -z "$served" ]; then
  if [ -n "$vmm_died" ]; then
    echo "msks: appliance VMM exited before serving — cloud-hypervisor.log: $app_dir/cloud-hypervisor.log, serial: $app_dir/serial.log" >&2
  else
    echo "msks: appliance guest never served https://$guest_ip:8660 within ${MSKS_APPLIANCE_SERVE_TIMEOUT_S}s — serial log: $app_dir/serial.log" >&2
  fi
  exit 1
fi
echo "msks: appliance serving (image $booted_image)"

# --- opt-in drift auto-restart (#160) ------------------------------
# MSKS_APPLIANCE_AUTO_RESTART=1: while the appliance runs, compare
# the booted image with the tree's current build; on drift, with no
# workspace running, rebuild and restart into the fresh image — the
# whole `up` update story, unattended. A workspace in any live-ish
# state holds the restart off (the documented behavior: a live
# workspace keeps its appliance until it stops). The loop is tied to
# this VMM's lifetime and never fires under MSKS_DEV_TREE — a
# dev-tree daemon serves the live tree, and restarting its appliance
# on image drift would churn for nothing.
if [ "${MSKS_APPLIANCE_AUTO_RESTART:-}" = "1" ] && [ -z "$MSKS_DEV_TREE" ]; then
  (
    interval="${MSKS_APPLIANCE_DRIFT_CHECK_S:-300}"
    # A non-numeric interval would kill the subshell at its first
    # sleep; fall back to the default and keep watching (#160 review).
    case "$interval" in
    "" | *[!0-9]*) interval=300 ;;
    esac
    while kill -0 "$chpid" 2>/dev/null; do
      sleep "$interval"
      kill -0 "$chpid" 2>/dev/null || break
      current="$(readlink -f "$app_dir/image" 2>/dev/null || true)"
      [ -n "$current" ] || continue
      [ "$current" = "$booted_image" ] && continue
      rows="$(curl -sk --connect-timeout 2 -H "Authorization: Bearer $bootstrap_token" \
        "https://$guest_ip:8660/api/v1/workspaces" 2>/dev/null)" || continue
      state="$(python3 -c '
import json, sys
try:
    rows = json.loads(sys.argv[1])
except Exception:
    # An unreadable answer never restarts anything: retry next cycle.
    print("busy")
    sys.exit()
live = ("starting", "running", "paused", "unknown")
print("busy" if any(r.get("status") in live for r in rows) else "idle")
' "$rows" 2>/dev/null || echo busy)"
      if [ "$state" != "idle" ]; then
        echo "msks: image drifted to $current but a workspace is live; holding off" >&2
        continue
      fi
      echo "msks: image drifted ($booted_image -> $current); rebuilding and restarting" >&2
      devenv tasks run msks:appliance-build || continue
      # TERM to this script runs the graceful trap (ACPI, then
      # SIGTERM); the supervisor restarts the process into the
      # freshly built image.
      kill -TERM "$$"
      exit 0
    done
  ) &
fi

# errexit-safe: a nonzero wait (crash, SIGKILL, SIGTERM) must not
# kill the script before the booter is reaped and the diagnostic
# prints — the exit status still reaches the supervisor either way.
rc=0
wait "$chpid" || rc=$?
wait "$booter" 2>/dev/null || true
echo "msks: appliance VMM exited (rc=$rc); the supervisor decides what happens next"
exit "$rc"
