#!/usr/bin/env bash
# The appliance as ONE run script (#25): the devenv process manager
# (#146) execs it as the `appliance` process — the build runs first
# inside the process exec — and the TERM/INT trap below owns the
# graceful choreography (the msks-appliance-up/-down scripts wrap the
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
  # The VMM too: the serve gate below can exit 1 while the guest
  # still runs, and an orphaned cloud-hypervisor keeps the state
  # disk attached behind an api.sock this trap is about to unlink —
  # the next start's double-up probe would then pass and boot a
  # SECOND VMM onto the same raw ext4 (#189 review). TERM is a hard
  # poweroff for a guest that never served — that is the only path
  # that reaches here with the VMM alive; every other exit already
  # reaped it in `wait "$chpid"` (and the reap clears chpid below,
  # so this kill cannot reach a recycled pid either).
  kill "${chpid:-}" 2>/dev/null || true
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
# The image's store shape (#212): debian boots the rootfs disk with
# the host store shared over virtiofs; the NixOS dev shape shares the
# same store and boots kernel+initrd+cmdline with no rootfs disk; the
# NixOS deployed shape carries the store on disk (erofs base + store
# volume) and shares nothing.
appliance_mode="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("mode", "debian"))' "$app_dir/appliance-manifest.json")"
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
  echo "msks: $app_dir/image is missing; rebuild with: MSKS_APPLIANCE_DIR=$app_dir msks-appliance-build" >&2
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
# The serial console backend (#189): File by default — the serial
# log is the recovery surface (the TOFU fingerprint fallback, the
# boot markers, the failure paths below point at it). Set
# MSKS_APPLIANCE_CONSOLE=pty for a host pty instead: cloud-
# hypervisor records the pty path where `ch-remote info` reports it
# (config.serial.file), and the guest's msks-debug-shell.service
# (behind the state-disk debug-shell marker) serves an interactive
# root shell on it — msks-appliance-shell drives this whole path.
# An empty value (or file) keeps the default; anything but those
# two names is a typo, and a typo here would silently cost the
# serial log — refuse it.
serial_config="{\"mode\": \"File\", \"file\": \"$app_dir/serial.log\"}"
serial_mode="file"
serial_note="$app_dir/serial.log"
case "${MSKS_APPLIANCE_CONSOLE:-}" in
"") ;;
file) ;;
pty)
  serial_config='{"mode": "Pty"}'
  serial_mode=pty
  serial_note="the pty console (MSKS_APPLIANCE_CONSOLE=pty)"
  ;;
*)
  echo "msks: MSKS_APPLIANCE_CONSOLE must be 'pty' or 'file' (got '$MSKS_APPLIANCE_CONSOLE')" >&2
  exit 1
  ;;
esac

# A stale debug-shell marker with the console in File mode (#189
# review): File-mode serial has no input path, so the guest's
# msks-debug-shell.service spends the boot blocked on a read that
# never delivers — the root shell is unreachable. Name it loudly,
# with the way out, instead of letting a SIGKILL'd helper session
# (or a host crash) leave every later boot quietly degraded. A
# read-only debugfs stat on the still-unattached disk, before the
# VMM exists: ~10ms, and no journal to replay yet.
# debugfs exits 0 even on a miss ("File not found by ext2_lookup" on
# stderr, empty stdout), so the probe matches non-empty stdout, not
# the exit code — else every marker-less disk prints the warning.
if [ "$serial_mode" = file ] &&
  debugfs -R "stat /debug-shell" "$state_disk" 2>/dev/null | grep -q .; then
  echo "msks: the state disk carries a debug-shell marker, but the File-mode console cannot host its shell — run: msks-appliance-shell --off (or open a session with: msks-appliance-shell)" >&2
fi
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
# Dev shapes only: the deployed appliance's store is on disk and
# there is nothing to share. mskstap0 is the shared NIC either way.
share_store=1
case "$appliance_mode" in
deployed) share_store= ;;
debian | dev) ;;
*)
  echo "msks: unknown appliance mode '$appliance_mode' in $app_dir/appliance-manifest.json" >&2
  exit 1
  ;;
esac
if [ -n "$share_store" ]; then
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
fi

# --- the appliance VM ----------------------------------------------------
rm -f "$app_dir/api.sock"

