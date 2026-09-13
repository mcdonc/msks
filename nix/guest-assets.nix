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
  # initrd's only userspace. pkgsStatic (musl) rather than an
  # enableStatic override: the override's output is not on
  # cache.nixos.org, so every cold machine compiled busybox from
  # source; pkgsStatic.busybox is substituted as a prebuilt binary
  # while staying inside the pinned nixpkgs.
  busybox = pkgs.pkgsStatic.busybox;

  # Static socat: the vsock console server (#21). nixpkgs' 1.8.x
  # builds it with WITH_VSOCK (verify with `socat -V`), so the guest
  # can listen on AF_VSOCK and hand out one pty-backed shell per
  # connection without any other guest userspace.
  socat = pkgs.pkgsStatic.socat;

  # The port the guest's vsock shell server listens on; the daemon's
  # console proxy connects to it after the CONNECT handshake. Fixed
  # and recorded in the manifest so both sides agree.
  vsockShellPort = 1023;

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
      # The vsock console transport (#21). The module name is
      # vmw_vsock_virtio_transport; virtio_vsock has never existed as
      # a module name.
      "vmw_vsock_virtio_transport"
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
      /bin/busybox modprobe vmw_vsock_virtio_transport

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
      # devtmpfs does not create /dev/ptmx on its own: mount devpts
      # and link the master device, or no pty can be allocated — the
      # vsock shell server needs one per connection (#21).
      /bin/busybox mkdir -p /dev/pts
      /bin/busybox mount -t devpts -o gid=5,mode=620 devpts /dev/pts
      /bin/busybox ln -sf pts/ptmx /dev/ptmx
      hostname msks-guest
      # devtmpfs does not create /dev/vsock either: read the misc
      # minor and mknod it (major 10 = misc). Without the node,
      # socket(AF_VSOCK) fails with ENODEV (#21).
      # devtmpfs creates the node on some kernels; make it ourselves
      # only when missing.
      if [ ! -e /dev/vsock ]; then
        vsock_minor=$(grep vsock /proc/misc | cut -d" " -f1)
        [ -n "$vsock_minor" ] && /bin/busybox mknod /dev/vsock c 10 "$vsock_minor"
      fi
      # Handle the host's ch-remote shutdown: cloud-hypervisor signals
      # the ACPI power button, acpid turns it into a guest poweroff.
      acpid

      # The vsock shell server (#21): one interactive busybox ash on
      # a pty per vsock connection. The daemon's /console proxy dials
      # the VMM's unix socket, sends "CONNECT ${toString vsockShellPort}\n",
      # and gets raw bidirectional bytes — socat here is the accept
      # side. A root shell today: the guest userspace is
      # busybox-as-root (#5); a non-root shell lands with a real guest
      # userland. The serial console respawn loop below is untouched.
      if [ -e /dev/vsock ]; then
        /bin/socat VSOCK-LISTEN:${toString vsockShellPort},reuseaddr,fork \
          EXEC:/bin/ash,pty,ctty,stderr,setsid </dev/null >/dev/console 2>&1 &
        echo "msks guest: vsock shell listening on port ${toString vsockShellPort}"
      else
        echo "msks guest: no /dev/vsock; shell server not started"
      fi

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
    closureInfo = pkgs.closureInfo { rootPaths = [ busybox socat ]; };
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
    ln -s "${socat}/bin/socat" "$root/bin/socat"
    # The rootfs boots read-only, so busybox --install cannot run there;
    # pre-create the applet links the guest init and shell use.
    for applet in sh ash ls cat uname ps mount umount dmesg poweroff \
      reboot vi hostname mkdir rmdir rm cp mv grep head tail wc id whoami \
      env uptime free clear dd sync sleep setsid cttyhack mknod chmod \
      chown date acpid cut; do
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
      vsock_shell_port = vsockShellPort;
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
        vsockShellPort
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
