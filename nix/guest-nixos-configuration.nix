# The workspace guest's NixOS system configuration (#250, #274).
#
# This module IS the guest: the image build (nix/guest-nixos.nix)
# evaluates it to produce the kernel, initrd, and rootfs, and the
# image ships it at /etc/nixos/ — configuration.nix imports it —
# so `nixos-rebuild switch` inside a running workspace re-evaluates
# the same configuration the image was built from. One source of
# truth: every contract item (docs/images.md) is a declarative
# setting here, and a rebuild that drops one fails the same battery
# the image build runs, not a workspace's first boot.
#
# The module must stay guest-evaluable: it references only nix/
# files that ship beside it (./console-helper-pkg.nix,
# ./agent-toolchain.nix, ./guest-pi-extension.ts and the shrinkwrap
# pair the toolchain build reads) and sources under ../src that
# ship the same way — the image bakes the whole import chain into
# /etc/nixos.
#
# The file is imported TWICE with two different nixpkgs: the image
# build's pinned revision, and — inside a rebuilt workspace — the
# nixpkgs source the image baked into the store. Everything it
# declares must hold under both.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  # The port the guest's vsock console listens on; the daemon dials
  # it after the CONNECT handshake (#21). Same fixed port, same
  # manifest field, as the Debian image.
  vsockShellPort = 1023;

  consoleHelper = pkgs.callPackage ./console-helper-pkg.nix { };

  # The agent toolchain (#266, #268): the shared pins and offline
  # builds — pi, herdr, Claude Code — staged the NixOS way, through
  # the system profile. One file (nix/agent-toolchain.nix) owns
  # every pin; the Debian image stages the same derivations under
  # /usr/local. Node itself comes from nixpkgs below — the
  # platform's own packaging where it exists is the rule, and on
  # NixOS it exists (the Debian image stages the official tarball
  # because Debian's own Node is older than pi's engines floor).
  toolchain = pkgs.callPackage ./agent-toolchain.nix { };

  # The model-discovery extension (#266, #268): the shared source
  # file the tmpfiles rules below plant inside the guest.
  piExtension = ./guest-pi-extension.ts;
in
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
  # docs. nix itself stays ON (#274): the daemon, the build
  # users, and the nix CLI + nixos-rebuild (nixpkgs wires it to
  # the profile when nix.enable) are what let a workspace user
  # run `nixos-rebuild switch` against the store the image ships
  # — every path registered valid at build time, the pinned
  # nixpkgs source baked in as root's channel, and /etc/nixos
  # carrying this very configuration.
  boot.loader.grub.enable = false;
  nix.enable = true;
  documentation.enable = false;

  # The search path nixos-rebuild rides (#274): the tool spawns
  # its nix calls with a stripped environment, so the session
  # NIX_PATH never reaches them — nix.conf is the lookup every
  # invocation falls back to. The stock entries (nixpkgs, the
  # configuration, the channels profile) match the channel
  # module's session defaults; nixos-system is rebuild-ng's
  # preferred entrypoint, pointed at the baked channel's own
  # eval-config wrapper so `nix-build <nixos-system> -A
  # config.system.build.toplevel` evaluates the shipped
  # configuration.
  nix.settings.nix-path =
    "nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos"
    + ":nixos-config=/etc/nixos/configuration.nix"
    + ":nixos-system=/nix/var/nix/profiles/per-user/root/channels/nixos/nixos"
    + ":/nix/var/nix/profiles/per-user/root/channels";

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
    extraGroups = [ "wheel" ];
    home = "/home/msks";
    createHome = false;
    hashedPassword = "!";
    shell = "${pkgs.bashInteractive}/bin/bash";
  };
  users.groups.msks.gid = 1000;

  # The workspace user's sudo (#169): passwordless root, granted
  # to wheel — the conventional admin group NixOS itself ships,
  # carrying the workspace user and any login user the identity
  # seed (#248) joins at first boot — because the password is
  # locked by design, NOPASSWD is the only form that can ever
  # run, and a per-image declarative rule keeps the policy owned
  # by the config (a rebuilt guest keeps exactly what it
  # declares; the seed never writes sudo configuration). The
  # msks user's own primary group (gid 1000) stays for /home
  # ownership.
  security.sudo.extraRules = [
    {
      groups = [ "wheel" ];
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
    # The agent toolchain (#266, #268): nixpkgs' Node — the
    # platform's own packaging, current enough for pi's
    # engines floor (asserted below) — plus the shared pins.
    # The profile puts every bin on each login PATH, the same
    # posture the Debian image's /usr/local staging gives: a
    # workspace boots with a working agent toolchain and no
    # per-user installer steps. pi's `env node` shebang resolves
    # because Node rides the same profile; Claude Code ships as
    # the loader-patched variant (a stock NixOS ships no
    # /lib64 loader shim — see nix/agent-toolchain.nix). The
    # pins move with an image rebuild; a workspace that already
    # booted keeps what it booted with.
    pkgs.nodejs_22
    toolchain.piPackage
    toolchain.claudeLoaderPatched
    toolchain.herdrPackage
    # fd and rg (#272): pi resolves its fd and rg tools from
    # PATH (fd, fdfind, or rg) and downloads them from GitHub
    # releases when it finds none — a download a fresh
    # workspace's first agent start would otherwise wait on,
    # behind the egress interceptor. nixpkgs' own packages put
    # both on the same profile PATH the toolchain rides
    # (nixpkgs' fd ships the `fd` name pi accepts).
    pkgs.fd
    pkgs.ripgrep
  ];

  # The model-discovery extension (#266, #268): planted the
  # NixOS way. tmpfiles copies the store file into /etc/skel —
  # every account the identity seed provisions copies the
  # skeleton at useradd -m — and into root's home, since root
  # seeds no skeleton. Copy-once semantics, and a real file
  # rather than a store symlink, keep a user's or root's later
  # edits theirs: nothing ever re-overwrites a copy that
  # exists, and within one workspace the closure is frozen in
  # the rootfs, so the once-only copy is also the every-boot
  # copy. The rules run at sysinit, ahead of the cloud-init
  # that runs the seed's useradd.
  systemd.tmpfiles.rules = [
    "d /etc/skel/.pi/agent/extensions 0755 root root -"
    "d /root/.pi/agent/extensions 0755 root root -"
    "C /etc/skel/.pi/agent/extensions/llm-models.ts 0644 root root - ${piExtension}"
    "C /root/.pi/agent/extensions/llm-models.ts 0644 root root - ${piExtension}"
  ];

  # Every `env`-shebang in the toolchain (`env node` for pi,
  # and whatever the build leaves beside it) resolves through
  # this activation-built /usr/bin/env. NixOS's default is the
  # same coreutils env; the guest states it because the whole
  # toolchain depends on it — a config that dropped it would
  # break every shebang at once, far from the cause.
  environment.usrbinenv = "${pkgs.coreutils}/bin/env";

  # pi's engines floor holds against whichever nixpkgs
  # evaluates this module: a pin move that regressed Node below
  # it must fail the evaluation (the image build's, or a
  # workspace rebuild's), not boot a workspace whose pi refuses
  # to start.
  assertions = [
    {
      assertion = lib.versionAtLeast pkgs.nodejs_22.version "22.19.0";
      message = "pi's engines floor (22.19.0) exceeds nixpkgs nodejs_22 (${pkgs.nodejs_22.version}) — the nixpkgs pin regressed";
    }
  ];
}