# The boot source: the image's shipped artifacts (first boot, dev,
# Debian) or — deployed, after any update — the generation the
# appliance profile pointed at when msks-appliance-update cached it
# host-side. boot-cache/current beats the shipped pair: an updated
# appliance boots its NEW generation, not the image's original one;
# without the cache an update would land in the profile and never be
# booted (the store volume keeps both, so the shipped pair keeps
# booting fine — just stale). Resolved HERE, in the main shell:
# boot_vm runs backgrounded below, and its variable writes would
# die with its subshell — the fallback branch and the ready line
# both read these from the main shell.
resolve_boot_source() {
  boot_kernel="$app_dir/vmlinux"
  boot_initrd="$app_dir/initrd"
  boot_cmdline="$base_cmdline"
  boot_erofs="$app_dir/base-store.erofs"
  boot_source="the image's shipped artifacts"
  boot_source_note=""
  if [ "$appliance_mode" = deployed ] && [ -f "$app_dir/boot-cache/current/kernel" ]; then
    boot_kernel="$app_dir/boot-cache/current/kernel"
    boot_initrd="$app_dir/boot-cache/current/initrd"
    boot_cmdline="$(cat "$app_dir/boot-cache/current/cmdline")"
    boot_source="boot-cache/current"
    boot_source_note="$(cat "$app_dir/boot-cache/current/profile" 2>/dev/null || echo boot-cache/current)"
    # The generation's own erofs (the update pinned the era's base
    # beside it): a later image rebuild swapped <state>/base-store.erofs
    # for a lower this generation's upper was never written against,
    # and mismatched-era boots freeze at switch-root (found live,
    # #220). Without a pinned base the shipped one stays — the
    # earliest caches predate the pin.
    if [ -f "$app_dir/boot-cache/current/base-store.erofs" ]; then
      boot_erofs="$app_dir/boot-cache/current/base-store.erofs"
    fi
  fi
}
resolve_boot_source

