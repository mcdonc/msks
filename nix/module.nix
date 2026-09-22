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
#     through the kvm supplementary group, and the two ambient
#     capabilities (CAP_NET_ADMIN + CAP_NET_BIND_SERVICE — ambient so
#     the tools and the VMM the daemon execs keep them);
#   - the generated settings file: the same msksd.yaml keys the
#     appliance's msks-boot wrote (tool paths resolved against this
#     configuration's pkgs), generated here at build time into the
#     store;
#   - the egress prerequisites: the kernel modules the daemon's
#     rulesets and taps need, and host IP forwarding.
#
# What deliberately does not survive (the appliance removal sweep's
# inventory, #232): the state-disk machinery, the store shapes and
# shares, the in-guest update channel, the sshd bridge listener, the
# nested-KVM module service, and the serial-console debug shell. The
# host's own systemd and /var carry what those units provided.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.msksd;

  # The daemon closure, built against the consuming host's nixpkgs
  # (the same wiring the appliance used): python 3.14, the pinned
  # textual, and the NFQUEUE binding against nixpkgs'
  # libnetfilter_queue.
  msksd = pkgs.python314.pkgs.callPackage ./msks-pkg.nix {
    textual = pkgs.python314.pkgs.callPackage ./textual-pkg.nix { };
    netfilterqueue = pkgs.python314.pkgs.callPackage ./netfilterqueue-pkg.nix { };
  };

  # The generated msksd.yaml (docs/config.md is the key reference):
  # the appliance's msks-boot file, minus the appliance paths. The
  # settings option merges operator keys under the generated ones;
  # MSKSD_* environment variables (environmentFile) override both.
  settingsFile = pkgs.writeText "msksd.yaml" (
    lib.generators.toYAML { } (
      {
        state_dir = cfg.stateDir;
        host = cfg.listenAddress;
        port = cfg.port;
        cloud_hypervisor = "${cfg.cloudHypervisor}/bin/cloud-hypervisor";
        qemu_img = "${pkgs.qemu-utils}/bin/qemu-img";
        mkfs_ext4 = "${pkgs.e2fsprogs}/sbin/mkfs.ext4";
        mkisofs = "${pkgs.cdrtools}/bin/mkisofs";
        egress_enabled = cfg.egress.enable;
        egress_uplink = cfg.egress.uplink;
        ip_tool = "${pkgs.iproute2}/sbin/ip";
        nft_tool = "${pkgs.nftables}/sbin/nft";
      }
      // cfg.settings
    )
  );
in
{
  options.services.msksd = {
    enable = lib.mkEnableOption "msksd, the msks daemon (workspaces as cloud-hypervisor microvms)";

    package = lib.mkOption {
      type = lib.types.package;
      default = msksd;
      defaultText = "built from this flake against the host's nixpkgs";
      description = "The msksd package to run.";
    };

    cloudHypervisor = lib.mkOption {
      type = lib.types.package;
      default = pkgs.cloud-hypervisor;
      description = "The cloud-hypervisor the daemon drives for workspace microvms.";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = "The address the API's HTTPS listener binds.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8660;
      description = "The port the API's HTTPS listener binds.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/msksd";
      description = ''
        The daemon's state home: the SQLite catalog, TLS keys, the
        workspace volumes and seed disks. Must stay under /var/lib
        (the service unit owns the directory through StateDirectory).
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        An EnvironmentFile for the service unit, holding MSKSD_*
        variables the file-based config cannot (secrets above all:
        MSKSD_BOOTSTRAP_TOKEN seeds the first API credential).
        Environment overrides the generated settings file.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Open the API port in the host firewall.";
    };

    settings = lib.mkOption {
      type = lib.types.attrs;
      default = { };
      description = ''
        Extra msksd.yaml keys (docs/config.md), merged under the
        generated ones — the escape hatch for daemon settings without
        a first-class option.
      '';
    };

    egress = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          The egress-consent stack: per-VM taps, nftables/NFQUEUE
          enforcement, DHCP, DNS, and NAT, all daemon-managed with
          the service's ambient CAP_NET_ADMIN. Disabling it keeps
          workspaces vsock-only (the strongest default egress
          posture).
        '';
      };

      uplink = lib.mkOption {
        type = lib.types.str;
        default = "eth0";
        description = ''
          The host interface workspace egress masquerades out of —
          the nftables NAT rule's oifname. Set it to the host's
          default-route interface.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    # StateDirectory manages the state home; the assertion keeps the
    # option honest about where it can live.
    assertions = [
      {
        assertion = cfg.stateDir == "/var/lib/msksd";
        message = "services.msksd.stateDir must be /var/lib/msksd — the unit owns it through StateDirectory.";
      }
    ];

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
    boot.kernel.sysctl."net.ipv4.ip_forward" = lib.mkDefault 1;

    networking.firewall.allowedTCPPorts = lib.optionals cfg.openFirewall [
      cfg.port
    ];

    # The daemon (#101's privilege contract, verbatim from the
    # appliance): unprivileged user, kvm access through the group,
    # and the two ambient capabilities — ambient so the tools and the
    # VMM the daemon execs keep them.
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
        AmbientCapabilities = "CAP_NET_ADMIN CAP_NET_BIND_SERVICE";
        CapabilityBoundingSet = "CAP_NET_ADMIN CAP_NET_BIND_SERVICE";
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
