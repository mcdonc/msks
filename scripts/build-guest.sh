#!/usr/bin/env bash
# Build the microvm guest assets with nix and land them in the guest
# state dir (#5) — .devenv/state/guest by default; MSKS_GUEST_DIR
# relocates it.
#
# Runs against the nixpkgs revision pinned by devenv.lock: the devenv
# task passes the pinned source via MSKS_GUEST_NIXPKGS. The build is
# pure derivations all the way down, so any Linux host with nix runs
# it unchanged.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"
guest_dir="${MSKS_GUEST_DIR:-$root/.devenv/state/guest}"

out="$(
  nix-build --no-out-link -I nixpkgs="$nixpkgs" \
    "$root/nix/guest.nix" -A guest
)"

# A GC root: the appliance references these paths only through the
# virtiofs store share (nothing in its closure depends on them), so
# without a root a garbage collect would ENOENT every workspace boot.
rm -f "$guest_dir/guest-root"
nix-build -I nixpkgs="$nixpkgs" "$root/nix/guest.nix" -A guest \
  -o "$guest_dir/guest-root"

mkdir -p "$guest_dir"
for name in vmlinux initrd rootfs.ext4 guest-manifest.json; do
  rm -f "$guest_dir/$name"
  cp -L "$out/$name" "$guest_dir/$name"
  chmod 0644 "$guest_dir/$name"
done
# The canonical containerDisk archive (#40); the build emits its
# exact filename so name/version changes cannot drift.
tar_name=$(cat "$out/image-archive-name")
rm -f "$guest_dir"/workspace-*.tar
cp -L "$out/$tar_name" "$guest_dir/$tar_name"
chmod 0644 "$guest_dir/$tar_name"

echo "msks: guest assets built into $guest_dir (from $out)"
echo "msks: boot one with: devenv tasks run msks:demo-vm"