# The disk set follows the mode: the rootfs disk is the Debian
# appliance's; the NixOS appliance direct-boots (no rootfs), and the
# deployed shape adds the erofs base (read-only) and the store
# volume. Every disk carries an explicit image_type: v52's raw-image
# autodetection breaks guests writing an ext4 superblock (sector 0).
# The deployed letters are PINNED to the config's expectations
# (nix/appliance-config.nix): vda erofs, vdb store volume, vdc state
# — the payload order below is that order.
# The store volume path resolves ONCE, exactly as appliance-setup.sh
# resolves it (relative values land below the app dir, never the
# invoking CWD — a CWD-relative attach would pass setup's check and
# then hand the VMM a different file, #221 review).
store_volume="${MSKS_APPLIANCE_STORE_VOLUME:-$app_dir/store-volume.img}"
case "$store_volume" in
/*) ;;
*) store_volume="$app_dir/$store_volume" ;;
esac
case "$appliance_mode" in
debian)
  disks=$(printf '    {"path": "%s/rootfs.ext4", "readonly": true, "image_type": "Raw"},\n    {"path": "%s", "image_type": "Raw"}' "$app_dir" "$state_disk")
  ;;
dev)
  disks=$(printf '    {"path": "%s", "image_type": "Raw"}' "$state_disk")
  ;;
deployed)
  # $boot_erofs: the cache's pinned base when a cached generation
  # boots (resolve_boot_source), the image's otherwise.
  disks=$(printf '    {"path": "%s", "readonly": true, "image_type": "Raw"},\n    {"path": "%s", "image_type": "Raw"},\n    {"path": "%s", "image_type": "Raw"}' \
    "$boot_erofs" "$store_volume" "$state_disk")
  ;;
esac

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
    "kernel": "$boot_kernel",
    "initramfs": "$boot_initrd",
    "cmdline": "$boot_cmdline msksd.bootstrap_token=$bootstrap_token msksd.default_image=$default_image msksd.image=$booted_image$dev_cmdline $MSKS_APPLIANCE_CMDLINE_EXTRA"
  },
  "disks": [
$disks
  ],
  "fs": [$(
    if [ -n "$share_store" ]; then
      printf '\n    {"tag": "store", "socket": "%s",\n     "num_queues": 1, "queue_size": 1024}%s' "$app_dir/vmm-sock" "$dev_fs"
    else
      # dev_fs carries a leading comma for the share case; alone in
      # the array it must arrive bare.
      printf '%s' "${dev_fs#,}"
    fi
  )
  ],
  "net": [
    {"tap": "mskstap0", "mac": "52:54:00:00:00:01"}
  ],
  "serial": $serial_config,
  "console": {"mode": "Off"}
}
JSON
  api vm.boot || return 1
  # This hint fires whenever no CA has landed yet — a first boot,
  # or a manually deleted msks-ca.pem: the guest mints its CA
  # at first start and the extractor below lands msks-ca.pem once
  # it serves — fresh devenv shells pick it up as MSKSC_CAFILE, so
  # connects verify and every later boot's line stays short. A
  # REPLACED state disk takes the short line too, misleadingly: the
  # stale msks-ca.pem stays on the host until the extractor
  # refreshes it, and verification against it fails until then —
  # pre-existing, documented behavior (README, the client presets
  # section).
  if [ -s "$app_dir/msks-ca.pem" ]; then
    echo "msks: appliance booting — https://$guest_ip:8660"
  else
    echo "msks: appliance booting — https://$guest_ip:8660 (first boot: msks warns it does not verify until the CA lands at $app_dir/msks-ca.pem — once serving, open a fresh devenv shell and it verifies)"
  fi
}
# The booter races the VMM's own startup — it exits as soon as
# vm.boot is accepted.
boot_vm &
booter=$!

# Invoked by the TERM/INT trap below; same SC2329 false positive
# as cleanup() above.
# shellcheck disable=SC2329
graceful() {
  # Mark the stop as REQUESTED before anything else: the VMM exit
  # this choreography causes must read below as the stop completing,
  # not as a crash.
  stopping=1
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
# await_serve — the gate as a function: the deployed fallback below
# reuses it for its retry boot. Returns 0 on serving; on failure the
# globals served/vmm_died carry which way it failed and the caller
# decides (a stop request during the wait is a completed stop, not a
# boot failure — clean exit so the supervisor does not restart a
# deliberately stopped process).
await_serve() {
  served=""
  vmm_died=""
  local _
  for _ in $(seq 1 "$MSKS_APPLIANCE_SERVE_TIMEOUT_S"); do
    if curl -sk --connect-timeout 2 "https://$guest_ip:8660/api/v1/health" >/dev/null 2>&1; then
      served=1
      return 0
    fi
    if ! kill -0 "$chpid" 2>/dev/null; then
      vmm_died=1
      return 1
    fi
    sleep 1
  done
  return 1
}
# || true: the gate's nonzero return is its MESSAGE (the globals
# carry the verdict) — under set -e a bare call would exit here,
# before the fallback below could run (found live, #220).
await_serve || true
if [ -z "$served" ] && [ -z "${stopping:-}" ]; then
  # The deployed fallback (#220, spike 3's boot-pointer answer): a
  # cached generation that fails to serve — a broken update — boots
  # the PREVIOUS cached generation (or the image's shipped pair when
  # there is none) once, before giving up. The broken cache entry
  # moves aside (.fell-back-<ts>, kept for forensics; the run script
  # never auto-boots it again) and `current` re-points at whatever
  # served. The guest profile stays aimed at the broken generation —
  # msks-appliance-update rewinds it to what actually runs. Only a
  # cache boot with a live VMM reaches here: the shipped pair has
  # nothing better to fall back to, and a VMM that died mid-boot
  # cannot be rebooted in place — both take the original exit below.
  if [ "$appliance_mode" = deployed ] &&
    [ "$boot_source" != "the image's shipped artifacts" ] && [ -z "$vmm_died" ]; then
    fb_target="shipped"
    if [ -f "$app_dir/boot-cache/previous/kernel" ]; then
      fb_target="previous"
    fi
    echo "msks: cached generation never served ($boot_source) — falling back to the $fb_target generation, serial: $serial_note" >&2
    mv -f "$app_dir/boot-cache/current" \
      "$app_dir/boot-cache/.fell-back-$(date +%s)"
    if [ "$fb_target" = previous ]; then
      mv -fT "$app_dir/boot-cache/previous" "$app_dir/boot-cache/current"
    else
      rm -f "$app_dir/boot-cache/previous"
    fi
    # Preserve the failed boot's serial evidence BEFORE the new VMM
    # exists: cloud-hypervisor opens the serial file with truncate at
    # vm.create (vmm/src/console_devices.rs), and the fallback's own
    # message points AT that log as the record of why the generation
    # failed (#221 review — the asymmetry with cloud-hypervisor.log,
    # which the fallback spawn appends to, shows preservation was the
    # intent).
    mv -f "$app_dir/serial.log" \
      "$app_dir/serial.log.fell-back-$(date +%s)" 2>/dev/null || true
    # Re-resolve against the moved links — boot_vm reads these
    # globals, and they still point at the broken generation.
    resolve_boot_source
    # Power off the dead guest, then start a FRESH VMM for the
    # fallback source. vm.shutdown is the hard stop: the guest never
    # served, so there is no graceful state to lose. A vm.delete +
    # re-create on the SAME VMM was tried and the recreated guest
    # froze at boot (systemd finding no units — the read-only erofs
    # re-attach through a reused VMM does not come up the same);
    # cloud-hypervisor exits cleanly on vm.shutdown's power-off when
    # no other VM holds it, so reap it and spawn a new one — the
    # spike-3 harness's answer, and what a supervisor restart would
    # do anyway, minus one crash cycle (#220).
    curl -sS --unix-socket "$app_dir/api.sock" -X PUT \
      http://localhost/api/v1/vm.shutdown >/dev/null 2>&1 || true
    for _ in $(seq 1 50); do
      kill -0 "$chpid" 2>/dev/null || break
      sleep 0.2
    done
    kill "$chpid" 2>/dev/null || true
    wait "$chpid" 2>/dev/null || true
    rm -f "$app_dir/api.sock" "$app_dir/vmm-sock" "$app_dir/vmm-sock.pid"
    # A stop that landed while the old VMM died: honor it instead of
    # booting a fallback nobody asked for — graceful pressed power on
    # the dead socket above, and a freshly spawned VMM would run
    # un-hit through the whole serve window before "stopped" (#221
    # review).
    if [ -n "${stopping:-}" ]; then
      echo "msks: appliance stopped during boot"
      exit 0
    fi
    cloud-hypervisor \
      --api-socket "$app_dir/api.sock" \
      >>"$app_dir/cloud-hypervisor.log" 2>&1 &
    chpid=$!
    boot_vm || {
      echo "msks: the fallback boot's vm.create/vm.boot failed — cloud-hypervisor.log: $app_dir/cloud-hypervisor.log, serial: $serial_note" >&2
      exit 1
    }
    await_serve || true
  fi
fi
if [ -z "$served" ]; then
  if [ -n "${stopping:-}" ]; then
    echo "msks: appliance stopped during boot"
    exit 0
  fi
  if [ -n "$vmm_died" ]; then
    echo "msks: appliance VMM exited before serving — cloud-hypervisor.log: $app_dir/cloud-hypervisor.log, serial: $serial_note" >&2
  else
    echo "msks: appliance guest never served https://$guest_ip:8660 within ${MSKS_APPLIANCE_SERVE_TIMEOUT_S}s — serial: $serial_note" >&2
  fi
  exit 1
fi
# The URL is the actionable bit on the ready line (the issue-#176
# reader looks here for "what do I connect to"); the image rides
# along by its short name only — the build line above already
# printed the full store path once.
echo "msks: appliance serving — https://$guest_ip:8660 (image ${booted_image##*/})${boot_source_note:+ (booted from $boot_source_note)}"
# Record the base this system booted with, as a HARD LINK (a later
# image rebuild replaces <state>/base-store.erofs by path; the link
# keeps the booted inode pinned). msks-appliance-update pins a
# generation's erofs from THIS file — the overlay's upper is
# consistent with exactly this era (#220).
if [ "$appliance_mode" = deployed ]; then
  rm -f "$app_dir/boot-cache/booted-base.erofs"
  mkdir -p "$app_dir/boot-cache"
  ln -f "$boot_erofs" "$app_dir/boot-cache/booted-base.erofs" 2>/dev/null ||
    cp -L "$boot_erofs" "$app_dir/boot-cache/booted-base.erofs"
