# The deployment-host module (#231, the #229 architecture): msksd
# and cloud-hypervisor run directly on the NixOS host, and workspace
# microvms run first-level on the host's /dev/kvm — one VM boundary,
# the same trust model the appliance's egress machinery always had
# inside its own kernel, now the host's.
#
# Source map: nix/appliance-config.nix — the appliance's NixOS
# configuration. What survives the hoist, carried verbatim unless
# named below:
#
#   - the service identity (#101): a dedicated system user, /dev/kvm
#     through the kvm supplementary group, and the ambient
#     capabilities the enabled paths need (egress work keeps
#     CAP_NET_ADMIN, ambient so the tools and the VMM the daemon
#     execs keep them; a privileged port adds CAP_NET_BIND_SERVICE);
#   - the generated settings file: the same msksd.yaml keys the
#     appliance's msks-boot wrote (tool paths resolved against this
#     configuration's pkgs, and every tool the daemon execs by
#     settings default — the resize pair, the conntrack revocation
#     tool, the volume formatter), generated here at build time into
#     the store;
#   - the egress prerequisites: the kernel modules the daemon's
#     rulesets and taps need, and host IP forwarding.
#
# What deliberately does not survive (the appliance removal sweep's
# inventory, #232): the state-disk machinery, the store shapes and
# shares, the in-guest update channel, the sshd bridge listener, the
# nested-KVM module service, and the serial-console debug shell. The
# host's own systemd and /var carry what those units provided.
#
# Host requirement: an nixpkgs carrying python314 (the daemon's
# interpreter; nixos-26.05 does).
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.msksd;

  # The keys this module generates: every settings key with a
  # first-class option. Rejected in services.msksd.settings — the
  # option owns them, and a settings-layer override would desync the
  # unit contract (state_dir from StateDirectory, port from the
  # firewall rule).
  generatedKeys = [
    "state_dir"
    "host"
    "port"
    "cloud_hypervisor"
    "qemu_img"
    "mkfs_ext4"
    "resize2fs"
    "e2fsck"
    "mkisofs"
    "conntrack_tool"
    "egress_enabled"
    "egress_uplink"
    "ip_tool"
    "nft_tool"
  ];

  # The daemon closure, built against the consuming host's nixpkgs
  # (the same wiring the appliance used): python 3.14, the pinned
  # textual, and the NFQUEUE binding against nixpkgs'
  # libnetfilter_queue.
  msksd = pkgs.python314.pkgs.callPackage ./msks-pkg.nix {
    textual = pkgs.python314.pkgs.callPackage ./textual-pkg.nix { };
    netfilterqueue = pkgs.python314.pkgs.callPackage ./netfilterqueue-pkg.nix { };
  };

  # The generated msksd.yaml (docs/config.md is the key reference):
  # the appliance's msks-boot file, minus the appliance paths, plus
  # the tools that file's PATH carried (the #184 resize pair, the
  # consent-revocation conntrack tool) — pinned to store paths so no
  # host package set is assumed.
  settingsFile = pkgs.writeText "msksd.yaml" (
    lib.generators.toYAML { } (
      {
        state_dir = cfg.stateDir;
        host = cfg.listenAddress;
        port = cfg.port;
        cloud_hypervisor = "${cfg.cloudHypervisor}/bin/cloud-hypervisor";
        qemu_img = "${pkgs.qemu-utils}/bin/qemu-img";
        mkfs_ext4 = "${pkgs.e2fsprogs}/sbin/mkfs.ext4";
        resize2fs = "${pkgs.e2fsprogs}/sbin/resize2fs";
        e2fsck = "${pkgs.e2fsprogs}/sbin/e2fsck";
        mkisofs = "${pkgs.cdrtools}/bin/mkisofs";
        conntrack_tool = "${pkgs.conntrack-tools}/bin/conntrack";
        egress_enabled = cfg.egress.enable;
        ip_tool = "${pkgs.iproute2}/sbin/ip";
        nft_tool = "${pkgs.nftables}/sbin/nft";
      }
      // (lib.optionalAttrs (cfg.egress.uplink != null) {
        egress_uplink = cfg.egress.uplink;
      })
      // cfg.settings
    )
  );

  # The capability set per enabled path: egress work keeps
  # CAP_NET_ADMIN (taps, per-VM nftables chains, NAT); a privileged
  # port adds CAP_NET_BIND_SERVICE. Ambient so the tools and the VMM
  # the daemon execs keep them.
  capabilities =
    lib.optionals cfg.egress.enable [ "CAP_NET_ADMIN" ]
    ++ lib.optional (cfg.port < 1024) "CAP_NET_BIND_SERVICE";
