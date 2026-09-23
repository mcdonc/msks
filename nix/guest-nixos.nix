# The NixOS workspace guest image (#250).
#
# Second catalog image beside the Debian one: the kernel, initrd,
# and rootfs come from a NixOS system evaluated against the same
# pinned nixpkgs the devenv shell uses (the spike-205 pattern), so
# the guest's packages — console helper, cloud-init, sshd, rsync —
# are built by nixpkgs instead of fetched as Debian artifacts.
# Packaged into the same containerDisk archive contract
# (boot/vmlinuz, boot/initrd.img, disk/rootfs.ext4, disk/image.json
# schema 2) and registered under the `nixos` catalog name. The
# daemon takes no new machinery: every NixOS-vs-Debian difference
# the daemon acts on is declared in image.json (the capabilities),
# never keyed off the image's name.
#
# The whole rootfs carries the system closure — no virtiofs store
# share, no host store dependency, and no nix database anywhere:
# spike 205 proved NixOS boot and activation resolve every path
# with none. No microvm.nix and no shapes (#237's study,
# superseded): this file is a plain nixpkgs evaluation.
#
# A NixOS image needs no inode-metadata story (the Debian build
# records and restores every distro inode's mode/uid/gid):
# activation materializes /etc, /var, and the setuid wrappers on
# each workspace's own overlay at boot, so the image ships
# root:root everywhere and carries no setuid bits at all.
#
#   $out/vmlinux            - the NixOS kernel (bzImage; named
#                             "vmlinux" to match the
#                             MSKSD_TEST_VMLINUX contract;
#                             guest-manifest.json records the
#                             actual format).
#   $out/initrd             - NixOS stage-1, gzip, virtio-trimmed.
#   $out/rootfs.ext4        - the system closure as a fresh ext4
#                             image: the pristine base each
#                             workspace's overlay copies on write
#                             from (#14); guests mount it rw.
#   $out/guest-manifest.json - artifact names + the boot cmdline.
#   $out/workspace-nixos-<version>.tar - the catalog archive.
#
# Evaluate through the msks-build-guest script (`msks-build-guest
# nixos`); it pins nixpkgs to the devenv.lock revision.
{
  lib,
  pkgs,
}:

