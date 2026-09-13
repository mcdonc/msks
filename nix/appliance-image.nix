# The msksd appliance image (#10).
#
# Built exactly like the workspace guest (nix/guest-assets.nix): pure
# derivations from the pinned nixpkgs, direct kernel boot, an ext4
# rootfs around a busybox + shell-script init. No NixOS, no module
# system — the host OS is irrelevant beyond nix + KVM.
#
#   $out/vmlinux            - the stock nixpkgs kernel (bzImage; the
#                             kernel carries CONFIG_PVH=y)
#   $out/initrd             - busybox + the modules needed to mount the
#                             root disk (virtio/blk/ext4 + ACPI button)
#   $out/rootfs.ext4        - the appliance OS: busybox, the init,
#                             a module tree with kvm/virtiofs/virtio_net
#   $out/state.ext4         - a blank persistent-state disk template
#                             (the up-task copies it once per install)
#   $out/appliance-manifest.json - store paths the host must realize
#                             (msksd closure, cloud-hypervisor, module
#                             set) + the network plan the init follows
#
# The heavy runtime (msksd's python closure, cloud-hypervisor for
# workspace VMs) is NOT copied into the image: at boot the init mounts
# the host's /nix/store read-only over virtiofs (tag=store), so the
# appliance runs the same store paths the host built — and workspace
# guest assets built by msks:build-guest flow in with zero copying.
# The manifest keeps those paths alive on the host via GC roots.
{
  lib,
  pkgs,
}:

let
  # The stock nixpkgs kernel: virtio, virtiofs and KVM all live in its
  # module tree; host-passthrough CPUs (the --cpus host=true flag the
  # up-task passes) expose the virtualization extensions the kvm
  # modules need for nested virt.
  kernel = pkgs.linux;

  busybox = pkgs.pkgsStatic.busybox;

  # The daemon closure, built from this repo by nixpkgs' python
  # machinery (nix/msks-pkg.nix). Interpolated into the init below:
  # the path resolves inside the guest through the virtiofs store
  # share, and the manifest reference keeps it realized on the host.
  msks = pkgs.python314.pkgs.callPackage ./msks-pkg.nix { };

  # The VMM for workspace VMs booted INSIDE the appliance: same
  # cloud-hypervisor the devenv shell pins.
  vmm = pkgs.cloud-hypervisor;

  # Network plan (the up-task mirrors it on the host bridge):
  net = {
    address = "192.168.77.2";
    prefixLength = 24;
    gateway = "192.168.77.1";
  };

  kernelCmdline = "console=ttyS0 root=/dev/vda rootfstype=ext4 ro";

  emptyFirmware = pkgs.runCommand "msks-empty-firmware" { } ''
    mkdir -p "$out/lib/firmware"
  '';

  # Modules the INITRD needs: mount the ext4 root over virtio-blk and
  # react to the ACPI power button (ch-remote shutdown signaling).
  initrdClosure = pkgs.makeModulesClosure {
    kernel = kernel.modules;
    firmware = emptyFirmware;
    rootModules = [
      "virtio_pci"
      "virtio_blk"
      "ext4"
      "button"
      "evdev"
    ];
  };

  initrdInit = pkgs.writeTextFile {
    name = "msks-appliance-initrd-init";
    executable = true;
    destination = "/init";
    text = ''
      #!/bin/busybox sh
      export PATH=/bin

      /bin/busybox mkdir -p /proc /sys /dev /newroot
      /bin/busybox mount -t proc none /proc
      /bin/busybox mount -t sysfs none /sys
      /bin/busybox mount -t devtmpfs none /dev

      /bin/busybox modprobe virtio_pci
      /bin/busybox modprobe virtio_blk
      /bin/busybox modprobe ext4

      n=0
      while [ ! -b /dev/vda ] && [ "$n" -lt 100 ]; do
        /bin/busybox sleep 0.1
        n=$((n + 1))
      done

      if [ ! -b /dev/vda ]; then
        echo "msks appliance initrd: /dev/vda never appeared; dropping to a shell"
        exec /bin/busybox setsid /bin/busybox cttyhack /bin/busybox sh
      fi

      if ! /bin/busybox mount -t ext4 -o ro /dev/vda /newroot; then
        echo "msks appliance initrd: mounting /dev/vda failed; dropping to a shell"
        exec /bin/busybox setsid /bin/busybox cttyhack /bin/busybox sh
      fi

      /bin/busybox umount /proc /sys
      /bin/busybox mkdir -p /newroot/dev
      /bin/busybox mount -o move /dev /newroot/dev
      echo "msks appliance initrd: switching to the ext4 rootfs"
      exec /bin/busybox switch_root /newroot /init
    '';
  };

  initrd = pkgs.makeInitrd {
    name = "msks-appliance-initrd";
    compressor = "gzip";
    contents = [
      {
        object = initrdInit;
        symlink = "/init";
        suffix = "/init";
      }
      {
        object = busybox;
        symlink = "/bin/busybox";
        suffix = "/bin/busybox";
      }
      {
        object = initrdClosure;
        symlink = "/lib/modules/${kernel.modDirVersion}";
        suffix = "/lib/modules/${kernel.modDirVersion}";
      }
    ];
  };

  # Modules the REAL ROOT needs: the virtiofs store mount, the NIC,
  # and the nested-KVM stack for workspace VMs. makeModulesClosure
  # walks dependencies, so modprobe resolves inside the rootfs.
  rootModulesClosure = pkgs.makeModulesClosure {
    kernel = kernel.modules;
    firmware = emptyFirmware;
    rootModules = [
      "virtio_pci"
      "virtiofs"
      "virtio_net"
      "kvm"
      "kvm_intel"
      "kvm_amd"
    ];
  };

  # PID 1 of the appliance: pseudo-filesystems, the read-only store
  # share, the state disk, the NIC, nested KVM — then exec msksd.
  applianceInit = pkgs.writeTextFile {
    name = "msks-appliance-init";
    executable = true;
    text = ''
      #!/bin/sh
      export PATH=/bin

      /bin/busybox mkdir -p /proc /sys /dev /tmp /run /nix/store /state
      /bin/busybox mount -t proc none /proc
      /bin/busybox mount -t sysfs none /sys
      mount -t devtmpfs none /dev 2>/dev/null || true
      # The rootfs is read-only: scratch space must be tmpfs, or any
      # tempfile/mktemp use EROFSes.
      mount -t tmpfs none /tmp
      mount -t tmpfs none /run

      # The host's /nix/store, read-only: everything heavy — msksd's
      # closure, the workspace VMM, guest assets — resolves through it.
      /bin/busybox modprobe virtio_pci
      /bin/busybox modprobe virtiofs
      if ! mount -t virtiofs -o ro store /nix/store; then
        echo "msks appliance: mounting the store share failed; dropping to a shell"
        exec setsid cttyhack /bin/busybox sh
      fi

      # Persistent state (sqlite, workspace overlays, logs): the second
      # disk. A fresh one fails to mount ext4; mkfs it once and retry.
      /bin/busybox modprobe virtio_blk
      if ! mount -t ext4 /dev/vdb /state; then
        echo "msks appliance: fresh state disk; formatting"
        /bin/busybox mke2fs -F /dev/vdb
        mount -t ext4 /dev/vdb /state
      fi

      # The NIC: static plan recorded in the manifest (the host bridge
      # mirrors it; see scripts/appliance-setup.sh).
      /bin/busybox modprobe virtio_net
      ip link set lo up
      ip link set eth0 up
      ip addr add ${net.address}/${toString net.prefixLength} dev eth0
      ip route add default via ${net.gateway}
      hostname msksd-appliance

      # Nested KVM for workspace VMs: host-passthrough CPUs expose the
      # virtualization extensions; /dev/kvm appearing is the contract.
      modprobe kvm_intel 2>/dev/null || modprobe kvm_amd 2>/dev/null || true
      if [ ! -e /dev/kvm ]; then
        mknod /dev/kvm c 10 232
      fi

      # React to the host's ch-remote shutdown (ACPI power button).
      acpid

      # Every msksd.<name>=<value> pair on the kernel cmdline
      # becomes an MSKSD_<NAME> environment variable (upper-cased;
      # dots map to underscores). Cmdline delivery survives
      # state-disk recreation and unclean shutdowns, unlike files
      # seeded onto the journaled ext4 from outside — and the host
      # controls daemon settings (the bootstrap token; the console
      # bring-up wait on slow nested-virt hosts) without an image
      # rebuild. Variable NAMES are echoed to the serial log, never
      # values.
      cmdline_names=""
      for pair in $(cat /proc/cmdline); do
        case "$pair" in
          msksd.*=*)
            key="''${pair#msksd.}"
            key="''${key%%=*}"
            var="$(printf '%s' "$key" | tr 'a-z.' 'A-Z_')"
            export MSKSD_"$var"="''${pair#*=}"
            cmdline_names="$cmdline_names $var"
            ;;
        esac
      done
      echo "msks appliance: cmdline env:$cmdline_names"

      echo
      echo "msks appliance: kernel $(uname -r) up; execing msksd"
      echo "msks appliance: serving https://${net.address}:8660 (TOFU fingerprint on the serial log)"

      export PATH="${vmm}/bin:${msks}/bin:$PATH"
      export MSKSD_STATE_DIR=/state
      export MSKSD_HOST=0.0.0.0
      export MSKSD_PORT=8660
      export MSKSD_CLOUD_HYPERVISOR="${vmm}/bin/cloud-hypervisor"
      # Debug escape hatch: a /state/debug-shell marker (seeded onto
      # the state disk from the host) backgrounds the daemon and gives
      # the console an interactive shell instead of exec'ing PID 1.
      if [ -e /state/debug-shell ]; then
        ( sleep 2; "${msks}/bin/msksd" ) &
        echo "msks appliance: DEBUG SHELL on console"
        echo "=== DIAG ==="
        ls -l /dev/kvm 2>&1 || echo "NO /dev/kvm node"
        modprobe kvm_intel 2>&1; echo "modprobe kvm_intel rc=$?"
        modprobe kvm_amd 2>&1; echo "modprobe kvm_amd rc=$?"
        ls -l /dev/kvm 2>&1 || echo "still NO /dev/kvm"
        grep -mcE "vmx|svm" /proc/cpuinfo
        "${vmm}/bin/cloud-hypervisor" --version 2>&1 || echo "CH EXEC FAIL rc=$?"
        ls /nix/store | head -3
        # Host-editable diagnostics: seed /state/diag.sh from outside.
        if [ -f /state/diag.sh ]; then
          echo "--- diag.sh ---"
          sh /state/diag.sh 2>&1
          echo "--- diag.sh end ---"
        fi
        echo "=== DIAG-END ==="
        exec setsid cttyhack /bin/busybox sh
      fi
      exec "${msks}/bin/msksd"
    '';
  };

  applianceRoot = pkgs.runCommand "msks-appliance-root" {
    inherit applianceInit;
  } ''
    set -eu
    root="$out/root"
    mkdir -p "$root"/bin "$root"/dev "$root"/etc "$root"/mnt "$root"/proc \
      "$root"/run "$root"/sys "$root"/tmp "$root"/state "$root"/nix/store \
      "$root"/lib/modules
    install -m 0755 "$applianceInit" "$root/init"
    # busybox as a REAL file, not a store symlink: /init needs /bin/sh
    # before the virtiofs store share mounts, so the appliance rootfs
    # cannot resolve absolute /nix/store paths yet (unlike the workspace
    # guest, which embeds its whole closure instead).
    cp -L "${busybox}/bin/busybox" "$root/bin/busybox"
    chmod 0755 "$root/bin/busybox"
    for applet in sh ash ls cat uname ps mount umount dmesg poweroff \
      reboot vi hostname mkdir rmdir rm cp mv grep head tail wc id whoami \
      env uptime free clear dd sync sleep setsid cttyhack mknod chmod \
      chown date acpid ip mke2fs modprobe sed tr; do
      ln -s busybox "$root/bin/$applet"
    done
    printf 'msksd-appliance\n' > "$root/etc/hostname"
    # Power button (host-side graceful shutdown) powers the VM off.
    printf 'button/power.* /bin/poweroff -f\n' > "$root/etc/acpid.conf"
    # The module tree the init modprobes from (kvm, virtiofs, net).
    cp -a -- "${rootModulesClosure}/lib/modules"/* "$root/lib/modules/"
  '';

  rootfs = pkgs.runCommand "msks-appliance-rootfs" {
    inherit applianceRoot;
    nativeBuildInputs = [ pkgs.e2fsprogs ];
    fakeEpoch = 1262304000;
  } ''
    set -eu
    mkdir -p "$out"
    img="$out/rootfs.ext4"
    # Size from the staging tree (x2 slack + fixed headroom), the same
    # recipe as the workspace guest's rootfs: deterministic and never
    # rounded by mke2fs' own minimum.
    blocks=$(( $(du -s --apparent-size --block-size=4096 "$applianceRoot/root" | cut -f1) * 2 + 8192 ))
    E2FSPROGS_FAKE_TIME="$fakeEpoch" mke2fs -q -t ext4 -b 4096 -I 256 \
      -L msks-rootfs \
      -E hash_seed=00000000-0000-0000-0000-000000000001 \
      -d "$applianceRoot/root" "$img" "$blocks"
    E2FSPROGS_FAKE_TIME="$fakeEpoch" tune2fs -U 00000000-0000-0000-0000-000000000002 "$img" >/dev/null
  '';

  # Persistent-state template: blank ext4. The up-task copies it once
  # per install; the appliance's init formats-and-retries if handed a
  # blank/foreign disk, so either path converges.
  stateDisk = pkgs.runCommand "msks-appliance-state" {
    nativeBuildInputs = [ pkgs.e2fsprogs ];
    fakeEpoch = 1262304000;
  } ''
    set -eu
    mkdir -p "$out"
    truncate -s 1G "$out/state.ext4"
    E2FSPROGS_FAKE_TIME="$fakeEpoch" mke2fs -q -F -t ext4 -b 4096 -I 256 \
      -L msks-state \
      -E hash_seed=00000000-0000-0000-0000-000000000003 \
      "$out/state.ext4"
    E2FSPROGS_FAKE_TIME="$fakeEpoch" tune2fs -U 00000000-0000-0000-0000-000000000004 "$out/state.ext4" >/dev/null
  '';

  manifest = pkgs.writeText "appliance-manifest.json" (builtins.toJSON {
    kernel = "${kernel}";
    initrd = "${initrd}";
    rootfs = "${rootfs}/rootfs.ext4";
    stateDisk = "${stateDisk}/state.ext4";
    cmdline = kernelCmdline;
    network = net;
    msksd = "${msks}";
    vmm = "${vmm}";
    modules = "${rootModulesClosure}";
  });
in
pkgs.runCommand "msks-appliance"
  {
    inherit rootfs stateDisk manifest kernel initrd;
    # The outputs referenced only by the manifest text: making them
    # build-input-style deps of this derivation keeps them realized on
    # the host (the guest sees them through the virtiofs share).
    deps = [
      kernel
      initrd
      msks
      vmm
      rootModulesClosure
    ];
  }
  ''
    set -eu
    mkdir -p "$out"
    cp -L "$kernel/bzImage" "$out/vmlinux"
    cp -L "$initrd/initrd" "$out/initrd"
    cp -L "$rootfs/rootfs.ext4" "$out/rootfs.ext4"
    cp -L "$stateDisk/state.ext4" "$out/state.ext4"
    cp -L "$manifest" "$out/appliance-manifest.json"
  ''
