#!/usr/bin/env bash
# Build the k8s vm-runner container image archive into .guest/ (#5).
#
# The archive is a plain docker-archive tar produced by nix; the
# build host needs neither a container daemon nor a registry. Import
# it on the k3s node to make the runner available to the k8s smoke
# tests:
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

# buildImage outputs the image archive (possibly gzipped) inside its
# output directory; keep the extension honest in .guest/.
archive="$(find "$out" -maxdepth 1 \( -name '*.tar' -o -name '*.tar.gz' \) -print -quit)"
if [ -z "$archive" ]; then
  echo "msks: no image archive found in $out" >&2
  exit 1
fi
case "$archive" in
  *.tar.gz) archive_name=msks-vm-runner.docker.tar.gz ;;
  *) archive_name=msks-vm-runner.docker.tar ;;
esac

mkdir -p "$guest_dir"
rm -f "$guest_dir/msks-vm-runner.docker.tar" "$guest_dir/msks-vm-runner.docker.tar.gz"
cp "$archive" "$guest_dir/$archive_name"
chmod 0644 "$guest_dir/$archive_name"
printf '{"image": "%s", "archive": "%s"}\n' "$image" "$archive_name" \
  > "$guest_dir/runner-image.json"

echo "msks: runner image archive built into .guest/ (from $out)"
echo "msks: import on the k3s node with:"
echo "  sudo k3s ctr images import $guest_dir/$archive_name"
echo "msks: the image is tagged $image"
