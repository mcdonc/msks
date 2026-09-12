#!/usr/bin/env bash
# Build the k8s vm-runner container image archive into .guest/ (#5).
#
# The archive is a plain docker-archive tar produced by nix — no
# container daemon or registry needed on the build host. Import it on
# the k3s node to make the runner available to the k8s smoke tests:
#
#   sudo k3s ctr images import .guest/msks-vm-runner.docker.tar
set -euo pipefail

root="${DEVENV_ROOT:?not running inside the devenv shell}"
nixpkgs="${MSKS_GUEST_NIXPKGS:?devenv must pass MSKS_GUEST_NIXPKGS}"
guest_dir="$root/.guest"
image="msks-vm-runner:dev"

out="$(
  nix-build --no-out-link -I nixpkgs="$nixpkgs" \
    "$root/nix/guest.nix" -A runner-image
)"

# buildImage outputs the image tarball (possibly gzipped) inside its
# output directory.
archive="$(find "$out" -maxdepth 1 \( -name '*.tar' -o -name '*.tar.gz' \) -print -quit)"
if [ -z "$archive" ]; then
  echo "msks: no image archive found in $out" >&2
  exit 1
fi

mkdir -p "$guest_dir"
rm -f "$guest_dir/msks-vm-runner.docker.tar"
cp "$archive" "$guest_dir/msks-vm-runner.docker.tar"
chmod 0644 "$guest_dir/msks-vm-runner.docker.tar"
printf '{"image": "%s", "archive": "msks-vm-runner.docker.tar"}\n' "$image" \
  > "$guest_dir/runner-image.json"

echo "msks: runner image archive built into .guest/ (from $out)"
echo "msks: import on the k3s node with:"
echo "  sudo k3s ctr images import $guest_dir/msks-vm-runner.docker.tar"
echo "msks: the image is tagged $image"
