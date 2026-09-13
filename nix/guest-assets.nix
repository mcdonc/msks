# Guest VM assets for direct kernel boot on cloud-hypervisor (#5).
#
# Everything a microvm needs comes out of this file as plain store
# paths, built from the pinned nixpkgs by pure derivations: the build
# runs on any Linux host with nix and touches nothing outside the
# repo.
#
# The root filesystem is Debian 13 (trixie), straight from Debian's
# official nocloud cloud image (#30): real Debian with systemd as
# PID 1, apt, Debian's own kernel/initrd/modules — and Debian's own
# socat (built WITH_VSOCK) serving the vsock console. No msks-built
# binary runs inside the guest; the only additions are systemd
# drop-in units. The image is pinned by its dated cloud.debian.org
# URL and sha512 ("latest" is a moving pointer; every dated build
# stays published).
#
#   $out/vmlinux            - Debian's kernel (bzImage, PVH entry
#                             point; CONFIG_PVH=y). Named "vmlinux"
#                             to match the MSKSD_TEST_VMLINUX
#                             contract; guest-manifest.json records
#                             the actual format.
#   $out/initrd             - Debian's initramfs for that kernel.
#   $out/rootfs.ext4        - the extracted Debian tree as a fresh
#                             read-only-boot ext4 image.
#   $out/guest-manifest.json - artifact names + the boot cmdline.
#
# Known extraction limitation: unprivileged debugfs rdump cannot
# restore setuid bits (su, mount show -rwxr-xr-x). Everything in the
# VM runs as root today, so nothing regresses; restoring them is
# #36-adjacent follow-up material.
#
# Evaluate through the devenv tasks (they pin nixpkgs to the
# devenv.lock revision); `nix-build nix/guest.nix -A guest` with plain
# NIX_PATH also works when the pinned channel is acceptable.
{
  lib,
  pkgs,
}:

let
  # The official Debian 13 nocloud image: systemd, no cloud-init.
  debianImage = pkgs.fetchurl {
    urls = [
      "https://cloud.debian.org/images/cloud/trixie/20260831-2587/debian-13-nocloud-amd64-20260831-2587.qcow2"
    ];
    hash = "sha512:e4f716b1fb48be24085c0907bd1a0a31f03b7bf2adfbd46d9f39595a225dc38741a4b5d79910e61fa1d885ac043e5ea0663fe805f26944e1dc1211a3206022c2";
  };

  # The port the guest's vsock console listens on; the daemon dials
  # it after the CONNECT handshake (#21). Fixed, recorded in the
  # manifest, matched by the systemd unit below.
  vsockShellPort = 1023;

  kernelCmdline = "console=ttyS0 root=/dev/vda rootfstype=ext4 ro";

  # The msks additions, staged as an overlay tree: the vsock console
  # service, serial-console autologin (the debug console), the vsock
  # module load, and a stable hostname. Debian's socat 1.8.x is
  # built WITH_VSOCK, so nothing is cross-compiled in.
  guestOverlay = pkgs.runCommand "msks-guest-overlay" { } ''
    set -eu
    mkdir -p \
      $out/etc/systemd/system/serial-getty@ttyS0.service.d \
      $out/etc/systemd/system/multi-user.target.wants \
      $out/etc/modules-load.d

    printf 'msks-guest\n' > $out/etc/hostname

    # The image's fstab mounts the root filesystem by the PARTUUID of
    # the cloud image's partition table; direct kernel boot presents
    # a bare ext4, so those device jobs can never start and systemd
    # stalls at boot. The kernel cmdline already names the root
    # device; systemd mounts the pseudo-filesystems itself.
    printf '%s\n' \
      '# msks: root comes from the kernel cmdline; no swap.' \
      '# /var is volatile: the root disk is read-only (#30).' \
      'tmpfs /var tmpfs mode=0755,nosuid,nodev 0 0' \
      > $out/etc/fstab

    printf '%s\n' \
      '# The vsock console transport: the module name on Debian' \
      '# is vmw_vsock_virtio_transport (#21).' \
      'vmw_vsock_virtio_transport' \
      > $out/etc/modules-load.d/msks-vsock.conf

    printf '%s\n' \
      '[Unit]' \
      'Description=msks vsock console (one shell per connection)' \
      'Documentation=https://github.com/mcdonc/msks' \
      'ConditionPathExists=/dev/vsock' \
      'After=systemd-modules-load.service' \
      ''' \
      '[Service]' \
      'ExecStart=/usr/bin/socat VSOCK-LISTEN:${toString vsockShellPort},reuseaddr,fork EXEC:/bin/bash,pty,ctty,echo=0,icanon=0,stderr,setsid' \
      'Restart=always' \
      'RestartSec=1' \
      'StandardInput=null' \
      ''' \
      '[Install]' \
      'WantedBy=multi-user.target' \
      > $out/etc/systemd/system/msks-console.service
    ln -s ../msks-console.service \
      $out/etc/systemd/system/multi-user.target.wants/msks-console.service

    # grub-common records successful boots into /boot — read-only
    # here, and a direct-boot VM has no grub to inform anyway.
    ln -s /dev/null $out/etc/systemd/system/grub-common.service

    # The serial console is the guest's debug channel: autologin root
    # on ttyS0 (the vsock console is the supported interactive path).
    printf '%s\n' \
      '[Service]' \
      'ExecStart=' \
      'ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM' \
      > $out/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
  '';

  # Parse sfdisk --json: print the byte offset of the Linux root
  # partition. A separate file (not inline) because nix ''-string
  # de-indentation would mangle a python block.
  partitionOffset = pkgs.writeText "msks-root-partition-offset.py" ''
    import json
    import sys

    table = json.load(sys.stdin)["partitiontable"]
    root = None
    for part in table["partitions"]:
        if part["type"] == "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709":
            root = part
            break
    assert root is not None, "no Linux root partition found"
    print(root["start"] * table.get("sectorsize", 512))
  '';

  # The Debian root tree: convert the qcow2 to raw, slice the root
  # partition out (offset from the partition table, not hardcoded),
  # dump the ext4 contents with debugfs (unprivileged — no mount),
  # and lay the overlay on top. A fresh ext4 is built from the tree
  # later, so this stays a plain directory.
  debianRoot = pkgs.runCommand "msks-debian-root"
    {
      nativeBuildInputs = [
        pkgs.qemu
        pkgs.e2fsprogs
        pkgs.util-linux
        (pkgs.python3.withPackages (ps: [ ]))
      ];
    }
    ''
      set -eu
      root="$out/root"
      mkdir -p "$root"

      # qcow2 -> raw
      qemu-img convert -O raw ${debianImage} debian.raw

      # Slice the root partition: the GPT partition labeled/type
      # "Linux filesystem" (nocloud keeps EFI + BIOS grub partitions
      # around it, which direct kernel boot does not need).
      offset=$(sfdisk --json debian.raw | python3 ${partitionOffset})
      dd if=debian.raw of=root.part bs=512 skip=$((offset / 512)) status=none

      # ext4 -> tree (ownership errors are expected unprivileged: the
      # files land owned by the build user; see the header note about
      # setuid).
      debugfs -R "rdump / $root" root.part 2>/dev/null || true
      rm -rf "$root"/lost+found
      # rdump's stderr mixes benign ownership noise with real errors,
      # so the exit code is useless; assert the dump itself landed.
      for top in bin usr etc var lib boot; do
        test -d "$root/$top"
      done

      # The msks overlay.
      cp -a --no-preserve=ownership ${guestOverlay}/. "$root"/

      # Sanity: this must be a bootable Debian.
      test -x "$root"/sbin/init
      test -x "$root"/usr/bin/socat
      test -d "$root"/lib/modules

      # Size the final image from the tree (content-derived, no
      # magic constant): Debian unpacks to ~600M plus headroom.
      du -s --apparent-size --block-size=4096 "$root" | cut -f1 > "$out"/tree-blocks
    '';

  # mke2fs -d packs a directory into an ext4 image without mounting
  # anything — the whole build stays unprivileged and host-independent.
  # One fakeroot session owns the tree and builds the image: the
  # extraction tree is owned by the build user (mke2fs -d bakes the
  # builder's ownership view straight into the image — every inode
  # would be nobody:nogroup). Under fakeroot the chown/chmod are
  # recorded, not performed, and mke2fs -d's stat() reads the faked
  # root ownership. This also restores sane permissions on the
  # password files; setuid bits stay lost (rdump cannot preserve
  # them, and everything runs as root today).
  packScript = pkgs.writeText "msks-rootfs-pack.sh" ''
    set -eu
    tree="''${PACK_TREE:?}"
    img="''${PACK_IMG:?}"
    blocks="''${PACK_BLOCKS:?}"
    fake_epoch="''${PACK_FAKE_EPOCH:?}"
    chown -R 0:0 "$tree"
    chmod 0640 "$tree"/etc/shadow "$tree"/etc/gshadow
    chmod 0600 "$tree"/etc/ssh/ssh_host_*_key 2>/dev/null || true
    E2FSPROGS_FAKE_TIME="$fake_epoch" mke2fs -q -t ext4 -b 4096 -I 256 \
      -L msks-rootfs \
      -E hash_seed=00000000-0000-0000-0000-000000000000 \
      -d "$tree" "$img" "$blocks"
    E2FSPROGS_FAKE_TIME="$fake_epoch" tune2fs -U clear "$img" >/dev/null
  '';

  rootfs = pkgs.runCommand "msks-guest-rootfs" {
    inherit debianRoot packScript;
    nativeBuildInputs = [ pkgs.e2fsprogs pkgs.fakeroot ];
    fakeEpoch = 1262304000;
  } ''
    set -eu
    mkdir -p "$out"
    # Content plus 1G of slack (the root boots read-only; the slack
    # only cushions future overlay content, not guest writes).
    PACK_TREE="$debianRoot/root" \
      PACK_IMG="$out/rootfs.ext4" \
      PACK_BLOCKS=$(( $(cat "$debianRoot"/tree-blocks) + 262144 )) \
      PACK_FAKE_EPOCH="$fakeEpoch" \
      fakeroot -- /bin/sh -e "$packScript"
  '';

