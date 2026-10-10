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
# files that ship beside it (./agent-toolchain.nix,
# ./guest-pi-extension.ts and the shrinkwrap
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
  imageBuild ? false,
  ...
}:

let
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

  # The workspace's interception CA (#427): the identity seed
  # stages it at /etc/msks at first boot, and the rebuild-time
  # evaluation inside the guest reads it (below). One probe of
  # the path, shared by every consumer, so no evaluation can see
  # the file appear mid-eval twice over.
  interceptorCA = /etc/msks/interceptor-ca.crt;
  hasInterceptorCA = builtins.pathExists interceptorCA;

  # The nix search path both surfaces ride (#274, #433): nix.conf
  # (nix.settings.nix-path — the lookup a stripped-env invocation
  # falls back to; nixos-rebuild spawns its nix calls with no
  # session NIX_PATH) and the login shells' NIX_PATH (nix.nixPath
  # — the channel module's stock three plus rebuild-ng's own
  # entrypoint). A LIST, never a colon-joined string: nix.conf
  # separates search-path entries on whitespace, so a
  # colon-joined string ships as ONE dead entry and
  # `<nixos-config>` stops resolving for exactly the env-less
  # callers that need it — the #427 fold unit was the first
  # canary. nixos-system is rebuild-ng's preferred entrypoint,
  # pointed at the baked channel's own eval-config wrapper so
  # `nix-build <nixos-system> -A config.system.build.toplevel`
  # evaluates the shipped configuration.
  nixSearchPath = [
    "nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos"
    "nixos-config=/etc/nixos/configuration.nix"
    "nixos-system=/nix/var/nix/profiles/per-user/root/channels/nixos/nixos"
    "/nix/var/nix/profiles/per-user/root/channels"
  ];
  # The console getty's per-instance drop-in (#481), shipped as a
  # systemd.packages entry: package drop-in dirs are lndir-merged
  # into the unit collection by nixpkgs' assembly — the one route
  # to an instance drop-in that stays inside the closure (a full
  # systemd.services unit named for the instance shadows the
  # template; asDropin leaves a broken wants symlink; the /etc
  # unit dir itself is a store symlink no bake may mkdir into).
  consoleGettyDropin = pkgs.runCommand "msks-console-getty-dropin" { } ''
    mkdir -p $out/lib/systemd/system/serial-getty@ttyS0.service.d
    printf '%s\n' \
      '[Unit]' \
      'After=msks-seed-hostname.service home.mount' \
      'X-RestartIfChanged=false' \
      '[Service]' \
      'Environment=TERM=xterm' \
      > $out/lib/systemd/system/serial-getty@ttyS0.service.d/10-msks-console.conf
  '';

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

  # The search-path halves (#274, #433): nix.conf for the
  # stripped-env callers (the rebuild tools), the session
  # NIX_PATH for every login shell — one list, both surfaces, so
  # an interactive `<nixpkgs>` and a unit's resolve identically.
  nix.settings.nix-path = nixSearchPath;
  nix.nixPath = nixSearchPath;

  system.stateVersion = lib.versions.majorMinor lib.version;

  # cloud-init owns the hostname (#370): the seed's
  # local-hostname names the workspace at first boot. A declared
  # value here would ship /etc/hostname as an immutable store
  # symlink and re-assert itself at every activation — both walls
  # the seed cannot get past — so NixOS declares none. The
  # activation shim below seeds the image default (msks-guest)
  # for a boot without a seed and applies whatever the plain
  # /etc/hostname file says; systemd applies it on every later
  # boot, and cloud-init's set-hostname stage rewrites it from the
  # seed's local-hostname.
  networking.hostName = "";
  networking.useDHCP = false;
  networking.useNetworkd = true;

  # The hostname file the comment above names (#370): activation
  # seeds it for the first boot of a seed-less workspace (systemd
  # reads /etc/hostname only when the file exists when PID 1
  # starts, and this image builds without one) and applies the
  # file's value to the running kernel; systemd does the same on
  # every later boot, and cloud-init's set-hostname stage rewrites
  # the file when the seed's local-hostname names the workspace.
  system.activationScripts.msksHostname.text = ''
    if [ ! -e /etc/hostname ]; then
      printf 'msks-guest\n' > /etc/hostname
    fi
    ${pkgs.nettools}/bin/hostname -F /etc/hostname
  '';

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
  # ships (#96, #82): the egress NIC driver (#52), the
  # egress NIC driver (#52), the ACPI button pair logind
  # answers the graceful shutdown with (#25), isofs (the
  # NoCloud seed disk is iso9660), crc32c-intel (ext4's
  # metadata_csum asks the crypto API for it), and the L3
  # recursion stack (tun + the nftables/NAT modules a
  # workspace hosting workspaces itself needs). KVM rides its
  # own unit: the flavor depends on the host CPU, and a failed
  # modules-load entry leaves a degraded boot.
  boot.kernelModules = [
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

  # The console (#481): ttyS0 carries both the kernel console
  # (console=ttyS0 above) and an autologin root getty — the daemon
  # bridges the serial device's socket to the client. The instance
  # is the nixpkgs TEMPLATE instantiated by getty-generator, with
  # services.getty.autologinUser baking --autologin root and the
  # shadow --login-program (NixOS ships no /bin/login) — it keeps
  # the template's Restart=always and PAMName=login. The
  # per-instance changes ride the drop-in above (consoleGettyDropin):
  # TERM for readline (#61); After= the seed's hostname unit and
  # home.mount — the console prompt is the readiness signal the
  # smoke harness (and an operator) keys on, so it waits for the
  # hostname (an interactive bash freezes $HOSTNAME at startup, and
  # NixOS's cloud-init runs at multi-user time, too late to rely
  # on) and for the home volume (a write that lands before the
  # mount goes to the overlay and the later mount hides it;
  # a volume-less nofail boot still reaches the prompt after the
  # 30s device timeout); X-RestartIfChanged so the #427 fold's
  # nixos-rebuild switch never restarts — and so kills — the live
  # console (a getty change takes effect on the next boot).
  systemd.packages = [ consoleGettyDropin ];

  # The seed's hostname, applied before any getty renders a
  # prompt (#481). cloud-init would set it (#370) — but NixOS runs
  # cloud-init.service at multi-user time, racing the getty's
  # interactive shell — and ordering the getty behind cloud-init
  # delays the console by everything cloud-init waits for.
  # Instead the seed's NoCloud meta-data is read directly, once,
  # before getty.target: the name lands in milliseconds, and
  # cloud-init's set-hostname later agrees with it. Idempotent by
  # content: a later boot reads the same seed, and a seed-less
  # workspace exits clean.
  systemd.services.msks-seed-hostname = {
    description = "msks: the seed's workspace hostname, before any getty (#481)";
    documentation = [ "https://github.com/mcdonc/msks" ];
    wantedBy = [ "multi-user.target" ];
    before = [ "getty.target" ];
    after = [ "dev-disk\x2dby\x2dlabel\x2dcidata.device" ];
    path = [
      pkgs.util-linux
      pkgs.coreutils
      pkgs.gnused
      pkgs.nettools
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu
      seed=/dev/disk/by-label/cidata
      [ -e "$seed" ] || exit 0
      mkdir -p /run/msks-seed
      mount -o ro "$seed" /run/msks-seed
      trap 'umount /run/msks-seed' EXIT
      name=$(sed -n 's/^local-hostname: //p' /run/msks-seed/meta-data | head -1)
      [ -n "$name" ] || exit 0
      printf '%s\n' "$name" > /etc/hostname
      hostname "$name"
    '';
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
      # The hostname module nixpkgs leaves out (its default list
      # runs update_hostname only — a declared networking.hostName
      # made set-hostname broken, and the fix was to drop it).
      # This image declares no hostname, so the stage works: the
      # list is nixpkgs' own with set_hostname restored ahead of
      # update_hostname, matching the Debian image's stock
      # cloud.cfg.
      cloud_init_modules = [
        "migrator"
        "seed_random"
        "bootcmd"
        "write-files"
        "growpart"
        "resizefs"
        "set_hostname"
        "update_hostname"
        "resolv_conf"
        "ca-certs"
        "rsyslog"
        "users-groups"
      ];
    };
  };

  # The workspace's interception CA folded into the system trust
  # store (#427). The identity seed (#424/#200) stages the
  # per-workspace certificate under /etc/msks at first boot, and
  # this module — re-evaluated inside the guest by the fold unit
  # below, after cloud-init has staged the file — reads it into
  # security.pki, so the system bundle the rebuild builds carries
  # the CA: every client trusts the interception leaves, not only
  # the ones that honor the seed's environment exports. The
  # pathExists guard keeps a boot ahead of the seed (and the
  # image build itself, on its host) evaluating the shipped
  # configuration exactly; the assertion below refuses an image
  # build on a host that carries the file, where evaluation would
  # otherwise bake a foreign CA into every guest. The path is the
  # guest's own reserved namespace — the seed's files under
  # /etc/msks are the only writers.
  security.pki.certificates = lib.optionals hasInterceptorCA [
    (builtins.readFile interceptorCA)
  ];

  # Node stays env-driven after the fold too: it ignores the
  # system trust store and adds roots through this variable
  # (#424), so the rebuilt system sets it declaratively — the
  # same value the seed's profile.d export carried, present for
  # every session shape once the fold has run.
  environment.sessionVariables = lib.optionalAttrs hasInterceptorCA {
    NODE_EXTRA_CA_CERTS = "/etc/msks/interceptor-ca.crt";
  };

  # The fold trigger (#427): one background rebuild per fresh
  # certificate. A oneshot after cloud-final (the stage that runs
  # the identity seed), gated by a marker holding the folded
  # certificate's hash — the same certificate no-ops on later
  # boots, a factory reset's re-mint rebuilds exactly once (the
  # reset drops the marker with the overlay). The rebuild never
  # blocks the workspace: the seed's staged exports already cover
  # the env-honoring clients from the first seconds, this unit
  # completes trust for the rest minutes later, and it runs at
  # low weight so a first boot's interactive work keeps the CPU.
  # The unit is conditioned on the staged certificate, so a
  # guest whose seed carried no CA block never starts it.
  systemd.services.msks-interceptor-ca = {
    description = "msks: fold the workspace's interceptor CA into the system trust bundle (#427)";
    documentation = [ "https://github.com/mcdonc/msks" ];
    after = [ "cloud-final.service" ];
    wantedBy = [ "multi-user.target" ];
    path = [
      pkgs.nixos-rebuild
      pkgs.nix
      pkgs.systemd
      pkgs.coreutils
    ];
    unitConfig = {
      ConditionPathExists = "/etc/msks/interceptor-ca.crt";
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      Nice = 19;
      CPUWeight = 20;
      IOSchedulingClass = "idle";
    };
    # The marker writes only after a successful switch: a failed
    # rebuild leaves no marker and the next boot retries the fold
    # (the failure stays visible on the unit until then).
    script = ''
      set -eu
      cert=/etc/msks/interceptor-ca.crt
      marker=/var/lib/msks/interceptor-ca.folded
      sum=$(sha256sum "$cert" | cut -d' ' -f1)
      if [ -r "$marker" ] && [ "$(cat "$marker")" = "$sum" ]; then
        exit 0
      fi
      install -d -m 0755 /var/lib/msks
      nixos-rebuild switch
      printf '%s\n' "$sum" > "$marker"
    '';
  };

  # The serial console is the guest's debug channel: autologin
  # root on ttyS0, pinned by the console drop-in above; NixOS's
  # getty module bakes --autologin into the getty/serial-getty/
  # console-getty templates, and systemd's getty-generator
  # instantiates serial-getty@ttyS0 from console=ttyS0.
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
  # home on the persistent /home volume (#14) — the seed creates
  # it on first boot (#171: every seed carries the home block, a
  # keyless one included).
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
  # own rsync. cloud-init/util-linux/iproute2 put the
  # operator-facing tools the Debian image ships in every PATH
  # (`cloud-init status`, blkid, ip) — the cloud-init units
  # carry their own job PATH, but a workspace console is a
  # login shell, not a cloud-init job.
  environment.systemPackages = [
    pkgs.rsync
    pkgs.cloud-init
    pkgs.util-linux
    pkgs.iproute2
    # ssh-keygen for the workspace's own key handling (the
    # Debian image's openssh carries it in /usr/bin).
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
    # curl (#424): the workspace's ordinary HTTPS client — the
    # probe e2e drives it against the daemon's own endpoint, and a
    # workspace without it reaches for the same download-on-first-
    # use path fd and rg stay off. The platform's own packaging,
    # like every tool on this profile.
    pkgs.curl
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
      assertion = !imageBuild || !hasInterceptorCA;
      message =
        ""
        + "this image build host carries /etc/msks/interceptor-ca.crt — "
        + "the evaluation would fold that certificate into every "
        + "guest's trust store; build the image from a host without it";
    }
    {
      assertion = lib.versionAtLeast pkgs.nodejs_22.version "22.19.0";
      message = "pi's engines floor (22.19.0) exceeds nixpkgs nodejs_22 (${pkgs.nodejs_22.version}) — the nixpkgs pin regressed";
    }
  ];
}