fi

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
      # The process exec exports MSKS_GUEST_NIXPKGS (the pinned
      # source) before this script starts; the build script inherits
      # it here.
      bash "$DEVENV_ROOT/scripts/build-appliance.sh" || continue
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
# The reap releases the pid for reuse; clear it so the EXIT trap's
# TERM above can never reach an innocent recycled pid (the
# post-reap kill would otherwise be a live round against the pid
# space, microscopic odds — closed outright instead).
chpid=""
wait "$booter" 2>/dev/null || true
# Name the exit for the console reader. A requested stop ends
# calmly: the line says "stopped", full stop — the choreography
# line above already names ACPI and SIGTERM, and rc here is the
# SHELL's own trap-interrupted wait (128 + the request's signal),
# not the VMM's exit: a fast ACPI poweroff exits 0 and still
# reports 143, so decorating the line would misname the VMM's
# death (#176). A clean exit nobody requested (the guest powered
# itself off, or someone drove vm.shutdown through the API socket)
# stays stopped: devenv's default restart policy is on_failure
# (five attempts), so exit 0 is final and the line names the way
# back instead of a restart that never comes. Anything else is a
# failure: the supervisor restarts it, and the why lives in the
# two logs, not the manager's replay of the console.
sig=""
if [ "$rc" -gt 128 ]; then
  name="$(kill -l "$rc" 2>/dev/null || true)"
  if [ -n "$name" ]; then sig=" (SIG$name)"; fi
fi
if [ -n "${stopping:-}" ]; then
  echo "msks: appliance stopped"
elif [ "$rc" -eq 0 ]; then
  echo "msks: appliance VMM exited cleanly (rc=0); it stays stopped — restart it with: devenv processes restart appliance"
else
  echo "msks: appliance VMM exited unexpectedly — rc=$rc$sig; the supervisor restarts it — cloud-hypervisor.log: $app_dir/cloud-hypervisor.log, serial: $serial_note" >&2
fi
exit "$rc"