let
  # The port the guest's vsock console listens on; the daemon dials
  # it after the CONNECT handshake (#21). Same fixed port, same
  # manifest field, as the Debian image.
  vsockShellPort = 1023;

  # First-boot provisioning (#41): the seed-disk consumer is
  # NixOS's own cloud-init (nixpkgs builds it) — the declared
  # provisioner stays cloud-init, and the create-time contract
  # matches the Debian image exactly.
  imageProvisioner = "cloud-init";

  imageName = "nixos";

  consoleHelper = pkgs.callPackage ./console-helper-pkg.nix { };

  # The guest system: every contract item from docs/images.md is a
  # declarative NixOS setting — no overlay tree, no baked /etc.
  guest =
    {
      config,
      lib,
      pkgs,
      ...
    }:
    {
      nixpkgs.hostPlatform = "x86_64-linux";

      # Runtime accounts stay first-class: the identity seed (#248)
      # creates the workspace's login user with shadow's useradd at
      # first boot — mutableUsers (the default, kept deliberately)
      # is what lets that account and its group membership live in
      # /etc on the workspace's overlay and survive rebuilds. The
      # deployment-host stance (false — the config owns accounts)
      # would forfeit every seeded login user.
      users.mutableUsers = lib.mkDefault true;

      # Direct kernel boot off the ext4 archive: no bootloader, no
      # nix (the closure resolves with no database — spike 205), no
      # docs.
      boot.loader.grub.enable = false;
      nix.enable = false;
      documentation.enable = false;

      system.stateVersion = lib.versions.majorMinor lib.version;

      networking.hostName = "msks-guest";
      networking.useDHCP = false;
      networking.useNetworkd = true;

      # wait-online stays off (the Debian image's posture): a
      # link-less networkd — the no-egress workspace — never reaches
      # "online", and network-online.target must never stall a boot.
      systemd.network.wait-online.enable = false;

      # Whatever NIC appears takes an address over DHCP from the
      # daemon (#52); with no NIC (a no-egress workspace) the
      # .network matches nothing and networkd stays idle. resolved
      # serves the DHCP-offered resolver at 127.0.0.53.
      systemd.network.enable = true;
      systemd.network.networks."80-msks-egress" = {
        matchConfig.Name = "en* eth*";
        DHCP = "yes";
      };
      services.resolved.enable = true;

      # Root boots rw from the kernel cmdline (the per-workspace
      # overlay absorbs writes, #14); /home is the second
      # persistent disk, labeled msks-home, nofail + a device
      # timeout exactly like the Debian image's fstab.
      fileSystems."/" = {
        device = "/dev/vda";
        fsType = "ext4";
      };
      fileSystems."/home" = {
        device = "/dev/disk/by-label/msks-home";
        fsType = "ext4";
        options = [
          "defaults"
          "nofail"
          "x-systemd.device-timeout=30s"
        ];
      };

      boot.kernelParams = [ "console=ttyS0" ];

      # Stage-1 holds the discipline the Debian build's six-module
      # initramfs established (#37, docs/boot-speed.md): the initrd
      # carries the virtio pair plus the ext4 root-fs closure —
      # tens of milliseconds, not a MODULES=most archive. gzip
      # because that is the initramfs compression the VMM line has
      # always decompressed.
      boot.initrd.availableKernelModules = [
        "virtio_pci"
        "virtio_blk"
      ];
      boot.initrd.compressor = "gzip";

      # The runtime module set — the same closure the Debian image
      # ships (#96, #82): the vsock console transport (#21), the
      # egress NIC driver (#52), the ACPI button pair logind
      # answers the graceful shutdown with (#25), isofs (the
      # NoCloud seed disk is iso9660), crc32c-intel (ext4's
      # metadata_csum asks the crypto API for it), and the L3
      # recursion stack (tun + the nftables/NAT modules a
      # workspace hosting workspaces itself needs). KVM rides its
      # own unit: the flavor depends on the host CPU, and a failed
      # modules-load entry leaves a degraded boot.
      boot.kernelModules = [
        "vmw_vsock_virtio_transport"
        "virtio_net"
        "button"
        "evdev"
        "isofs"
        "crc32c-intel"
        "tun"
        "nf_tables"
        "nft_chain_nat"
        "nft_masq"
        "nft_ct"
        "nf_nat"
        "nf_conntrack"
      ];

      # The vsock console (#63): the helper binary plus the unit
      # shape the Debian image ships — DefaultDependencies=no so
      # the console starts as soon as the vsock module lands (#37's
      # escape from basic.target ordering), Restart=always +
      # StartLimitIntervalSec=0 so a too-early start self-heals.
      # TERM is the helper's own business (#61).
      systemd.services.msks-console = {
        description = "msks vsock console (one negotiated shell per connection)";
        documentation = [ "https://github.com/mcdonc/msks" ];
        after = [
          "systemd-modules-load.service"
          "dev-pts.mount"
        ];
        wantedBy = [ "multi-user.target" ];
        unitConfig = {
          ConditionPathExists = "/dev/vsock";
          DefaultDependencies = "no";
          StartLimitIntervalSec = 0;
        };
        serviceConfig = {
          ExecStart = "${consoleHelper}/bin/msks-console-helper ${toString vsockShellPort}";
          # The helper's auth (#123) shells out to `ssh-keygen`,
          # and the session shells it execs inherit this unit's
          # environment — a system service gets none of the profile
          # PATHs a login shell builds, so name the system profile
          # explicitly (the Debian image's /usr/bin needs no such
          # help).
          Environment = [ "PATH=/run/current-system/sw/bin:/bin" ];
          Restart = "always";
          RestartSec = "0.1";
          StandardInput = "null";
        };
      };

      # Nested KVM (#82): the flavor depends on the host CPU; a
      # workspace booted where vmx does not reach still boots — the
      # unit stays active (exited) and /dev/kvm simply never
      # appears.
      systemd.services.msks-kvm = {
        description = "msks nested-KVM module (inner workspace VMs)";
        after = [ "systemd-modules-load.service" ];
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${pkgs.runtimeShell} -c \"${pkgs.kmod}/bin/modprobe kvm-intel || ${pkgs.kmod}/bin/modprobe kvm-amd || true\"";
        };
      };

      # sshd posture (#110): every login is a key login; the
      # forward is the road in. Authentication policy only, never
      # algorithm policy (#115). Host keys generate per-workspace
      # at first boot (openssh's own unit) — none are baked.
      services.openssh = {
        enable = true;
        settings = {
          PasswordAuthentication = false;
          KbdInteractiveAuthentication = false;
          PermitRootLogin = "prohibit-password";
        };
      };

      # cloud-init over the NoCloud seed (#41): the same two pins
      # the Debian image's dropins make — the seed disk is the only
      # datasource, and network rendering stays off (networkd owns
      # the NIC). users [] keeps cloud-init from creating accounts
      # (#171): the image ships the msks workspace user (#63), the
      # identity seed (#248) makes the login user's home, and both
      # payload forms work (cloud-config YAML and #! scripts).
      services.cloud-init = {
        enable = true;
        settings = {
          datasource_list = [
            "NoCloud"
            "None"
          ];
          network.config = "disabled";
          users = [ ];
        };
      };

      # The serial console is the guest's debug channel: autologin
      # root on ttyS0 (the vsock console is the supported interactive
      # path), the same parity the Debian image ships. NixOS's getty
      # module bakes --autologin into the getty/serial-getty/console-
      # getty templates; systemd's getty-generator instantiates
      # serial-getty@ttyS0 from console=ttyS0.
      services.getty.autologinUser = "root";

      # One console look across images: the Debian guest's plain
      # PS1 (\u@\h:\w\$ — root@msks-guest:~# for root,
      # msks@msks-guest:~$ for the workspace user), not NixOS's
      # bracketed default. The smoke suite's prompt needles key on
      # the shape, and users get the same console whichever image a
      # workspace boots.
      programs.bash.promptInit = ''PS1='\u@\h:\w\$ ' '';

      # The console workspace user (#63): uid/gid 1000, locked
      # password (the console helper and ssh keys are the road in),
      # home on the persistent /home volume (#14) — the identity
      # seed creates it on first boot.
      users.users.msks = {
        uid = 1000;
        isNormalUser = true;
        group = "msks";
        home = "/home/msks";
        createHome = false;
        hashedPassword = "!";
        shell = "${pkgs.bashInteractive}/bin/bash";
      };
      users.groups.msks.gid = 1000;

      # The workspace user's sudo (#169): passwordless root, granted
      # to the workspace GROUP — the shipped msks user and any login
      # user the identity seed (#248) adds to the group — because the
      # password is locked by design, NOPASSWD is the only form that
      # can ever run, and a per-image declarative rule keeps the
      # policy owned by the config (a rebuilt guest keeps exactly
      # what it declares; the seed never writes sudo
      # configuration). NixOS delivers sudo itself as an
      # activation-built wrapper, not a setuid file.
      security.sudo.extraRules = [
        {
          groups = [ "msks" ];
          commands = [
            {
              command = "ALL";
              options = [ "NOPASSWD" ];
            }
          ];
        }
      ];

      # The sync half of the TCP service plane (#110): nixpkgs'
      # own rsync. The console helper rides the system profile too,
      # so `msks-console-helper` is on PATH like Debian's
      # /usr/bin copy. cloud-init/util-linux/iproute2 put the
      # operator-facing tools the Debian image ships in every PATH
      # (`cloud-init status`, blkid, ip) — the cloud-init units
      # carry their own job PATH, but a workspace console is a
      # login shell, not a cloud-init job.
      environment.systemPackages = [
        consoleHelper
        pkgs.rsync
        pkgs.cloud-init
        pkgs.util-linux
        pkgs.iproute2
        # The console helper's auth (#123) shells out to
        # `ssh-keygen -Y verify`; the Debian image's openssh
        # carries it in /usr/bin, so it rides the profile here too
        # (the helper is static and PATH-inherits from its service).
        pkgs.openssh
      ];
    };

  nixos =
    (import (pkgs.path + "/nixos") {
      system = "x86_64-linux";
      configuration = guest;
    }).config;

  toplevel = nixos.system.build.toplevel;
  kernel = nixos.boot.kernelPackages.kernel;
  kernelFile = nixos.system.boot.loader.kernelFile;
  initrd = "${nixos.system.build.initialRamdisk}/initrd";
  kernelVersion = kernel.modDirVersion;

  # Catalog identity: the NixOS release plus the toplevel's short
  # hash — the release label alone (26.05pre…) stays constant across
  # months of pin bumps while the closure changes, and two different
  # builds must never share a name:version in the catalog.
  imageVersion =
    let
      toplevelHash = builtins.substring 0 8 (
        lib.head (lib.splitString "-" (baseNameOf (toString toplevel)))
      );
    in
    "${nixos.system.nixos.version}-${toplevelHash}";

  # Same boot shape as the Debian image plus the stage-2 init:
  # NixOS's own init must be named on the cmdline (there is no
  # bootloader to encode it), and the store path resolves INSIDE
  # the guest's rootfs — the archive is self-contained on any
  # host that imports it.
  kernelCmdline = "console=ttyS0 root=/dev/vda rootfstype=ext4 rw init=${toplevel}/init";

  # The system closure, resolved by nix: store-paths lists every
  # path stage-2 activation and the units need.
  closureInfo = pkgs.closureInfo {
    rootPaths = [ toplevel ];
  };

  # The NixOS root tree: the closure under /nix/store plus the
  # empty mount points; activation materializes everything else
  # (/etc, /var, the wrappers) on the workspace's own overlay at
  # boot.
  nixosRoot =
    pkgs.runCommand "msks-nixos-root"
      {
        inherit
          closureInfo
          toplevel
          ;
        nativeBuildInputs = [ pkgs.gnutar ];
      }
      ''
        set -eu
        root=root-tree
        mkdir -p "$root"/nix/store \
          "$root"/{boot,dev,etc,home,proc,root,run,srv,sys,tmp,var}
        while read -r p; do
          cp -a "$p" "$root"/nix/store/
        done < "$closureInfo"/store-paths
        # A bootable tree: stage-2 init present, and the units the
        # contract names are wanted at boot — the console service,
        # cloud-init, sshd. A config that silently dropped one (the
        # enable flipped off, the wantedBy lost) fails the build
        # here, not a workspace's first boot.
        test -x "$toplevel"/init
        test -e "$toplevel"/etc/systemd/system/multi-user.target.wants/msks-console.service
        test -e "$toplevel"/etc/systemd/system/multi-user.target.wants/cloud-init.service
        test -e "$toplevel"/etc/systemd/system/multi-user.target.wants/sshd.service
        grep -q msks-console-helper "$closureInfo"/store-paths
        mkdir -p "$out"
        du -s --apparent-size --block-size=4096 "$root" | cut -f1 > "$out"/tree-blocks
        # The opaque-tar hop (the same discipline as the Debian
        # build): the tree rides the store as one blob, never as a
        # tree — the store's auto-optimise hardlinks identical
        # files inside tree-shaped paths, and mke2fs -d packs
        # hardlink groups as one inode the guest's runtime writes
        # would collide in. --hard-dereference flattens any
        # hardlink a builder left inside a store path for the same
        # reason; sorted member order keeps the hop deterministic.
        tar --sort=name --hard-dereference --owner=0 --group=0 \
          --numeric-owner -C "$root" -cf "$out"/root.tar .
      '';

  # mke2fs -d packs the tree into an ext4 image without mounting
  # anything — the build stays unprivileged and host-independent
  # (see the Debian build's rootfs for the fakeroot mechanics).
  # One fakeroot session owns the tree: chown -R 0:0 is the whole
  # metadata story (no inode manifest — see the header).
  packScript = pkgs.writeText "msks-nixos-rootfs-pack.sh" ''
    set -eu
    tree="''${PACK_TREE:?}"
    img="''${PACK_IMG:?}"
    blocks="''${PACK_BLOCKS:?}"
    fake_epoch="''${PACK_FAKE_EPOCH:?}"
    chown -R 0:0 "$tree"
    E2FSPROGS_FAKE_TIME="$fake_epoch" mke2fs -q -t ext4 -b 4096 -I 256 \
      -L msks-rootfs \
      -E hash_seed=00000000-0000-0000-0000-000000000000 \
      -d "$tree" "$img" "$blocks"
    E2FSPROGS_FAKE_TIME="$fake_epoch" tune2fs -U clear "$img" >/dev/null
  '';

  rootfs =
    pkgs.runCommand "msks-guest-nixos-rootfs"
      {
        inherit
          nixosRoot
          packScript
          ;
        nativeBuildInputs = [
          pkgs.e2fsprogs
          pkgs.fakeroot
          pkgs.gnutar
        ];
        fakeEpoch = 1262304000;
      }
      ''
        set -eu
        mkdir -p "$out"
        mkdir work
        tar -C work -xf "$nixosRoot"/root.tar
        chmod -R u+w work
        # Content plus 1G of slack, like the Debian image: the
        # base keeps room for activation writes, and the
        # per-workspace overlay (#14) carries whatever the guest
        # writes beyond it.
        PACK_TREE=work \
          PACK_IMG="$out/rootfs.ext4" \
          PACK_BLOCKS=$(( $(cat "$nixosRoot"/tree-blocks) + 262144 )) \
          PACK_FAKE_EPOCH="$fakeEpoch" \
          fakeroot -- /bin/sh -e "$packScript"
      '';

  bootTree =
    pkgs.runCommand "msks-image-nixos-boot-tree"
      {
        inherit
          rootfs
          initrd
          imageName
          imageVersion
          kernelCmdline
          vsockShellPort
          kernelVersion
          ;
        vmlinuz = "${kernel}/${kernelFile}";
      }
      ''
        set -eu
        mkdir -p "$out"/boot "$out"/disk
        cp "$vmlinuz" "$out"/boot/vmlinuz
        cp "$initrd" "$out"/boot/initrd.img
        cp "${rootfs}/rootfs.ext4" "$out"/disk/rootfs.ext4
        # Self-describing (#40): the archive alone builds a boot
        # spec. The capabilities carry the whole NixOS-vs-Debian
        # difference the daemon acts on — the same declared
        # cloud-init provisioner, the same prelude-v1 console, the
        # same bzImage direct boot.
        cat > "$out"/disk/image.json <<EOF
        {
          "schema": 2,
          "name": "${imageName}",
          "version": "${imageVersion}",
          "cmdline": "${kernelCmdline}",
          "vsock_shell_port": ${toString vsockShellPort},
          "console_protocol": "prelude-v1",
          "console_users": ["root", "msks"],
          "kernel_version": "${kernelVersion}",
          "kernel_format": "bzImage",
          "capabilities": {"provisioner": "${imageProvisioner}"}
        }
        EOF
      '';

  imageArchive = (pkgs.callPackage ./image-archive.nix { }).mkImageArchive {
    inherit
      bootTree
      imageName
      imageVersion
      ;
  };

