# Guest VM assets for direct kernel boot on cloud-hypervisor (#5).
#
# Everything a microvm needs comes out of this file as plain store
# paths, built from the pinned nixpkgs by pure derivations: the build
# runs on any Linux host with nix and touches nothing outside the
# repo.
#
#   $out/vmlinux            - the stock nixpkgs kernel image (bzImage
#                             format; the kernel carries the PVH entry
#                             point itself, CONFIG_PVH=y). Named
#                             "vmlinux" to match the MSKSD_TEST_VMLINUX
#                             contract; guest-manifest.json records the
#                             actual format.
#   $out/initrd             - static busybox + the virtio/ext4 modules
#                             the stock kernel builds as modules
#                             (CONFIG_VIRTIO_*=m, CONFIG_EXT4_FS=m).
#   $out/rootfs.ext4        - read-only ext4 image around a static
#                             busybox closure.
#   $out/guest-manifest.json - artifact names + the boot cmdline.
#
# Evaluate through the devenv tasks (they pin nixpkgs to the
# devenv.lock revision); `nix-build nix/guest.nix -A guest` with plain
# NIX_PATH also works when the pinned channel is acceptable.
{
  lib,
  pkgs,
}:

let
  # The stock nixpkgs kernel: substituted from cache.nixos.org (no
  # local compile) and fully virtio-capable once the modules load.
  kernel = pkgs.linux;

  # Static busybox: the guest userspace closure is busybox alone (no
  # libc runtime to copy into the image), and the same binary is the
  # initrd's only userspace.
  busybox = pkgs.busybox.override { enableStatic = true; };

  kernelCmdline = "console=ttyS0 root=/dev/vda rootfstype=ext4 ro";

  # --- initrd ------------------------------------------------------------
  # The stock kernel builds virtio and ext4 as modules, so a direct
  # kernel boot needs an initrd that loads them before the rootfs can
  # be mounted.
  # The virtio/ext4 modules need no firmware blobs; makeModulesClosure
  # still requires a directory to scan for them.
  emptyFirmware = pkgs.runCommand "msks-empty-firmware" { } ''
    mkdir -p "$out/lib/firmware"
  '';

  modulesClosure = pkgs.makeModulesClosure {
    # The modular kernel keeps its module tree in the `modules` output
    # (out only carries bzImage and friends).
    kernel = kernel.modules;
    firmware = emptyFirmware;
    rootModules = [
      "virtio_pci"
      "virtio_blk"
      "ext4"
      # Power button + its input device: the host's graceful shutdown
      # (ch-remote shutdown) signals ACPI, and the guest's acpid turns
      # the button event into a poweroff.
      "button"
      "evdev"
    ];
  };

  initrdInit = pkgs.writeTextFile {
    name = "msks-initrd-init";
    executable = true;
    destination = "/init";
    text = ''
      #!/bin/busybox sh
      export PATH=/bin

      /bin/busybox mkdir -p /proc /sys /dev /newroot
      /bin/busybox mount -t proc none /proc
      /bin/busybox mount -t sysfs none /sys
      /bin/busybox mount -t devtmpfs none /dev

      echo "msks initrd: loading virtio and ext4 modules"
      /bin/busybox modprobe virtio_pci
      /bin/busybox modprobe virtio_blk
      /bin/busybox modprobe ext4

      # No udev here: poll for the virtio root disk to appear.
      n=0
      while [ ! -b /dev/vda ] && [ "$n" -lt 100 ]; do
        /bin/busybox sleep 0.1
        n=$((n + 1))
      done

      if [ ! -b /dev/vda ]; then
        echo "msks initrd: /dev/vda never appeared; dropping to a shell"
        exec /bin/busybox setsid /bin/busybox cttyhack /bin/busybox sh
      fi

      if ! /bin/busybox mount -t ext4 -o ro /dev/vda /newroot; then
        echo "msks initrd: mounting /dev/vda failed; dropping to a shell"
        exec /bin/busybox setsid /bin/busybox cttyhack /bin/busybox sh
      fi

      /bin/busybox umount /proc /sys
      /bin/busybox mkdir -p /newroot/dev
      /bin/busybox mount -o move /dev /newroot/dev
      echo "msks initrd: switching to the ext4 rootfs"
      exec /bin/busybox switch_root /newroot /init
    '';
  };

  initrd = pkgs.makeInitrd {
    name = "msks-guest-initrd";
    compressor = "gzip";
    contents = [
      {
        object = busybox;
        symlink = "/bin/busybox";
        suffix = "/bin/busybox";
      }
      {
        object = initrdInit;
        symlink = "/init";
        suffix = "/init";
      }
      {
        object = modulesClosure;
        symlink = "/lib/modules/${kernel.modDirVersion}";
        suffix = "/lib/modules/${kernel.modDirVersion}";
      }
    ];
  };

  # --- ext4 rootfs -------------------------------------------------------
  # PID 1 of the real root: mounts the pseudo-filesystems, announces
  # the boot on the serial console, and keeps respawning a shell on it.
  # The VM's lifetime is controlled from the host (ch-remote shutdown,
  # kill) — the shell loop keeps the guest alive if the console ever
  # delivers EOF.
  guestInit = pkgs.writeTextFile {
    name = "msks-guest-init";
    executable = true;
    text = ''
      #!/bin/sh
      export PATH=/bin

      /bin/busybox mkdir -p /proc /sys /dev /tmp /run
      /bin/busybox mount -t proc none /proc
      /bin/busybox mount -t sysfs none /sys
      # The initrd moves its devtmpfs onto /newroot/dev; only mount
      # our own when that did not happen.
      mount -t devtmpfs none /dev 2>/dev/null || true
      hostname msks-guest
      # Handle the host's ch-remote shutdown: cloud-hypervisor signals
      # the ACPI power button, acpid turns it into a guest poweroff.
      acpid

      echo
      echo "msks guest: kernel $(uname -r) up; busybox shell on console"
      echo "msks guest: run 'poweroff -f' to stop the VM from inside"
      while :; do
        setsid cttyhack /bin/busybox sh
        sleep 1
      done
    '';
  };

  # Staging tree for the image: the busybox closure under /nix/store,
  # an /init, and a minimal /bin + /etc.
  guestRoot = pkgs.runCommand "msks-guest-root" {
    inherit guestInit;
    closureInfo = pkgs.closureInfo { rootPaths = [ busybox ]; };
  } ''
    set -eu
    root="$out/root"
    mkdir -p "$root"/bin "$root"/dev "$root"/etc "$root"/mnt "$root"/proc \
      "$root"/run "$root"/sys "$root"/tmp "$root"/nix/store
    while IFS= read -r path; do
      cp -a -- "$path" "$root/nix/store/"
    done < "$closureInfo/store-paths"
    install -m 0755 "$guestInit" "$root/init"
    ln -s "${busybox}/bin/busybox" "$root/bin/busybox"
    # The rootfs boots read-only, so busybox --install cannot run there;
    # pre-create the applet links the guest init and shell use.
    for applet in sh ash ls cat uname ps mount umount dmesg poweroff \
      reboot vi hostname mkdir rmdir rm cp mv grep head tail wc id whoami \
      env uptime free clear dd sync sleep setsid cttyhack mknod chmod \
      chown date acpid; do
      ln -s busybox "$root/bin/$applet"
    done
    printf 'msks-guest\n' > "$root/etc/hostname"
    # Power button (host-side graceful shutdown) powers the VM off.
    printf 'button/power.* /bin/poweroff -f\n' > "$root/etc/acpid.conf"
  '';

  # mke2fs -d packs a directory into an ext4 image without mounting
  # anything — the whole build stays unprivileged and host-independent.
  rootfs = pkgs.runCommand "msks-guest-rootfs" {
    inherit guestRoot;
    nativeBuildInputs = [ pkgs.e2fsprogs ];
    fakeEpoch = 1262304000;
  } ''
    set -eu
    img="$out/rootfs.ext4"
    mkdir -p "$out"
    blocks=$(( $(du -s --apparent-size --block-size=4096 "$guestRoot/root" | cut -f1) * 2 + 8192 ))
    E2FSPROGS_FAKE_TIME="$fakeEpoch" mke2fs -q -t ext4 -b 4096 -I 256 \
      -L msks-rootfs \
      -E hash_seed=00000000-0000-0000-0000-000000000000 \
      -d "$guestRoot/root" "$img" "$blocks"
    E2FSPROGS_FAKE_TIME="$fakeEpoch" tune2fs -U clear "$img" >/dev/null
  '';

  # Machine-readable description of the artifacts: relative names, the
  # kernel they were built for, and the cmdline to boot them with.
  manifest = pkgs.writeText "guest-manifest.json" (
    builtins.toJSON {
      schema = 1;
      kernel_version = kernel.modDirVersion;
      kernel_format = "bzImage";
      cmdline = kernelCmdline;
      vmlinux = "vmlinux";
      initrd = "initrd";
      rootfs = "rootfs.ext4";
    }
  );
in
pkgs.runCommand "msks-guest"
  {
    inherit manifest;
    passthru = {
      inherit
        kernel
        initrd
        rootfs
        kernelCmdline
        ;
    };
  }
  ''
    set -eu
    mkdir -p "$out"
    cp "${kernel}/bzImage" "$out/vmlinux"
    cp "${initrd}/initrd" "$out/initrd"
    cp "${rootfs}/rootfs.ext4" "$out/rootfs.ext4"
    cp "${manifest}" "$out/guest-manifest.json"
  ''
