#!/usr/bin/env bash
# Build the msksd appliance assets with nix and land them in the
# appliance state dir (#10) — .devenv/state/appliance by default;
# MSKS_APPLIANCE_DIR relocates it.
#
# Runs against the nixpkgs revision pinned by devenv.lock: the
# msks-appliance-build script and the appliance process pass the
# pinned source via MSKS_GUEST_NIXPKGS. Pure derivations all the way
# down — any Linux host with nix runs this unchanged; the host OS is
# irrelevant. Idempotent (#166): every invocation re-runs the
# (cached) nix-build and re-links the GC-root symlink, and copies the
# artifacts only when the image changed or one is missing — the
# appliance process runs this script before every boot.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"

# Which appliance build (#212): the Debian repack (still the default
# until parity flips it) or the NixOS system. The NixOS build's store
# shape rides MSKS_APPLIANCE_MODE (dev: the host store over virtiofs;
# deployed: the erofs base + store volume) and defaults to dev — the
# shape a checkout boots for development.
build="${MSKS_APPLIANCE_BUILD:-debian}"
case "$build" in
debian) ;;
nixos) ;;
*)
  echo "msks: MSKS_APPLIANCE_BUILD must be 'debian' or 'nixos' (got '$build')" >&2
  exit 1
  ;;
esac
app_dir="${MSKS_APPLIANCE_DIR:-$root/.devenv/state/appliance}"
# A relative MSKS_APPLIANCE_DIR resolves below the repo root,
# matching the Python-side resolution: a CWD-relative read would
# depend on where the shell was opened.
case "$app_dir" in
/*) ;;
*) app_dir="$root/$app_dir" ;;
esac

# The deployed update key (#220): root's authorized_keys on the
# appliance carries the public half, read from an
# `appliance-update-key` NIX_PATH entry at evaluation time. The key
# seeds ONCE per appliance dir and never rotates silently — a
# rotation would strand the private half the update path holds.
# Dev and Debian builds stay keyless: nothing to update over ssh
# (the store is the host's / a repacked image).
update_key_ies=""
if [ "$build" = nixos ] && [ "${MSKS_APPLIANCE_MODE:-dev}" = deployed ]; then
  mkdir -p "$app_dir"
  if [ ! -f "$app_dir/update-key" ]; then
    ssh-keygen -q -t ed25519 -N '' -C "msks-appliance-update" \
      -f "$app_dir/update-key.tmp" </dev/null
    mv -f "$app_dir/update-key.tmp" "$app_dir/update-key"
    mv -f "$app_dir/update-key.tmp.pub" "$app_dir/update-key.pub"
  fi
  [ -f "$app_dir/update-key.pub" ] || {
    echo "msks: $app_dir/update-key exists without its .pub — delete both and rebuild to reseed" >&2
    exit 1
  }
  update_key_ies="-I appliance-update-key=$app_dir/update-key.pub"
fi
# One build, two uses: the GC-root symlink IS the build — nix-build
# prints the out path, which doubles as the $out the artifact copies
# read from. A cached derivation makes this seconds; changed inputs
# rebuild. rm -f first so a missing or bogus symlink always heals
# (#160). The previous target feeds the status line at the end.
previous="$(readlink -f "$app_dir/image" 2>/dev/null || true)"
# No rm -f here: nix-build -o below atomically replaces the symlink
# (bogus or dangling targets included, verified — #160 healing is
# unaffected), and keeping the old GC root in place until the build
# succeeds avoids an unrooted window where a store GC could collect
# the closure a RUNNING appliance still resolves through.
echo "msks: ensuring appliance assets in $app_dir (idempotent — unchanged inputs are a cached no-op)"
if [ "$build" = nixos ]; then
  out="$(
    # The env prefix must CONTINUE into nix-build (the backslash): on
    # its own line the assignment is an unexported shell variable, the
    # evaluation falls to its plain-eval default ("deployed"), and a
    # dev checkout silently builds the deployed shape (found live,
    # #220).
    # $update_key_ies: zero-or-one pre-quoted -I flag, word-split by
    # design. The directive line must end at its codes.
    # shellcheck disable=SC2086
    MSKS_APPLIANCE_MODE="${MSKS_APPLIANCE_MODE:-dev}" \
      nix-build -I nixpkgs="$nixpkgs" $update_key_ies \
      "$root/nix/appliance-nixos.nix" -o "$app_dir/image"
  )"
else
  out="$(
    nix-build -I nixpkgs="$nixpkgs" \
      "$root/nix/appliance.nix" -A appliance -o "$app_dir/image"
  )"
fi

# The artifact set is mode-derived: the Debian build ships a rootfs
# disk; the NixOS build direct-boots kernel+initrd+cmdline, and the
# deployed shape adds the erofs base and the store-volume template.
image_mode="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("mode", "debian"))' "$app_dir/image/appliance-manifest.json" 2>/dev/null || echo debian)"
case "$build:$image_mode" in
debian:*) artifacts="vmlinux initrd rootfs.ext4 appliance-manifest.json" ;;
nixos:dev) artifacts="vmlinux initrd appliance-manifest.json" ;;
nixos:deployed) artifacts="vmlinux initrd base-store.erofs appliance-manifest.json" ;;
*)
  echo "msks: unknown appliance image mode in $app_dir/image/appliance-manifest.json" >&2
  exit 1
  ;;
esac

missing=""
for name in $artifacts; do
  [ -f "$app_dir/$name" ] || missing="$missing $name"
done
if [ "$previous" != "$out" ] || [ -n "$missing" ]; then
  # Copy only what changed, cmp-gated per artifact: the big ones
  # (vmlinux, initrd, base-store.erofs) are byte-stable across
  # config-only edits, and a no-change reboot must not rewrite them
  # (page cache, disk wear). The missing-artifact checks heal a
  # deleted or half-deleted state dir (#160); temp+mv is atomic: an
  # interrupted copy leaves the complete old file or no file — a
  # truncated rootfs would pass -f forever while the gate above
  # protects it.
  for name in $artifacts; do
    if [ ! -e "$out/$name" ]; then
      echo "msks: build output is missing $name" >&2
      exit 1
    fi
    if ! cmp -s "$out/$name" "$app_dir/$name"; then
      cp -L "$out/$name" "$app_dir/.$name.tmp"
      chmod 0644 "$app_dir/.$name.tmp"
      mv -f "$app_dir/.$name.tmp" "$app_dir/$name"
    fi
  done
fi
# The seed-once resources (the state template; the deployed store
# volume — generation storage a rebuild must never replace) live in
# the STORE, not the artifact output: a 40G sparse template inside
# $out costs the daemon a full byte-for-byte hash of its apparent
# size at every rebuild (sparse holes included). Their paths ride the
# manifest; each seeds its target once per install.
seed_once() {
  src_key="$1"
  dst="$2"
  src="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$app_dir/image/appliance-manifest.json" "$src_key")"
  if [ -n "$src" ] && [ ! -f "$dst" ]; then
    cp -L --sparse=always "$src" "$dst.tmp"
    chmod 0644 "$dst.tmp"
    mv -f "$dst.tmp" "$dst"
  fi
}
seed_once stateDisk "$app_dir/state.ext4"
seed_once storeVolume "${MSKS_APPLIANCE_STORE_VOLUME:-$app_dir/store-volume.img}"
if [ "$previous" = "$out" ]; then
  echo "msks: appliance assets up to date in $app_dir (image $out)"
else
  echo "msks: appliance assets built into $app_dir (from $out)"
fi
