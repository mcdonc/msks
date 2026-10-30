#!/usr/bin/env bash
# Boot the two-hop rig: virtiofsd exporting the hop-1 mount, then the
# nested cloud-hypervisor guest with the share attached. Waits for the
# guest driver to mount the share and write $SHARE/ready.
#
#   up.sh [--fresh]   --fresh rebuilds and reseeds the guest overlay
set -euo pipefail

RIG=/var/lib/hop2
SHARE=/Users/chrism/hop2/share
SOCK=$RIG/run/vfsd.sock
CONSOLE=$RIG/logs/console.log
KERNEL=/boot/vmlinuz-$(uname -r)
INITRD=/boot/initrd.img-$(uname -r)
TAG=hostshare
MEM=1536M
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || {
  echo "run as root"
  exit 1
}

bash "$HERE/down.sh" 2>/dev/null || true
mkdir -p "$SHARE" "$RIG"/{run,logs}
# A ready file from a previous guest would satisfy the wait below
# instantly; the fresh guest must be the one to write it.
rm -f "$SHARE"/{ready,cmd,out}

if [ "${1:-}" = "--fresh" ] || [ ! -f "$RIG/root-spec" ]; then
  bash "$HERE/bootstrap.sh" --fresh >/dev/null
  rm -f "$SHARE"/{cmd,out,ready} 2>/dev/null || true
fi
ROOT_SPEC=$(cat "$RIG/root-spec")

echo "== virtiofsd"
/usr/libexec/virtiofsd \
  --shared-dir "$SHARE" \
  --socket-path "$SOCK" \
  >"$RIG/logs/virtiofsd.log" 2>&1 &
echo $! >"$RIG/run/virtiofsd.pid"

echo "== cloud-hypervisor"
# console=ttyAMA0 (PL011); virtio-console hvc0 is also present.
CMDLINE="root=$ROOT_SPEC rw console=ttyAMA0"
: >"$CONSOLE"
/usr/local/bin/cloud-hypervisor \
  --kernel "$KERNEL" \
  --initramfs "$INITRD" \
  --cmdline "$CMDLINE" \
  --disk path="$RIG/guest.qcow2",image_type=qcow2 \
  --fs tag="$TAG",socket="$SOCK" \
  --cpus boot=2 \
  --memory size="$MEM",shared=on \
  --serial file="$CONSOLE" \
  >"$RIG/logs/ch.log" 2>&1 &
echo $! >"$RIG/run/ch.pid"

echo "== waiting for guest to mount the share"
ok=""
for _ in $(seq 1 90); do
  if [ -f "$SHARE/ready" ]; then
    ok=1
    break
  fi
  if ! kill -0 "$(cat "$RIG/run/ch.pid")" 2>/dev/null; then
    echo "cloud-hypervisor died early:"
    tail -20 "$RIG/logs/ch.log"
    exit 1
  fi
  sleep 1
done
if [ -z "$ok" ]; then
  echo "guest never became ready; console tail:"
  tail -30 "$CONSOLE"
  tail -10 "$RIG/logs/virtiofsd.log" || true
  exit 1
fi
echo "RIG-UP: guest mounted $TAG (console: $CONSOLE)"