in
pkgs.runCommand "msks-guest"
  {
    inherit debianRoot rootfs;
    passthru = {
      inherit
        kernelCmdline
        vsockShellPort
        ;
    };
  }
  ''
    set -eu
    mkdir -p "$out"
    # Debian ships exactly one kernel per image; take it by glob and
    # record its version for the manifest consumers that want it.
    vmlinuz=$(ls "$debianRoot"/root/boot/vmlinuz-*)
    initrd=$(ls "$debianRoot"/root/boot/initrd.img-*)
    version=$(basename "$vmlinuz" | sed 's/^vmlinuz-//')
    cp "$vmlinuz" "$out/vmlinux"
    cp "$initrd" "$out/initrd"
    cp "${rootfs}/rootfs.ext4" "$out/rootfs.ext4"
    printf '%s' "$version" > "$out"/kernel-version
    # An unquoted heredoc: $version expands in the shell; the
    # cmdline and port were interpolated by nix at eval time.
    cat > "$out"/guest-manifest.json <<EOF
    {
      "schema": 1,
      "kernel_version": "$version",
      "kernel_format": "bzImage",
      "cmdline": "${kernelCmdline}",
      "vmlinux": "vmlinux",
      "initrd": "initrd",
      "rootfs": "rootfs.ext4",
      "vsock_shell_port": ${toString vsockShellPort}
    }
    EOF
  ''
