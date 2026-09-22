#!/usr/bin/env bash
# The deployed update channel (#220, spike 3's path in production
# form): nixos-rebuild boot --target-host against the appliance's
# bridge sshd, copying only missing store paths into the overlay's
# upper layer, with the appliance-side system profile as the
# generation pointer.
#
# What it does, in order:
#
#   1. Refuses anything but a serving deployed-mode appliance, with
#      the update key the image was built with (build-appliance.sh
#      seeds <state>/update-key once and bakes its public half into
#      root's authorized_keys at build time — an image built without
#      one stays locked, the safe default).
#   2. Rewinds the profile if a fallback boot left it pointing at a
#      generation the appliance is not running: the run script boots
#      the last good generation when the profile's choice fails to
#      serve, and this is where the pointer catches up (#212's
#      recorded decision — the run script boots, the update path
#      reconciles).
#   3. Runs nixos-rebuild boot --target-host with the mode pinned to
#      deployed for the evaluation (the config's plain-eval default
#      — a stray exported MSKS_APPLIANCE_MODE=dev would otherwise
#      silently evaluate a dev-shaped system that cannot boot on
#      the appliance; the hazard appliance-config.nix states).
#   4. Caches the new generation's boot artifacts (kernel, initrd,
#      command line) host-side, so the NEXT msks-appliance-up boots
#      the profile's generation instead of the image's — and a
#      generation that fails to serve falls back to the previous
#      cache entry.
#
# Activation is a reboot the operator owns:
#
#   msks-appliance-update && msks-appliance-down && msks-appliance-up
#
# Run inside the devenv shell. Everything lands in the appliance
# state dir (MSKS_APPLIANCE_DIR relocates it): update-key, boot-cache/,
# update.log.

