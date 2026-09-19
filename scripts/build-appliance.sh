#!/usr/bin/env bash
# Build the msksd appliance assets with nix and land them in the
# appliance state dir (#10) — .devenv/state/appliance by default;
# MSKS_APPLIANCE_DIR relocates it.
#
# Runs against the nixpkgs revision pinned by devenv.lock: the
# msks-appliance-build script and the appliance process pass the
# pinned source via MSKS_GUEST_NIXPKGS. Pure derivations all the way
# down — any Linux host with nix runs this unchanged; the host OS is
# irrelevant. Idempotent (#166): every invocation re-lands every
# artifact and the GC-root symlink, and unchanged inputs make the
# nix-build a cached no-op — the appliance process runs this script
# before every boot.
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"
app_dir="${MSKS_APPLIANCE_DIR:-$root/.devenv/state/appliance}"
# A relative MSKS_APPLIANCE_DIR resolves below the repo root,
# matching the Python-side resolution: a CWD-relative read would
# depend on where the shell was opened.
case "$app_dir" in
/*) ;;
*) app_dir="$root/$app_dir" ;;
esac

mkdir -p "$app_dir"
# One build, two uses: the GC-root symlink IS the build — nix-build
# prints the out path, which doubles as the $out the artifact copies
# read from. A cached derivation makes this seconds; changed inputs
# rebuild. rm -f first so a missing or bogus symlink always heals
# (#160). The previous target feeds the status line at the end.
previous="$(readlink -f "$app_dir/image" 2>/dev/null || true)"
rm -f "$app_dir/image"
echo "msks: building appliance assets into $app_dir (idempotent — unchanged inputs are a cached no-op)"
out="$(
  nix-build -I nixpkgs="$nixpkgs" \
    "$root/nix/appliance.nix" -A appliance -o "$app_dir/image"
)"

for name in vmlinux initrd rootfs.ext4 appliance-manifest.json; do
  rm -f "$app_dir/$name"
  cp -L "$out/$name" "$app_dir/$name"
  chmod 0644 "$app_dir/$name"
done
# The state disk is NOT an artifact: a rebuild must never clobber live
# appliance state. Seed it once from the template; the
# appliance's msks-state-format.service (blank, foreign, and existing
# disks all converge) handle the rest.
if [ ! -f "$app_dir/state.ext4" ]; then
  cp -L "$out/state.ext4" "$app_dir/state.ext4"
  chmod 0644 "$app_dir/state.ext4"
fi
if [ "$previous" = "$out" ]; then
  echo "msks: appliance assets up to date in $app_dir (image $out)"
else
  echo "msks: appliance assets built into $app_dir (from $out)"
fi
