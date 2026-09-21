#!/usr/bin/env bash
# Spike 211-3 for #211/#205: the deployed update path end to end.
#
#   1. Boot the spike-2 store shape with sshd on the msks bridge
#      (192.168.77.2), key-only root, every ssh session speaking the
#      local-overlay store via sshd SetEnv.
#   2. nixos-rebuild boot --target-host for three generations:
#      B (marker-only), C (pulls a new package), broken (unroutable
#      sshd) — measuring copy deltas and update wall time.
#   3. Boot each from the appliance-side system profile (kernel,
#      initrd, and kernel-params pulled over ssh), verifying the new
#      generation runs.
#   4. Fallback: the broken generation fails boot-to-ssh within its
#      timeout; the previous generation boots and recovers.
#
# The host bridge (msksbr0 192.168.77.1/24) and tap (mskstap0) are the
# operator-installed pieces the msks appliance uses; this spike borrows
# them exclusively and refuses to start while any cloud-hypervisor
# (the appliance included) is running.
#
# Run from a devenv shell:  bash scripts/spike-211-3.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"
state_dir="${MSKS_SPIKE211_DIR:-$root/.devenv/state/spike211-3}"
case "$state_dir" in
/*) ;;
*) state_dir="$root/$state_dir" ;;
esac
mkdir -p "$state_dir"

boot_timeout_s="${MSKS_SPIKE211_BOOT_TIMEOUT_S:-60}"
case "$boot_timeout_s" in
'' | *[!0-9]*)
  echo "spike211: MSKS_SPIKE211_BOOT_TIMEOUT_S must be a positive integer (got '$boot_timeout_s')" >&2
  exit 1
  ;;
esac

guest_ip=192.168.77.2
# IdentitiesOnly: never offer the invoking user's agent/default keys —
# the retry loops would hammer ssh-agent (and its passphrase prompts)
# while the guest is unreachable. BatchMode: no interactive prompt of
# any kind can hang the harness.
ssh_opts=(-o IdentitiesOnly=yes -o BatchMode=yes
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR -o ConnectTimeout=5 -i "$state_dir/spike3-key")
# The remote command strings are static; $guest_ip expanding on the
# client side is exactly what we want.
# shellcheck disable=SC2029
ssh_guest() { ssh "${ssh_opts[@]}" "root@${guest_ip}" "$@"; }

# --- preflight: bridge, tap, exclusive use --------------------------------
ip addr show msksbr0 2>/dev/null | grep -q "inet ${guest_ip%.*}.1/" || {
  echo "spike211: msksbr0 must hold ${guest_ip%.*}.1/24 (operator-installed; see docs/networking.md)" >&2
  exit 1
}
ip link show mskstap0 >/dev/null 2>&1 || {
  echo "spike211: mskstap0 is missing (operator-installed tap for the appliance bridge)" >&2
  exit 1
}
if pgrep -x cloud-hypervisor >/dev/null; then
  echo "spike211: a cloud-hypervisor is already running — this spike needs the bridge exclusively" >&2
  exit 1
fi
if [ ! -e "$state_dir/spike3-key" ]; then
  ssh-keygen -q -t ed25519 -N '' -f "$state_dir/spike3-key" >/dev/null
fi
export NIX_PATH="spike3-ssh-key=$state_dir/spike3-key.pub:${NIX_PATH:-}"

# nix-copy-closure and nixos-rebuild spawn their own ssh with default
# config: they would not offer the spike key and would fail host-key
# verification (each fresh volume mints new host keys). A dedicated
# config plus an ssh wrapper on PATH points every spawned ssh at it.
real_ssh=$(command -v ssh)
mkdir -p "$state_dir/bin"
cat >"$state_dir/ssh_config" <<CONF
Host ${guest_ip}
  StrictHostKeyChecking no
  UserKnownHostsFile $state_dir/known_hosts
  IdentityFile $state_dir/spike3-key
  IdentitiesOnly yes
  LogLevel ERROR
  ConnectTimeout 5
CONF
cat >"$state_dir/bin/ssh" <<WRAPPER
#!/usr/bin/env bash
exec "$real_ssh" -F "$state_dir/ssh_config" "\$@"
WRAPPER
chmod +x "$state_dir/bin/ssh"

# --- build generation A (cached no-op when unchanged) ---------------------
echo "spike211: building generation A (first build substitutes the NixOS closure)"
for attr in spike lowerImage upperVolume; do
  SPIKE3_GEN=A nix-build -I nixpkgs="$nixpkgs" \
    "$root/nix/spike-211-3.nix" -A "$attr" \
    -o "$state_dir/$attr" >/dev/null
done
image="$(readlink -f "$state_dir/spike")"
lower="$(readlink -f "$state_dir/lowerImage")"

volume="$state_dir/store-volume.img"
if [ ! -e "$volume" ]; then
  cp --reflink=auto "$(readlink -f "$state_dir/upperVolume")" "$volume.tmp"
  chmod u+w "$volume.tmp"
  mv -f "$volume.tmp" "$volume"
fi

run_dir="$state_dir/run"
if [ -f "$run_dir/ch.pid" ]; then
  old_pid=$(cat "$run_dir/ch.pid" 2>/dev/null || true)
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "spike211: another spike run is live (cloud-hypervisor pid $old_pid)" >&2
    exit 1
  fi
fi
rm -rf "$run_dir"
mkdir -p "$run_dir" "$state_dir/boot-cache"

# --- cloud-hypervisor lifecycle --------------------------------------------
ch_pid=""
stopping=0
cleanup() {
  if [ -n "$ch_pid" ] && kill -0 "$ch_pid" 2>/dev/null; then
    curl -sS --unix-socket "$run_dir/api.sock" -X PUT \
      http://localhost/api/v1/vm.power-button >/dev/null 2>&1 || true
    for _ in $(seq 1 50); do
      kill -0 "$ch_pid" 2>/dev/null || break
      sleep 0.2
    done
    kill "$ch_pid" 2>/dev/null || true
    wait "$ch_pid" 2>/dev/null || true
    ch_pid=""
  fi
}
on_signal() {
  stopping=1
  cleanup
  exit 130
}
trap on_signal INT TERM
trap cleanup EXIT

# boot_gen KERNEL INITRD CMDLINE — start CH with the given artifacts and
# wait for ssh. Sets BOOT_T to the boot-to-ssh time; returns nonzero if
# ssh never answers within boot_timeout_s (the fallback trigger).
BOOT_T=""
boot_gen() {
  local kernel="$1" initrd="$2" cmdline="$3" t0 t1 ok
  rm -f "$run_dir/api.sock" "$run_dir/serial.log"
  # Heredoc-interpolated JSON, the appliance-run.sh tradeoff: a double
  # quote or backslash in state_dir fails the request loudly (curl
  # --fail-with-body) rather than corrupting the payload silently.
  local payload
  payload="$(
    cat <<JSON
{
  "cpus": {"boot_vcpus": 2, "max_vcpus": 2},
  "memory": {"size": 1073741824},
  "payload": {
    "kernel": "$kernel",
    "initramfs": "$initrd",
    "cmdline": "$cmdline"
  },
  "disks": [
    {"path": "$lower", "readonly": true, "image_type": "Raw"},
    {"path": "$volume", "readonly": false, "image_type": "Raw"}
  ],
  "net": [
    {"tap": "mskstap0", "mac": "52:54:00:00:00:99"}
  ],
  "serial": {"mode": "File", "file": "$run_dir/serial.log"},
  "console": {"mode": "Off"}
}
JSON
  )"
  cloud-hypervisor --api-socket "$run_dir/api.sock" \
    >"$run_dir/cloud-hypervisor.log" 2>&1 &
  ch_pid=$!
  echo "$ch_pid" >"$run_dir/ch.pid"
  for _ in $(seq 1 100); do
    [ -S "$run_dir/api.sock" ] && break
    kill -0 "$ch_pid" 2>/dev/null || {
      echo "spike211: cloud-hypervisor exited early (see $run_dir/cloud-hypervisor.log)" >&2
      return 1
    }
    sleep 0.1
  done
  curl -sS --fail-with-body --unix-socket "$run_dir/api.sock" -X PUT \
    -H 'content-type: application/json' -d "$payload" \
    http://localhost/api/v1/vm.create
  t0=$(date +%s.%N)
  curl -sS --fail-with-body --unix-socket "$run_dir/api.sock" -X PUT \
    http://localhost/api/v1/vm.boot >/dev/null
  ok=0
  for _ in $(seq 1 $((boot_timeout_s * 2))); do
    if ssh_guest true 2>/dev/null; then
      ok=1
      break
    fi
    kill -0 "$ch_pid" 2>/dev/null || break
    sleep 0.5
  done
  t1=$(date +%s.%N)
  [ "$stopping" = 1 ] && exit 130
  if [ "$ok" = 1 ]; then
    BOOT_T=$(awk -v a="$t1" -v b="$t0" 'BEGIN { printf "%.1f", a - b }')
    return 0
  fi
  return 1
}

boot_or_die() {
  local name="$1"
  if ! boot_gen "$state_dir/boot-cache/$name/kernel" \
    "$state_dir/boot-cache/$name/initrd" \
    "$(cat "$state_dir/boot-cache/$name/cmdline")"; then
    echo "spike211: generation $name never answered ssh — serial tail:" >&2
    tail -20 "$run_dir/serial.log" >&2 || true
    exit 1
  fi
  echo "spike211: gen $name boot-to-ssh: ${BOOT_T}s, marker: $(ssh_guest cat /etc/spike3-generation)"
}

# pull_current_gen NAME — read the appliance-side system profile over
# ssh and cache its boot artifacts on the host. CH boots host-side
# files, so each generation's kernel and initrd are exported from the
# appliance through ssh; boot.json supplies the kernel command line.
PROFILE_PATH=""
pull_current_gen() {
  local name="$1"
  # The system profile exists only after the first switch; the first
  # boot's generation ships in the base, so fall back to the running
  # system.
  PROFILE_PATH=$(ssh_guest 'readlink -f /nix/var/nix/profiles/system 2>/dev/null || readlink -f /run/current-system')
  mkdir -p "$state_dir/boot-cache/$name"
  ssh_guest cat "$PROFILE_PATH/kernel" >"$state_dir/boot-cache/$name/kernel"
  ssh_guest cat "$PROFILE_PATH/initrd" >"$state_dir/boot-cache/$name/initrd"
  # The toplevel's kernel-params file carries no init= (bootspec
  # keeps the init separate), so compose the full command line here.
  {
    ssh_guest cat "$PROFILE_PATH/kernel-params"
    printf ' init=%s/init\n' "$PROFILE_PATH" # leading space: no trailing newline above
  } >"$state_dir/boot-cache/$name/cmdline"
  echo "$PROFILE_PATH" >"$state_dir/boot-cache/$name/profile"
}

upper_bytes() {
  ssh_guest "du -sb /nix/.upper-volume/store | cut -f1"
}

# rebuild GEN — nixos-rebuild boot --target-host; sets REBUILD_T.
REBUILD_T=""
rebuild() {
  local gen="$1" t0 t1
  echo "spike211: nixos-rebuild boot --target-host for generation $gen"
  t0=$(date +%s.%N)
  if ! SPIKE3_GEN="$gen" PATH="$state_dir/bin:$PATH" nixos-rebuild --no-flake boot \
    --target-host "root@${guest_ip}" \
    -I "nixpkgs=$nixpkgs" \
    -I "nixos-config=$root/nix/spike-211-3-config.nix" \
    >"$run_dir/rebuild-$gen.log" 2>&1; then
    echo "spike211: nixos-rebuild failed for $gen — log tail:" >&2
    tail -20 "$run_dir/rebuild-$gen.log" >&2
    exit 1
  fi
  t1=$(date +%s.%N)
  REBUILD_T=$(awk -v a="$t1" -v b="$t0" 'BEGIN { printf "%.1f", a - b }')
}

# update_cycle GEN PREV_BYTES — deploy, reboot from the profile, verify.
UPPER_BYTES=""
update_cycle() {
  local gen="$1" prev="$2" bytes
  rebuild "$gen"
  bytes=$(upper_bytes)
  echo "spike211: gen $gen: nixos-rebuild ${REBUILD_T}s, delta copy $((bytes - prev)) bytes (${bytes} total)"
  pull_current_gen "$gen"
  cleanup
  boot_or_die "$gen"
  UPPER_BYTES=$bytes
}

# --- phase 1: boot generation A --------------------------------------------
echo "spike211: booting generation A"
if ! boot_gen "$image/vmlinux" "$image/initrd" "$(cat "$image/cmdline")"; then
  echo "spike211: generation A never answered ssh — serial tail:" >&2
  tail -20 "$run_dir/serial.log" >&2 || true
  exit 1
fi
echo "spike211: gen A boot-to-ssh: ${BOOT_T}s, marker: $(ssh_guest cat /etc/spike3-generation)"
pull_current_gen A
bytes_A=$(upper_bytes)
echo "spike211: upper-layer baseline (generation A ships in the base image): ${bytes_A} bytes"

# --- phase 2: marker-only update (generation B) ----------------------------
update_cycle B "$bytes_A"
last_good=B

# --- phase 3: package-pulling update (generation C) ------------------------
update_cycle C "$UPPER_BYTES"
last_good=C
echo "spike211: gen C hello: $(ssh_guest hello | head -1)"
echo "spike211: generations retained on the volume: $(ssh_guest 'ls -d /nix/var/nix/profiles/system-*-link | wc -l')"

# --- phase 4: the broken generation and the fallback -----------------------
rebuild broken
echo "spike211: broken generation deployed in ${REBUILD_T}s"
pull_current_gen broken
cleanup
if boot_gen "$state_dir/boot-cache/broken/kernel" \
  "$state_dir/boot-cache/broken/initrd" \
  "$(cat "$state_dir/boot-cache/broken/cmdline")"; then
  echo "spike211: the broken generation answered ssh — the fallback demo is invalid" >&2
  exit 1
fi
echo "spike211: broken generation missed the ${boot_timeout_s}s boot-to-ssh budget (expected); serial tail:"
tail -5 "$run_dir/serial.log" || true
cleanup
echo "spike211: falling back to the last good generation ($last_good)"
boot_or_die "$last_good"
echo "spike211: fallback recovered; appliance profile still points at: $(ssh_guest readlink -f /nix/var/nix/profiles/system | xargs basename)"
echo "spike211: done — artifacts and boot caches under $state_dir"