in
{
  options.services.msksd = {
    enable = lib.mkEnableOption "msksd, the msks daemon (workspaces as cloud-hypervisor microvms)";

    package = lib.mkOption {
      type = lib.types.package;
      default = msksd;
      defaultText = lib.literalExpression "pkgs.python314.pkgs.callPackage ./nix/msks-pkg.nix";
      description = "The msksd package to run.";
    };

    cloudHypervisor = lib.mkOption {
      type = lib.types.package;
      default = pkgs.cloud-hypervisor;
      description = "The cloud-hypervisor the daemon drives for workspace microvms.";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        The address the API's HTTPS listener binds. The TLS
        certificate's name covers exactly this address, and the
        daemon serves it on every interface when set to a wildcard —
        pair a wildcard with openFirewall and a bootstrap token.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8660;
      description = "The port the API's HTTPS listener binds (a privileged port adds CAP_NET_BIND_SERVICE).";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/msksd";
      description = ''
        The daemon's state home: the SQLite catalog, TLS keys, the
        workspace volumes and seed disks. Must stay /var/lib/msksd —
        the service unit owns the directory through StateDirectory.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        A path string (pass /etc/msksd/env, not a store path — a
        path value would copy the secret into the store) to an
        EnvironmentFile holding MSKSD_* variables the file-based
        config cannot hold secrets above all: the
        MSKSD_BOOTSTRAP_TOKEN seeding the first API credential).
        Environment overrides the generated settings file, and the
        unit does not start until the file exists — fail-closed for
        a secrets file.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the API port in the host firewall (only relevant once listenAddress is not loopback).";
    };

    settings = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.oneOf [
          lib.types.bool
          lib.types.int
          lib.types.float
          lib.types.str
        ]
      );
      default = { };
      description = ''
        Extra msksd.yaml keys (docs/config.md) the module does not
        generate — the escape hatch for daemon settings without a
        first-class option. A generated key set here fails evaluation
        naming the first-class option to use instead.
      '';
    };

    egress = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          The egress-consent stack: per-VM taps,
          nftables/NFQUEUE enforcement, DHCP, DNS, and NAT, all
          daemon-managed with the service's ambient CAP_NET_ADMIN.
          Disabling it keeps workspaces vsock-only (the strongest
          default egress posture) and drops the capability grant and
          host IP forwarding with it.
        '';
      };

      uplink = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = ''
          The host interface workspace egress masquerades out of —
          the nftables NAT rule's oifname. Required when egress is
          enabled: a wrong name installs cleanly and NAT matches
          nothing, so there is no safe default. Set it to the host's
          default-route interface (ip route show default).
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.stateDir == "/var/lib/msksd";
        message = "services.msksd.stateDir must be /var/lib/msksd — the unit owns it through StateDirectory.";
      }
      {
        assertion = cfg.egress.enable -> cfg.egress.uplink != null;
        message = "services.msksd.egress.uplink is required when egress is enabled — set the host's default-route interface (ip route show default).";
      }
    ]
    ++ map (key: {
      assertion = !builtins.hasAttr key cfg.settings;
      message = "services.msksd.settings.${key} is generated by the module — set its first-class option under services.msksd instead.";
    }) generatedKeys;

    # The service identity (#101, verbatim from the appliance): a
    # dedicated system user; /dev/kvm arrives through the kvm
    # supplementary group (NixOS's udev rule assigns the node to the
    # group).
    users.groups.msksd = { };
    users.users.msksd = {
      isSystemUser = true;
      group = "msksd";
      home = cfg.stateDir;
      shell = "/run/current-system/sw/bin/nologin";
    };

    # The egress prerequisites (the appliance loaded these at boot;
    # deterministic, not on demand): the tap devices and the
    # nftables/NAT machinery the daemon's rulesets need. IP
    # forwarding stays a default the host's own config can steer.
    boot.kernelModules = lib.optionals cfg.egress.enable [
      "tun"
      "nf_tables"
      "nft_chain_nat"
      "nft_masq"
      "nft_ct"
      "nf_nat"
      "nf_conntrack"
    ];
    boot.kernel.sysctl."net.ipv4.ip_forward" = lib.mkIf cfg.egress.enable (
      lib.mkDefault 1
    );

    networking.firewall.allowedTCPPorts = lib.optionals cfg.openFirewall [
      cfg.port
    ];

    # The daemon (#101's privilege contract, verbatim from the
    # appliance): unprivileged user, kvm access through the group,
    # the capabilities of the enabled paths (see `capabilities`), a
    # crash-looping daemon restarting forever is the contract the
    # appliance shipped.
    systemd.services.msksd = {
      description = "msksd daemon (workspaces as cloud-hypervisor microvms)";
      documentation = [ "https://github.com/mcdonc/msks" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      unitConfig.StartLimitIntervalSec = 0;
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/msksd --config ${settingsFile}";
        User = "msksd";
        Group = "msksd";
        SupplementaryGroups = "kvm";
        AmbientCapabilities = lib.concatStringsSep " " capabilities;
        CapabilityBoundingSet = lib.concatStringsSep " " capabilities;
        StateDirectory = "msksd";
        Restart = "always";
        RestartSec = 1;
        StandardInput = "null";
        StandardOutput = "journal";
        StandardError = "journal";
      }
      // lib.optionalAttrs (cfg.environmentFile != null) {
        EnvironmentFile = cfg.environmentFile;
      };
    };
  };
}