set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
app_dir="${MSKS_APPLIANCE_DIR:-$root/.devenv/state/appliance}"
case "$app_dir" in
/*) ;;
*) app_dir="$root/$app_dir" ;;
esac
guest_ip="192.168.77.2"

die() {
  echo "msks: $*" >&2
  exit 1
}

[ -f "$app_dir/appliance-manifest.json" ] ||
  die "no appliance manifest in $app_dir — build first: msks-appliance-build"
mode="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("mode","debian"))' "$app_dir/appliance-manifest.json")"
[ "$mode" = deployed ] ||
  die "the appliance in $app_dir is '$mode', not deployed — the update channel is deployed-mode only"
[ -f "$app_dir/update-key" ] ||
  die "$app_dir/update-key is missing — this image was built without an update key; rebuild with msks-appliance-build to seed one"

# One update at a time: two concurrent runs would race on the pin,
# the profile rewind, and the current/previous rotation (#221
# review). mkdir is atomic; a stale lock names its owner.
update_lock="$app_dir/update.lock"
if ! mkdir "$update_lock" 2>/dev/null; then
  die "another msks-appliance-update holds $update_lock (started $(cat "$update_lock/pid" 2>/dev/null || echo '?'))"
fi
echo $$ >"$update_lock/pid"
# The pid file lives INSIDE the lock dir, so rmdir alone can never
# remove it — every run left a stale lock that blocked the next
# update until removed by hand (found live, #222). Remove the
# contents, then the dir — but only when the lock is still ours: an
# operator may have removed a stalled run's stale lock and let a
# second run take the name, and this trap must not release THAT
# run's lock out from under it (the pid check names the owner).
trap '[ "$(cat "$update_lock/pid" 2>/dev/null)" = "$$" ] && { rm -f "$update_lock/pid"; rmdir "$update_lock" 2>/dev/null; } || true' EXIT

curl -sk --connect-timeout 2 "https://$guest_ip:8660/api/v1/health" >/dev/null 2>&1 ||
  die "the appliance is not serving https://$guest_ip:8660 — start it first: msks-appliance-up"
# The era record is a prerequisite, checked before any build: without
# it the update cannot pin the generation's erofs (the pin block
# below names the why).
[ -f "$app_dir/boot-cache/booted-base.erofs" ] ||
  die "no booted-base record at $app_dir/boot-cache/booted-base.erofs — boot the appliance once with the current scripts (msks-appliance-down && msks-appliance-up), then update"

# nix-copy-closure and nixos-rebuild spawn their OWN ssh with the
# default config: it would offer the invoking user's agent keys
# (passphrase dialogs on a desktop, once per retry) and fail host-key
# verification against keys the state disk minted at first boot. A
# dedicated config plus an ssh wrapper on PATH points every spawned
# ssh at the update key and a pinned known_hosts (TOFU: pinned on
# first update, loud on rotation — a rotated host key is worth an
# operator's attention, not a silent accept).
mkdir -p "$app_dir/boot-cache"
real_ssh=$(command -v ssh)
ssh_known_hosts="$app_dir/update-known-hosts"
[ -f "$ssh_known_hosts" ] ||
  ssh-keyscan -T 5 -t ed25519 "$guest_ip" 2>/dev/null >"$ssh_known_hosts" ||
  die "could not pin $guest_ip's host key (ssh-keyscan) — is sshd up?"
cat >"$app_dir/update-ssh-config" <<CONF
Host $guest_ip
  UserKnownHostsFile $ssh_known_hosts
  IdentityFile $app_dir/update-key
  IdentitiesOnly yes
  LogLevel ERROR
  ConnectTimeout 10
CONF
mkdir -p "$app_dir/update-bin"
cat >"$app_dir/update-bin/ssh" <<WRAPPER
#!/usr/bin/env bash
exec "$real_ssh" -F "$app_dir/update-ssh-config" "\$@"
WRAPPER
chmod +x "$app_dir/update-bin/ssh"

guest() {
  local probe
  probe=$("$app_dir/update-bin/ssh" -o BatchMode=yes "root@$guest_ip" true 2>&1) ||
    case "$probe" in
    *"Host key verification failed"* | *"host key has changed"*)
      # The pin doing its job: rotation is worth an operator's eyes.
      # The one legitimate re-pin is a reseeded state disk (host keys
      # live there); anything else, investigate before deleting the
      # pin.
      die "ssh to $guest_ip failed on the pinned host key — re-pin ONLY after reseeding the state disk: rm $ssh_known_hosts"
      ;;
    *)
      die "ssh to $guest_ip failed (is the appliance serving?) — $probe"
      ;;
    esac
  "$app_dir/update-bin/ssh" "root@$guest_ip" "$@"
}

# --- profile rewind ------------------------------------------------------
# The generation the appliance RUNS is the truth; the profile only
# points at what the next boot tries. A fallback boot leaves the
# profile aimed at the broken generation — re-point it at the running
# one before the update builds on top of reality.
serving="$(guest readlink -f /run/current-system)"
profile_target="$(guest readlink -f /nix/var/nix/profiles/system 2>/dev/null || echo "$serving")"
if [ "$serving" != "$profile_target" ]; then
  gen_link="$(guest "for l in /nix/var/nix/profiles/system-*-link; do [ \"\$(readlink -f \"\$l\")\" = '$serving' ] && basename \"\$l\" && break; done")"
  [ -n "$gen_link" ] ||
    die "profile points at $profile_target but $serving is running, and no system-*-link resolves to it"
  guest "ln -sfn '$gen_link' /nix/var/nix/profiles/system"
  echo "msks: profile rewound to $gen_link (was pointing at the fallback-booted-away generation)"
fi

# --- the rebuild ---------------------------------------------------------
# Mode pinned: the evaluation must see deployed no matter what the
# invoking shell exports (appliance-config.nix's hazard note).
nixpkgs="${MSKS_GUEST_NIXPKGS:?MSKS_GUEST_NIXPKGS is not set — the update needs the pinned nixpkgs the image was built with}"
echo "msks: nixos-rebuild boot --target-host root@$guest_ip (log: $app_dir/update.log)"
t0=$(date +%s)
if ! env MSKS_APPLIANCE_MODE=deployed PATH="$app_dir/update-bin:$PATH" \
  nixos-rebuild --no-flake boot \
  --target-host "root@$guest_ip" \
  -I "nixpkgs=$nixpkgs" \
  -I "nixos-config=$root/nix/appliance-config.nix" \
  -I "appliance-update-key=$app_dir/update-key.pub" \
  >"$app_dir/update.log" 2>&1; then
  echo "msks: nixos-rebuild failed — tail of $app_dir/update.log:" >&2
  tail -20 "$app_dir/update.log" >&2
  exit 1
fi
t1=$(date +%s)
echo "msks: rebuild and copy done in $((t1 - t0))s"

# --- cache the new generation's boot artifacts ---------------------------
# CH boots host-side files, so the generation the profile now points
# at must have its kernel/initrd/cmdline on the host for the next
# msks-appliance-up to boot it (the run script prefers
# boot-cache/current over the image's shipped artifacts).
profile_path="$(guest readlink -f /nix/var/nix/profiles/system)"
# The single quotes are the point: the guest shell expands the
# inner $( ) against the appliance profile.
# shellcheck disable=SC2016
gen_num="$(guest 'basename "$(readlink /nix/var/nix/profiles/system)" | sed "s/^system-\([0-9]*\)-link$/\1/"')"
case "$gen_num" in
'' | *[!0-9]*) die "profile target does not parse as system-N-link: $profile_path" ;;
esac
gen_dir="$app_dir/boot-cache/gen-$gen_num"
mkdir -p "$gen_dir"
# The era's erofs base rides with the generation (hard link — free
# while the base is unchanged): the overlay's LOWER layer and the
# volume's upper must come from the same era, and a later image
# rebuild replaces <state>/base-store.erofs with a lower the older
# generation's upper was never written against — those boots freeze
# at switch-root (found live, #220). The pin comes from the run
# script's booted-base record ONLY — what the RUNNING system booted
# with, the upper's era. Without the record the era is unknowable
# host-side, and pinning from <state>/base-store.erofs would freeze
# the update's generation whenever the image was rebuilt since the
# volume's era — refuse instead (a boot with the current scripts
# writes the record at serve time).
era_base="$app_dir/boot-cache/booted-base.erofs"
if [ -f "$era_base" ]; then
  ln -f "$era_base" "$gen_dir/base-store.erofs" 2>/dev/null ||
    cp -L "$era_base" "$gen_dir/base-store.erofs"
else
  die "no booted-base record at $era_base — boot the appliance once with the current scripts (msks-appliance-down && msks-appliance-up), then update: the record pins each generation's erofs to the era the volume's upper was written against"
fi
guest cat "$profile_path/kernel" >"$gen_dir/kernel"
guest cat "$profile_path/initrd" >"$gen_dir/initrd"
# kernel-params carries no init= (bootspec keeps the init path
# separate) and no trailing newline — compose the full line here.
{
  guest cat "$profile_path/kernel-params"
  printf ' init=%s/init\n' "$profile_path"
} >"$gen_dir/cmdline"
printf '%s\n' "$profile_path" >"$gen_dir/profile"

# current -> this generation; the previous current (if any) becomes
# the fallback the run script reaches for when this one fails to
# serve. Atomic: readers only ever see a complete gen dir.
prev=""
if [ -L "$app_dir/boot-cache/current" ]; then
  prev="$(basename "$(readlink "$app_dir/boot-cache/current")")"
fi
ln -sfn "gen-$gen_num" "$app_dir/boot-cache/current"
if [ -n "$prev" ] && [ "$prev" != "gen-$gen_num" ] && [ -d "$app_dir/boot-cache/$prev" ]; then
  ln -sfn "$prev" "$app_dir/boot-cache/previous"
fi

# Host-side trim mirroring the guest's keep-3 profile policy: the
# current and previous generations stay (the fallback's target);
# older gen dirs go. The erofs pins are hard links — deleting a dir
# releases one link; the shared inode lives while current, previous,
# or the booted-base record holds another (#221 review).
keep_current="$(basename "$(readlink "$app_dir/boot-cache/current" 2>/dev/null)" 2>/dev/null || true)"
keep_prev="$(basename "$(readlink "$app_dir/boot-cache/previous" 2>/dev/null)" 2>/dev/null || true)"
for d in "$app_dir"/boot-cache/gen-*; do
  [ -d "$d" ] || continue
  n="$(basename "$d")"
  case "$n" in
  "$keep_current" | "$keep_prev") ;;
  *) rm -rf "$d" ;;
  esac
done

echo "msks: generation $gen_num cached ($profile_path)"
echo "msks: activate with: msks-appliance-down && msks-appliance-up"