in
pkgs.runCommand "msks-guest-nixos"
  {
    inherit
      nixosRoot
      rootfs
      imageArchive
      imageName
      imageVersion
      initrd
      kernelVersion
      ;
    vmlinuz = "${kernel}/${kernelFile}";
    passthru = {
      inherit
        imageArchive
        toplevel
        ;
      inherit
        kernelCmdline
        vsockShellPort
        ;
    };
  }
  ''
    set -eu
    mkdir -p "$out"
    cp "$vmlinuz" "$out"/vmlinux
    cp "$initrd" "$out"/initrd
    cp "${rootfs}/rootfs.ext4" "$out"/rootfs.ext4
    # The canonical artifact: named by name-version, OCI layout inside.
    cp "${imageArchive}" "$out/workspace-''${imageName}-''${imageVersion}.tar"
    printf '%s' "workspace-''${imageName}-''${imageVersion}.tar" > "$out"/image-archive-name
    printf '%s' "${kernelVersion}" > "$out"/kernel-version
    cat > "$out"/guest-manifest.json <<EOF
    {
      "schema": 1,
      "kernel_version": "${kernelVersion}",
      "kernel_format": "bzImage",
      "cmdline": "${kernelCmdline}",
      "vmlinux": "vmlinux",
      "initrd": "initrd",
      "rootfs": "rootfs.ext4",
      "vsock_shell_port": ${toString vsockShellPort},
      "console_protocol": "prelude-v1",
      "console_users": ["root", "msks"]
    }
    EOF
  ''
