#!/usr/bin/env bash
# Bootstrap the two-hop rig inside the outer Lima VM (msks-hop2):
#   hop 1: macOS -> outer VM (VZ virtiofs, ~/hop2 mounted rw)
#   hop 2: outer VM -> nested cloud-hypervisor guest via virtiofsd
#
# Idempotent: safe to re-run. Everything lands under /var/lib/hop2.
# Requires: the outer VM is up, its /Users/chrism/hop2 mount exists.
set -euo pipefail

RIG=/var/lib/hop2
CH_VERSION=v54.0
IMAGE_SHA=d8470b8c6c38fead046c794b5800a5a7b96672d5bcf543cc230ceb0c4b8ace05ed341a0c8928045422243fde26b2f1f2f58e99244c65709ffda2e3d4b674dd5a
IMAGE_URL=https://cloud.debian.org/images/cloud/trixie/20261001-2618/debian-13-genericcloud-arm64-20261001-2618.qcow2

[ "$(id -u)" = 0 ] || {
  echo "run as root"
  exit 1
}

mkdir -p "$RIG"/{share,run,logs}
mountpoint -q /Users/chrism/hop2 || {
  echo "/Users/chrism/hop2 not mounted (hop-1 share)"
  exit 1
}

echo "== packages"
apt-get install -y -qq virtiofsd qemu-utils gcc >/dev/null

echo "== cloud-hypervisor $CH_VERSION"
if ! /usr/local/bin/cloud-hypervisor --version 2>/dev/null | grep -q "$CH_VERSION"; then
  curl -sL -o /usr/local/bin/cloud-hypervisor \
    "https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/$CH_VERSION/cloud-hypervisor-static-aarch64"
  chmod +x /usr/local/bin/cloud-hypervisor
fi
/usr/local/bin/cloud-hypervisor --version

echo "== guest base image"
# Same Debian trixie genericcloud image the outer VM boots (and the same
# digest the Lima template pins); kept outside the share so guest-disk
# churn never adds noise to the filesystem under test.
if [ ! -f "$RIG/base.qcow2" ]; then
  curl -sL -o "$RIG/base.qcow2.tmp" "$IMAGE_URL"
  echo "$IMAGE_SHA  $RIG/base.qcow2.tmp" | sha512sum -c -
  mv "$RIG/base.qcow2.tmp" "$RIG/base.qcow2"
fi
if [ ! -f "$RIG/guest.qcow2" ] || [ "${1:-}" = "--fresh" ]; then
  rm -f "$RIG/guest.qcow2"
  qemu-img create -f qcow2 -b "$RIG/base.qcow2" -F qcow2 "$RIG/guest.seed" 8G >/dev/null
  bash "$(dirname "$0")/seed-guest.sh" "$RIG/guest.seed"
  # cloud-hypervisor caps qcow2 backing depth at one level: flatten
  # the overlay into a standalone image.
  rm -f "$RIG/guest.qcow2"
  qemu-img convert -O qcow2 "$RIG/guest.seed" "$RIG/guest.qcow2"
  rm -f "$RIG/guest.seed"
fi

echo "== rig ready: $RIG"
