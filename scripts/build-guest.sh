#!/usr/bin/env bash
# Build the microvm guest assets with nix and land them in .guest/ (#5).
#
# Runs against the nixpkgs revision pinned by devenv.lock: the devenv
# task passes the pinned source via MSKS_GUEST_NIXPKGS. Pure
# derivations only — any Linux host with nix works, nothing outside
# the repo is fetched by hand.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"
guest_dir="$root/.guest"

out="$(
  nix-build --no-out-link -I nixpkgs="$nixpkgs" \
    "$root/nix/guest.nix" -A guest
)"

mkdir -p "$guest_dir"
for name in vmlinux initrd rootfs.ext4 guest-manifest.json; do
  rm -f "$guest_dir/$name"
  cp -L "$out/$name" "$guest_dir/$name"
  chmod 0644 "$guest_dir/$name"
done

echo "msks: guest assets built into .guest/ (from $out)"
echo "msks: boot one with: devenv tasks run msks:demo-vm"
