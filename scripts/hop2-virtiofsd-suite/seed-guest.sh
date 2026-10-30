#!/usr/bin/env bash
# Seed the nested guest disk (guest.qcow2) with the suite driver:
# attach via qemu-nbd, mount the root partition, drop in a systemd
# unit that mounts the virtiofs share at boot and runs the driver
# script, then detach. Also prints the guest's root= device for the
# kernel cmdline.
set -euo pipefail

RIG=/var/lib/hop2
NBD=/dev/nbd0
DISK=${1:-$RIG/guest.qcow2}
MNT=$(mktemp -d)

[ "$(id -u)" = 0 ] || {
  echo "run as root"
  exit 1
}

modprobe nbd
if [ ! -b "$NBD" ]; then
  echo "$NBD did not appear (nbd module failed)"
  exit 1
fi
qemu-nbd -d "$NBD" >/dev/null 2>&1 || true
qemu-nbd -c "$NBD" "$DISK"
trap 'umount "$MNT" 2>/dev/null || true; qemu-nbd -d "$NBD" >/dev/null; rm -rf "$MNT"' EXIT

# Find the ext4 root partition on the disk (partition scan can lag
# a moment after attach).
ROOT_PART=""
for _ in $(seq 1 10); do
  ROOT_PART=$(lsblk -rno NAME,FSTYPE "$NBD" | awk '$2=="ext4"{print "/dev/"$1; exit}')
  [ -n "$ROOT_PART" ] && break
  sleep 1
done
[ -n "$ROOT_PART" ] || {
  lsblk -f "$NBD"
  echo "no ext4 partition found"
  exit 1
}
mount "$ROOT_PART" "$MNT"

# Read the root device the image itself uses (keep the fstab's prefix
# semantics: PARTUUID=/UUID=/dev path all valid on the kernel cmdline).
ROOT_SPEC=$(awk '$2=="/" && $3=="ext4"{print $1}' "$MNT/etc/fstab")
[ -n "$ROOT_SPEC" ] || {
  echo "no root entry in guest fstab"
  exit 1
}

# Guest-side driver: mounts the share, runs one command per phase,
# echoing PHASE markers to the serial console for the outer driver.
cat >"$MNT/root/guest-driver.sh" <<'EOF'
#!/bin/bash
# Runs INSIDE the nested guest. Waits for the virtiofs device, mounts
# the share, then signals readiness and stays up (the outer driver
# sends commands by writing /mnt/share/cmd and reading /mnt/share/out).
set -u
SHARE=/mnt/share
dmesg -n 4
echo "GUEST-DRIVER: waiting for virtiofs device"
for i in $(seq 1 30); do
  grep -q virtiofs /proc/filesystems && mountpoint -q $SHARE && break
  mkdir -p $SHARE
  mount -t virtiofs hostshare $SHARE 2>/dev/null || true
  sleep 1
done
if ! mountpoint -q $SHARE; then
  echo "GUEST-DRIVER: MOUNT-FAILED"
  grep -i virtio /proc/bus/pci/devices 2>/dev/null
  ls /sys/fs/virtiofs 2>/dev/null
  exit 1
fi
echo "GUEST-DRIVER: MOUNT-OK $(grep hostshare /proc/mounts)"
echo "GUEST-DRIVER: kernel $(uname -r)"
rm -f $SHARE/cmd $SHARE/out $SHARE/ready $SHARE/driver.log
log() { echo "[$(date +%T)] $*" >>/root/driver.local.log; echo "[$(date +%T)] $*" >>$SHARE/driver.log 2>/dev/null; }
echo ok > $SHARE/ready
log driver-started pid=$$
# Command loop. Protocol: cmd holds "<seq> <command>"; the answer goes
# to out.<seq>, and end.<seq> (written LAST, after out is complete) is
# the completion marker the outer side polls - a marker that exists
# only after its data does, so an early poll can never see a partial
# answer. timeout -k escalates to SIGKILL so a child stuck on a
# stalled mount cannot wedge the loop forever.
while :; do
  if [ -f $SHARE/cmd ] && [ -s $SHARE/cmd ]; then
    line=$(cat $SHARE/cmd 2>/dev/null || true)
    rm -f $SHARE/cmd
    seq=${line%% *}; cmd=${line#* }
    if [ "$cmd" = QUIT ]; then
      echo ok > $SHARE/end.$seq; break
    fi
    echo "GUEST-CMD: $line"
    log "cmd: $line"
    timeout -k 5 600 bash -c "$cmd" >$SHARE/out.$seq 2>&1
    rc=$?
    echo $rc >$SHARE/rc.$seq
    echo ok > $SHARE/end.$seq
    echo "GUEST-DONE rc=$rc"
    log "done seq=$seq rc=$rc"
  fi
  sleep 0.2
done
echo "GUEST-DRIVER: exiting"
EOF
chmod +x "$MNT/root/guest-driver.sh"

# The driver heredoc is a quoted string, invisible to bash -n of this
# script; parse it on its own so a broken driver cannot seed silently.
# The extraction patterns quote $MNT deliberately (they match the
# literal heredoc text in this file).
# shellcheck disable=SC2016
sed -n '/^cat >"\$MNT\/root\/guest-driver.sh" <<'"'EOF'"'$/,/^EOF$/p' "$0" |
  sed '1d;$d' >/tmp/guest-driver-check.sh
bash -n /tmp/guest-driver-check.sh ||
  {
    echo "seeded driver has a syntax error"
    exit 1
  }
# shellcheck disable=SC2016
grep -q 'end\.\$seq' /tmp/guest-driver-check.sh ||
  {
    echo "seeded driver is not the seq/end protocol"
    exit 1
  }

# systemd unit to run it on the guest console at boot.
mkdir -p "$MNT/etc/systemd/system"
cat >"$MNT/etc/systemd/system/hop2-driver.service" <<'EOF'
[Unit]
Description=hop2 suite guest driver
After=local-fs.target
[Service]
Type=simple
StandardOutput=journal+console
StandardError=journal+console
ExecStart=/root/guest-driver.sh
[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/hop2-driver.service "$MNT/etc/systemd/system/multi-user.target.wants/hop2-driver.service"

# First boot of a pristine cloud image stalls on cloud-init without a
# datasource; the rig boots direct-kernel and runs the driver instead.
mkdir -p "$MNT/etc/cloud"
touch "$MNT/etc/cloud/cloud-init.disabled"
ln -sf /dev/null "$MNT/etc/systemd/system/ssh.service"
ln -sf /dev/null "$MNT/etc/systemd/system/ssh.socket"

sync
umount "$MNT"
echo "ROOT_SPEC=$ROOT_SPEC"
echo "$ROOT_SPEC" >"$RIG/root-spec"
