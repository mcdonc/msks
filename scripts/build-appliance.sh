#!/usr/bin/env bash
# Build the msksd appliance assets with nix and land them in .appliance/ (#10).
#
# Runs against the nixpkgs revision pinned by devenv.lock: the devenv
# task passes the pinned source via MSKS_GUEST_NIXPKGS. Pure
# derivations all the way down — any Linux host with nix runs this
# unchanged; the host OS is irrelevant.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"
app_dir="$root/.appliance"

out="$(
  nix-build --no-out-link -I nixpkgs="$nixpkgs" \
    "$root/nix/appliance.nix" -A appliance
)"

mkdir -p "$app_dir"
for name in vmlinux initrd rootfs.ext4 appliance-manifest.json; do
  rm -f "$app_dir/$name"
  cp -L "$out/$name" "$app_dir/$name"
  chmod 0644 "$app_dir/$name"
done
# The state disk is NOT an artifact: a rebuild must never clobber live
# appliance state. Seed it once from the template; the up-task and the
# appliance's msks-state-format.service (blank, foreign, and existing
# disks all converge) handle the rest.
if [ ! -f "$app_dir/state.ext4" ]; then
  cp -L "$out/state.ext4" "$app_dir/state.ext4"
  chmod 0644 "$app_dir/state.ext4"
fi
# A GC root keeping the closure (msksd, VMM, modules) realized on this
# host — the appliance resolves them through the virtiofs store share.
rm -f "$app_dir/image"
nix-build --no-out-link -I nixpkgs="$nixpkgs" \
  "$root/nix/appliance.nix" -A appliance -o "$app_dir/image"

echo "msks: appliance assets built into .appliance/ (from $out)"
echo "msks: boot it with: devenv processes up -d"
