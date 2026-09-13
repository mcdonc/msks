# Guest VM assets for direct kernel boot on cloud-hypervisor (#5).
#
# Everything a microvm needs comes out of this file as plain store
# paths, built from the pinned nixpkgs by pure derivations: the build
# runs on any Linux host with nix and touches nothing outside the
# repo.
#
# The root filesystem is Debian 13 (trixie), straight from Debian's
# official nocloud cloud image (#30): real Debian with systemd as
# PID 1, apt, Debian's own modules — and Debian's own socat (built
# WITH_VSOCK) serving the vsock console. The kernel is Debian's
# *cloud* flavor of the same upstream version (#37): ext4 and
# virtio-pci built in, so the initramfs msks builds carries a single
# module (virtio_blk) and boots in tens of milliseconds where the
# generic initrd cost ~2.5s. The image is pinned by its dated
# cloud.debian.org URL and sha512; the cloud kernel by its
# deb.debian.org pool URL and sha256 ("latest" is a moving pointer;
# dated builds stay published).
#
#   $out/vmlinux            - Debian's cloud kernel (bzImage, PVH
#                             entry point; CONFIG_PVH=y). Named
#                             "vmlinux" to match the
#                             MSKSD_TEST_VMLINUX contract;
#                             guest-manifest.json records the actual
#                             format.
#   $out/initrd             - msks-built minimal initramfs: busybox,
#                             virtio_blk.ko, mount root, switch_root.
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

  # The workspace image identity (#40): the catalog reference is
  # <name>:<version>.
  imageName = "debian";
  imageVersion = "13.6";

  kernelCmdline = "console=ttyS0 root=/dev/vda rootfstype=ext4 ro";

  # Debian's cloud kernel, same upstream version as the nocloud
  # image's generic one (#37): CONFIG_EXT4_FS=y and
  # CONFIG_VIRTIO_PCI=y built in — only virtio_blk stays a module,
  # which the minimal initramfs below loads. Pinned by pool URL and
  # sha256; the deb carries vmlinuz, its config, and the matching
  # /usr/lib/modules tree.
  cloudKernelDeb = pkgs.fetchurl {
    url = "https://deb.debian.org/debian/pool/main/l/linux/"
      + "linux-image-6.12.107+deb13-cloud-amd64-unsigned_6.12.107-1_amd64.deb";
    hash = "sha256-5xJGCP1rv6GrcxSdxn7MgtCYWX11zx5UApHYynEUD+A=";
  };

  cloudKernel = pkgs.runCommand "msks-cloud-kernel"
    { nativeBuildInputs = [ pkgs.dpkg ]; }
    ''
      set -eu
      dpkg-deb -x ${cloudKernelDeb} "$out"
    '';

  # The minimal initramfs (#37): busybox, the one module the kernel
  # cannot mount root without, and an init that mounts /dev/vda and
  # switch_roots into systemd. The generic Debian initrd this
  # replaces is a 34MB MODULES=most archive and sat ~2.5s deep in
  # the boot critical path.
  minimalInitrd = pkgs.runCommand "msks-minimal-initrd"
    {
      inherit cloudKernel;
      # Static: the initramfs has no dynamic loader. nixpkgs'
      # default busybox links against a store glibc.
      busybox = pkgs.pkgsStatic.busybox;
      nativeBuildInputs = [ pkgs.cpio pkgs.gzip pkgs.xz ];
    }
    ''
      set -eu
      mkdir -p "$out"/tree/bin "$out"/tree/modules \
        "$out"/tree/proc "$out"/tree/dev "$out"/tree/newroot
      cp "$busybox"/bin/busybox "$out"/tree/bin/busybox
      moddir="$cloudKernel/usr/lib/modules"
      moddir=$(echo "$moddir"/*)
      # .ko.xz: busybox insmod reads plain modules only.
      xz -dc "$moddir"/kernel/drivers/block/virtio_blk.ko.xz \
        > "$out"/tree/modules/virtio_blk.ko
      cat > "$out"/tree/init <<'INIT'
      #!/bin/busybox sh
      # Mount root and hand off to systemd (#37): keep this as small
      # as it looks — every millisecond here delays the console. On
      # any failure, a shell beats a silent hang in an 811KB
      # initramfs (the serial console is reachable).
      /bin/busybox mount -t proc proc /proc \
        && /bin/busybox mount -t devtmpfs devtmpfs /dev \
        && /bin/busybox insmod /modules/virtio_blk.ko \
        && /bin/busybox mount -t ext4 -o ro /dev/vda /newroot \
        || exec /bin/busybox sh
      /bin/busybox mount --move /dev /newroot/dev
      exec /bin/busybox switch_root /newroot /sbin/init
      INIT
      chmod +x "$out"/tree/init
      (cd "$out"/tree && find . | cpio -o -H newc --quiet | gzip -9) \
        > "$out"/initrd
      rm -rf "$out"/tree
    '';

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

    # Escape the default basic.target ordering (#37): the console
    # starts as soon as the vsock module is loaded, not after the
    # whole boot. A too-early start self-heals through Restart=
    # always, and StartLimitIntervalSec=0 keeps systemd's default
    # burst limit from ending those retries.
    printf '%s\n' \
      '[Unit]' \
      'Description=msks vsock console (one shell per connection)' \
      'Documentation=https://github.com/mcdonc/msks' \
      'ConditionPathExists=/dev/vsock' \
      'After=systemd-modules-load.service dev-pts.mount' \
      'DefaultDependencies=no' \
      'StartLimitIntervalSec=0' \
      ''' \
      '[Service]' \
      'ExecStart=/usr/bin/socat VSOCK-LISTEN:${toString vsockShellPort},reuseaddr,fork EXEC:/bin/bash,pty,ctty,echo=0,icanon=0,stderr,setsid' \
      'Restart=always' \
      'RestartSec=0.1' \
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

    # AppArmor profile loading costs ~0.7s of every boot (#37) and
    # confines nothing in a pristine workspace VM.
    ln -s /dev/null $out/etc/systemd/system/apparmor.service

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
        pkgs.kmod
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

      # The cloud kernel's module tree replaces the generic one
      # (#37): the running kernel is the cloud flavor, and a stale
      # vermagic tree would make every module probe miss. The
      # generic /boot payload (kernel, initrd) leaves with it — the
      # VM direct-boots artifacts kept outside the image.
      rm -rf "$root"/lib/modules/*
      rm -rf "$root"/usr/lib/modules/* 2>/dev/null || true
      rm -f "$root"/boot/vmlinuz-* "$root"/boot/initrd.img-* \
        "$root"/boot/System.map-* "$root"/boot/config-*
      mkdir -p "$root"/usr/lib/modules
      cp -a --no-preserve=ownership \
        "${cloudKernel}"/usr/lib/modules/. "$root"/usr/lib/modules/
      cp "${cloudKernel}"/boot/config-* "$root"/boot/

      # The deb ships no depmod metadata (its postinst generates it
      # on the target); generate it here so modprobe — the vsock
      # console's module load, udev alias lookups — can resolve
      # anything at all. The deb's module dirs copy read-only.
      find "$root"/usr/lib/modules -type d -exec chmod u+w {} +
      kver=$(ls "$root"/usr/lib/modules | head -1)
      depmod -b "$root" "$kver"
      test -s "$root"/usr/lib/modules/"$kver"/modules.dep

      # Boot diet (#37): drop the wants symlinks of units a
      # workspace never uses. Removing the symlink (not masking)
      # keeps the targets clean of failed jobs: networkd and
      # timesyncd have no network to serve, unattended-upgrades no
      # repo to reach, e2scrub_reap no LVM to reap.
      wants="$root"/etc/systemd/system
      # The image ships some wants directories read-only; the build
      # owns them now.
      chmod u+w "$wants"/*.target.wants "$wants"/*.target.requires 2>/dev/null || true
      rm -f "$wants"/multi-user.target.wants/systemd-networkd.service
      rm -f "$wants"/multi-user.target.wants/unattended-upgrades.service
      rm -f "$wants"/multi-user.target.wants/e2scrub_reap.service
      rm -f "$wants"/sockets.target.wants/systemd-networkd.socket
      rm -f "$wants"/sysinit.target.wants/systemd-resolved.service
      rm -f "$wants"/sysinit.target.wants/systemd-timesyncd.service
      rm -f "$wants"/network-online.target.wants/systemd-networkd-wait-online.service
      # The netplan renderer config re-enables networkd through the
      # systemd generator at every boot even with every wants
      # symlink gone; a workspace has no NIC to configure.
      chmod u+w "$root"/etc
      chmod -R u+w "$root"/etc/netplan
      rm -rf "$root"/etc/netplan

      # Sanity: this must be a bootable Debian.
      test -x "$root"/sbin/init
      test -x "$root"/usr/bin/socat
      test -n "$(ls "$root"/usr/lib/modules/*/kernel/drivers/block/virtio_blk.ko.xz)" \
        || { echo "cloud module tree missing virtio_blk"; exit 1; }

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

  # The canonical image artifact (#40): a container-image tar
  # (`podman load` compatible) in the containerDisk convention — one
  # layer carrying boot/ (kernel, initrd) and disk/ (rootfs.ext4,
  # image.json schema 2). Importable with podman/skopeo/plain tar,
  # and consumable as a containerDisk by the k8s backend later
  # (#15).
  bootTree = pkgs.runCommand "msks-image-boot-tree"
    {
      inherit debianRoot rootfs cloudKernel minimalInitrd;
      inherit imageName imageVersion kernelCmdline vsockShellPort;
    }
    ''
      set -eu
      vmlinuz=$(ls "$cloudKernel"/boot/vmlinuz-*)
      initrd="${minimalInitrd}/initrd"
      mkdir -p "$out"/boot "$out"/disk
      cp "$vmlinuz" "$out"/boot/vmlinuz
      cp "$initrd" "$out"/boot/initrd.img
      cp "${rootfs}/rootfs.ext4" "$out"/disk/rootfs.ext4
      kernel_version=$(basename "$vmlinuz" | sed 's/^vmlinuz-//')
      # Guard against version drift: the catalog label must match the
      # Debian tree this image actually wraps.
      shipped=$(cat "$debianRoot"/root/etc/debian_version)
      if [ "$shipped" != "${imageVersion}" ]; then
        echo "imageVersion ${imageVersion} != /etc/debian_version $shipped" >&2
        exit 1
      fi
      # Self-describing (#40 review): the archive alone builds a boot
      # spec — no sidecar metadata for foreign imports to miss.
      cat > "$out"/disk/image.json <<EOF
      {
        "schema": 2,
        "name": "${imageName}",
        "version": "${imageVersion}",
        "cmdline": "${kernelCmdline}",
        "vsock_shell_port": ${toString vsockShellPort},
        "kernel_version": "$kernel_version",
        "kernel_format": "bzImage"
      }
      EOF
    '';

  # The image archive: a container-image tar built with plain tar
  # instead of dockerTools (#40 review). The layout is the one
  # `podman save` writes (manifest.json +
  # <id>/{layer.tar,json,VERSION} + repositories; the format
  # originates with `docker save`, which is the last time docker is
  # mentioned here). The layer is UNCOMPRESSED (members readable in
  # place with `tar tf`, no decompression at import) and byte-stable
  # (--sort=name --mtime=@1 --owner=0 --group=0 --numeric-owner), so
  # identical rebuilds hash identically and the per-hash cache
  # dedupes across hosts and CI.
  imageArchive = pkgs.runCommand "msks-image-archive"
    {
      inherit bootTree imageName imageVersion;
      nativeBuildInputs = [ pkgs.gnutar ];
      imageId = "msks" + builtins.hashString "sha256" (imageName + ":" + imageVersion);
    }
    ''
      set -eu
      mkdir work
      # The layer: the containerDisk tree, uncompressed, sorted,
      # zeroed timestamps and ownership.
      tar --sort=name --mtime='@1' --owner=0 --group=0 --numeric-owner \
        -C "${bootTree}" -cf work/layer.tar .
      # Container-image bookkeeping.
      mkdir "work/$imageId"
      mv work/layer.tar "work/$imageId/layer.tar"
      printf '1.0' > "work/$imageId/VERSION"
      # A minimally valid image config: podman requires the rootfs
      # diff_ids (the uncompressed layer's digest).
      layer_digest=$(sha256sum "work/$imageId/layer.tar" | cut -d' ' -f1)
      printf '%s' \
        '{"architecture":"amd64","os":"linux","config":{},' \
        '"rootfs":{"type":"layers","diff_ids":["sha256:'"$layer_digest"'"]}}' \
        > "work/$imageId/json"
      # Unquoted heredocs: the env-provided name/version/imageId
      # expand in the shell.
      cat > work/manifest.json <<EOF
      [{"Config":"$imageId/json","RepoTags":["workspace-''${imageName}:''${imageVersion}"],"Layers":["$imageId/layer.tar"]}]
      EOF
      cat > work/repositories <<EOF
      {"workspace-''${imageName}":{"''${imageVersion}":"$imageId"}}
      EOF
      tar --sort=name --mtime='@1' --owner=0 --group=0 --numeric-owner \
        -C work -cf "$out" manifest.json repositories "$imageId"
    '';

in
pkgs.runCommand "msks-guest"
  {
    inherit debianRoot rootfs imageArchive imageName imageVersion;
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
    # The cloud kernel (#37) plus the minimal initramfs; the version
    # string (flavor name included) distinguishes it from the
    # generic kernel this image replaced.
    vmlinuz=$(ls "${cloudKernel}"/boot/vmlinuz-*)
    version=$(basename "$vmlinuz" | sed 's/^vmlinuz-//')
    cp "$vmlinuz" "$out/vmlinux"
    cp "${minimalInitrd}/initrd" "$out/initrd"
    cp "${rootfs}/rootfs.ext4" "$out/rootfs.ext4"
    # The canonical artifact: named by name-version, OCI layout inside.
    cp "${imageArchive}" "$out/workspace-''${imageName}-''${imageVersion}.tar"
    printf '%s' "workspace-''${imageName}-''${imageVersion}.tar" > "$out"/image-archive-name
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
