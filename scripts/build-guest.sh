#!/usr/bin/env bash
# Build the microvm guest assets with nix and land them in the
# guest state dir (#5) — .devenv/state/guest by default; GUEST_DIR
# relocates it. `build-guest.sh nixos` (#250) builds the NixOS guest
# instead — .devenv/state/guest-nixos, relocated by GUEST_NIXOS_DIR
# — with the same output surface. The arch-suffixed flavors (#637)
# pin the guest's architecture: `debian-aarch64` and
# `nixos-aarch64` build the arm64 guests into their own dirs
# (GUEST_AARCH64_DIR / GUEST_NIXOS_AARCH64_DIR relocate); the bare
# flavor names follow the building host's uname, so an aarch64
# dev host's `debian` IS the arm64 guest.
#
# Runs against the nixpkgs revision pinned by devenv.lock: the
# msks-build-guest script passes the pinned source via
# GUEST_NIXPKGS. The build is
# pure derivations all the way down, so any Linux host with nix runs
# it unchanged.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
nixpkgs="${GUEST_NIXPKGS:?devenv must pass GUEST_NIXPKGS}"

flavor="${1:-debian}"
case "$flavor" in
debian)
  attr=guest
  guest_dir="${GUEST_DIR:-$root/.devenv/state/guest}"
  ;;
debian-x86_64)
  attr=guest-x86_64
  guest_dir="${GUEST_X86_64_DIR:-$root/.devenv/state/guest-x86_64}"
  ;;
debian-aarch64)
  attr=guest-aarch64
  guest_dir="${GUEST_AARCH64_DIR:-$root/.devenv/state/guest-aarch64}"
  ;;
nixos)
  attr=guest-nixos
  guest_dir="${GUEST_NIXOS_DIR:-$root/.devenv/state/guest-nixos}"
  ;;
nixos-x86_64)
  attr=guest-nixos-x86_64
  guest_dir="${GUEST_NIXOS_X86_64_DIR:-$root/.devenv/state/guest-nixos-x86_64}"
  ;;
nixos-aarch64)
  attr=guest-nixos-aarch64
  guest_dir="${GUEST_NIXOS_AARCH64_DIR:-$root/.devenv/state/guest-nixos-aarch64}"
  ;;
*)
  echo "usage: $0 [debian|debian-x86_64|debian-aarch64|nixos|nixos-x86_64|nixos-aarch64]" >&2
  exit 2
  ;;
esac

# A relative guest dir resolves below the repo root, matching
# the Python-side resolution (guestassets.guest_dir): a CWD-relative
# read would depend on where the shell was opened.
case "$guest_dir" in
/*) ;;
*) guest_dir="$root/$guest_dir" ;;
esac

out="$(
  nix-build --no-out-link -I nixpkgs="$nixpkgs" \
    "$root/nix/guest.nix" -A "$attr"
)"

# No GC root: nothing reads these store paths live. The copy step
# below is the consumer surface (demo-vm, the smoke tests, the
# seed flows all boot from the guest_dir copies, which a garbage
# collect never touches), and the daemon imports image archives
# into its own catalog.
echo "msks: building $flavor guest assets into $guest_dir (idempotent — unchanged inputs are a cached no-op)"

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
# The name marker lands beside the archive it names: the release
# workflow's channel step and scripts/image_meta.py read the pair
# from the guest dir (the marker lived only in the store output
# before, and every dir consumer had to glob).
printf '%s\n' "$tar_name" >"$guest_dir/image-archive-name"
chmod 0644 "$guest_dir/image-archive-name"

echo "msks: $flavor guest assets built into $guest_dir (from $out)"
if [ "$flavor" = debian ]; then
  echo "msks: boot one with: msks-demo-vm"
fi
