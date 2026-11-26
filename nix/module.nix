# The deployment-host module (#231, the #229 architecture): msksd
# and cloud-hypervisor run directly on the NixOS host, and workspace
# microvms run first-level on the host's /dev/kvm — one VM boundary,
# the same trust model the appliance's egress machinery always had
# inside its own kernel, now the host's.
#
# Source map: the retired nix/appliance-config.nix — the appliance's NixOS
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
#     the store through pkgs.formats.yaml (#801) — the format
#     services.msksd.server.settings is typed with, and the one the
#     host's client config (/etc/msks/msks.yaml, from
#     services.msksd.client.settings) is generated through;
#   - the egress prerequisites: the kernel modules the daemon's
#     rulesets and taps need, and host IP forwarding.
#
# What deliberately does not survive (this module's
# inventory, #232): the state-disk machinery, the store shapes and
# shares, the in-guest update channel, the sshd bridge listener, the
# nested-KVM module service, and the serial-console debug shell. The
# host's own systemd and /var carry what those units provided.
#
# Host requirement: an nixpkgs carrying python314 (the daemon's
# interpreter; the unstable pin does).
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.msksd;

  # The yaml format both generated files are written through
  # (#801): the type of services.msksd.server.settings and
  # services.msksd.client.settings, so a list, a mapping, a float,
  # and every scalar pass from Nix into the file under the yaml's
  # own key spelling.
  yamlFormat = pkgs.formats.yaml { };

  # The keys this module generates, each with the remedy its
  # refusal gives: the first-class option that owns the key (the
  # option owns it, and a settings-layer override would desync the
  # unit contract — state_dir from StateDirectory, port from the
  # firewall rule), or null for a tool path the module pins to the
  # host's packages, which no option overrides. Rejected in
  # services.msksd.server.settings.
  generatedKeys = {
    state_dir = "services.msksd.stateDir (fixed at /var/lib/msksd under the module)";
    host = "services.msksd.listenAddress, with services.msksd.tcp.enable";
    port = "services.msksd.port";
    socket = "services.msksd.socket.enable";
    socket_group = "services.msksd.socket.group";
    cloud_hypervisor = "services.msksd.cloudHypervisor";
    qemu_img = null;
    mkfs_ext4 = null;
    resize2fs = null;
    e2fsck = null;
    mkisofs = null;
    conntrack_tool = null;
    egress_enabled = "services.msksd.egress.enable";
    egress_uplink = "services.msksd.egress.uplink";
    ip_tool = null;
    nft_tool = null;
  };

  # A Nix path value anywhere in the client layer (#801): the yaml
  # type admits paths, and a path copies its file into the store —
  # world-readable, and the client file's paths (token_file,
  # identity_file) point at secrets. The layer has no legitimate
  # store-path content, so a path value anywhere under it refuses.
  hasPathValue =
    v:
    if builtins.isPath v then
      true
    else if builtins.isAttrs v then
      builtins.any hasPathValue (builtins.attrValues v)
    else if builtins.isList v then
      builtins.any hasPathValue v
    else
      false;

  # The daemon closure, built against the consuming host's nixpkgs
  # (the same wiring the appliance used): python 3.14 and the
  # pinned textual/litellm. The NFQUEUE binding is vendored ctypes
  # (#542): the wheel needs no compiled dependency, and the
  # module loads the platform's libnetfilter_queue at runtime
  # (the service's environment carries it). msksdPkgs carries the
  # certifi overlay only while the host's nixpkgs sits below the
  # pyproject floor (#606, #785): below it, the overlay resolves
  # the wheel nix/certifi-pkg.nix pins across the whole python
  # set, so every dependent (httpx, mitmproxy) builds against the
  # one certifi the wheel's >=2026.10.23 floor names; a host set
  # that already clears the floor keeps its own certifi and every
  # dependent comes from the binary cache. nix/certifi-gate.nix
  # names a composed set that misses the floor either way.
  msksdPkgs = import ./certifi-gate.nix (
    if (import ./certifi-floor.nix).metBy pkgs then
      pkgs
    else
      pkgs.extend (import ./certifi-overlay.nix)
  );
  msksd = msksdPkgs.python314.pkgs.callPackage ./msks-pkg.nix {
    textual = msksdPkgs.python314.pkgs.callPackage ./textual-pkg.nix { };
    litellm = msksdPkgs.python314.pkgs.callPackage ./litellm-pkg.nix { };
  };

  # The owner-exec helper (#1087, #1080 phase 2a), built from the
  # in-repo source against a musl static stdenv (a setuid binary
  # ships best with no interpreter and no libraries at exec), the
  # host's own virtiofsd pinned as the exec target — the binary
  # behind security.wrappers.msks-virtiofsd-as below.
  virtiofsdAs = pkgs.pkgsStatic.callPackage ./virtiofsd-as.nix {
    virtiofsd = pkgs.virtiofsd;
  };

  # The generated msksd.yaml (docs/config.md is the key reference):
  # the appliance's msks-boot file, minus the appliance paths, plus
  # the tools that file's PATH carried (the #184 resize pair, the
  # consent-revocation conntrack tool) — pinned to store paths so no
  # host package set is assumed. The operator's server.settings
  # layer merges over it: the format admits null (an unset layer),
  # and `//` takes a set alone.
  serverSettings =
    if cfg.server.settings == null then { } else cfg.server.settings;
  settingsFile = yamlFormat.generate "msksd.yaml" (
    {
      state_dir = cfg.stateDir;
      # #799: TCP is the module's default listener; the socket
      # joins when socket.enable names it (and tcp.enable off
      # leaves it alone — an empty host keeps TCP off, the
      # daemon's mirror of the socket setting's empty string).
      host = lib.optionalString cfg.tcp.enable cfg.listenAddress;
      port = cfg.port;
      socket = lib.optionalString cfg.socket.enable "/run/msks/msksd.sock";
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
    // (lib.optionalAttrs (cfg.socket.group != null) {
      socket_group = cfg.socket.group;
    })
    // serverSettings
  );

  # The host's client config (#801): the file a bare `msks` reads
  # once MSKSC_CONFIG_DIR points at /etc/msks (environment.etc and
  # environment.sessionVariables below). Keys are the client file's
  # own (docs/cli.md, the client config file): paths and addresses,
  # token_file among them — never a token. The format renders an
  # empty layer as an empty document, which the client reads as all
  # defaults.
  clientSettingsFile = yamlFormat.generate "msks.yaml" cfg.client.settings;

  # The capability set per enabled path: egress work keeps
  # CAP_NET_ADMIN (taps, per-VM nftables chains, NAT) and
  # CAP_NET_BIND_SERVICE (the egress stack binds DHCP 67 and DNS 53
  # — verified live: dropping it fails workspace start with EPERM);
  # a TCP port below 1024 adds CAP_NET_BIND_SERVICE without egress
  # (#799: only when the TCP listener serves — the socket never
  # binds one).
  capabilities =
    lib.optionals cfg.egress.enable [
      "CAP_NET_ADMIN"
      "CAP_NET_BIND_SERVICE"
    ]
    ++ lib.optional (cfg.tcp.enable && cfg.port < 1024) "CAP_NET_BIND_SERVICE";
in
{
  # The scalar-only settings option (v0.10.0 through v0.10.2)
  # renamed to server.settings (#801): the old spelling still
  # evaluates, with the rename warning, since every value the old
  # type took is a yaml value too.
  imports = [
    (lib.mkRenamedOptionModule
      [ "services" "msksd" "settings" ]
      [
        "services"
        "msksd"
        "server"
        "settings"
      ]
    )
  ];

  options.services.msksd = {
    enable = lib.mkEnableOption "msksd, the msks daemon (workspaces as cloud-hypervisor microvms)";

    package = lib.mkOption {
      type = lib.types.package;
      default = msksd;
      defaultText = lib.literalExpression "let p = import ./nix/certifi-gate.nix (if (import ./nix/certifi-floor.nix).metBy pkgs then pkgs else pkgs.extend (import ./nix/certifi-overlay.nix)); in p.python314.pkgs.callPackage ./nix/msks-pkg.nix { ... }";
      description = ''
        The msksd package to run. The default builds the one wheel
        against a python package set whose certifi clears the
        >=2026.10.23 floor — the set's own certifi while it already
        clears it, the wheel nix/certifi-pkg.nix pins while it does
        not (#606, #785) — and a package set of your own resolves
        certifi itself.
      '';
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
        The address the TCP listener binds while tcp.enable is
        set (the default). The TLS certificate's name covers
        exactly this address, and the daemon serves it on every
        interface when set to a wildcard — pair a wildcard with
        openFirewall and a bootstrap token.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8660;
      description = "The port the TCP listener binds while tcp.enable is set (a port below 1024 keeps CAP_NET_BIND_SERVICE even with egress off).";
    };

    socket = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          The local listener (#799): the daemon serves
          /run/msks/msksd.sock with peer-credential
          authentication — no TLS, no token — beside the TCP
          listener. Same-host clients in its group run the
          client with no configuration; turning tcp.enable off
          makes the socket the only listener.
        '';
      };

      group = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = ''
          The group the socket moves into at bind: its members
          connect with no token. The module creates the group and
          adds it to the unit's SupplementaryGroups (the daemon
          may only chgrp to a group it belongs to). Null keeps the
          daemon's own group — the msksd user alone connects.
        '';
      };
    };

    tcp = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          The TCP listener: HTTPS + WSS with TLS and bearer
          tokens, the shape a remote client needs. The default
          listener; listenAddress, port, and openFirewall apply
          while it serves, and turning it off (with the socket
          on) leaves the socket alone (#799).
        '';
      };
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
        EnvironmentFile holding MSKSD_* variables — secrets above
        all: the MSKSD_BOOTSTRAP_TOKEN seeding the first API
        credential — that the file-based config cannot hold.
        Environment overrides the generated settings file, and the
        unit does not start until the file exists — fail-closed for
        a secrets file.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the TCP listener's port in the host firewall (requires tcp.enable; only relevant once listenAddress is not loopback).";
    };

    server.settings = lib.mkOption {
      type = yamlFormat.type;
      default = { };
      description = ''
        The daemon's msksd.yaml keys the module does not generate
        (docs/config.md), written into the generated file verbatim
        under the file's own snake_case spelling: seed_images (a
        list of image tars), llm_models (a string, a list of
        strings, or a list of LiteLLM mappings), egress_dns_upstream,
        and every other documented key — lists and mappings pass as
        the yaml types them. A key the module generates, set here,
        fails evaluation naming the first-class option to use
        instead. The generated file lands in the store
        world-readable: pass secrets through environmentFile, never
        through server.settings, and spell file paths as strings — a
        path value copies its file into the store.
      '';
    };

    client.settings = lib.mkOption {
      type = yamlFormat.type;
      default = { };
      description = ''
        The host's client config, generated as /etc/msks/msks.yaml —
        the file a bare msks reads (docs/cli.md, the client config
        file), under the file's own spelling: every key the file
        takes, url, token_file, cafile, socket, identity_file,
        pubkey_file, daemons and active_daemon, ssh_options among
        them. The file applies to every user of the host: a
        cache_dir or state_dir in it gives every user one state
        root. The module points every user's MSKSC_CONFIG_DIR at
        /etc/msks as a default, so a bare msks on the host reads
        this file; a user with a config of their own exports
        MSKSC_CONFIG_DIR elsewhere. The file holds paths and
        addresses, never a secret: token_file and identity_file
        name the files that hold them. Spell file paths as strings:
        a path value copies its file into the world-readable store,
        and the module refuses a path value in this layer. The
        client refuses an unknown key at load, naming the key and
        the valid ones.
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
          Disabling it keeps workspaces without a NIC (the strongest
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
    # Enabled means installed (#796): the package's console
    # scripts (msks, msksd, msks-term-popup, the msks-setup-*
    # tools) go on the system PATH for every user — the same
    # derivation the unit below execs, so the system closure
    # gains nothing beyond the daemon it already carries. The
    # gate keeps import-alone inert; checks.module pins both
    # branches.
    environment.systemPackages = [ cfg.package ];

    # The host's client config (#801): /etc/msks/msks.yaml, the file
    # a bare `msks` reads once MSKSC_CONFIG_DIR points at /etc/msks
    # (sessionVariables reaches every login session; a user's own
    # export in their shell wins over it). Generated even with an
    # empty client.settings: the client writes a first-run template
    # when the file is missing, /etc/msks is root's to write, and an
    # absent file would make every bare `msks` on the host refuse.
    # World-readable by design: the client file holds paths and
    # addresses (token_file is a path), and the client's closed key
    # list refuses anything else.
    environment.etc."msks/msks.yaml".source = clientSettingsFile;
    environment.sessionVariables.MSKSC_CONFIG_DIR = lib.mkDefault "/etc/msks";

    assertions = [
      {
        assertion = cfg.stateDir == "/var/lib/msksd";
        message = "services.msksd.stateDir must be /var/lib/msksd — the unit owns it through StateDirectory.";
      }
      {
        assertion = cfg.egress.enable -> cfg.egress.uplink != null;
        message = "services.msksd.egress.uplink is required when egress is enabled — set the host's default-route interface (ip route show default).";
      }
      {
        assertion = cfg.socket.enable || cfg.tcp.enable;
        message = "services.msksd serves nothing — enable the socket listener (services.msksd.socket.enable, the default) or the TCP listener (services.msksd.tcp.enable).";
      }
      {
        assertion = cfg.openFirewall -> cfg.tcp.enable;
        message = "services.msksd.openFirewall needs services.msksd.tcp.enable — the module's default listener is the socket, and the firewall rule belongs to the TCP listener.";
      }
      {
        assertion = (cfg.socket.group != null) -> cfg.socket.enable;
        message = "services.msksd.socket.group needs services.msksd.socket.enable — the share rides the socket listener.";
      }
      {
        assertion = cfg.socket.group != "msksd";
        message = "services.msksd.socket.group must differ from the daemon's own group — share a group of operators instead (the daemon user always connects).";
      }
    ]
    ++ lib.mapAttrsToList (key: remedy: {
      assertion = !builtins.hasAttr key serverSettings;
      message =
        "services.msksd.server.settings.${key} is generated by the module — "
        + (
          if remedy != null then
            "set ${remedy} instead."
          else
            "the module pins this tool path to the host's packages, and no option overrides it."
        );
    }) generatedKeys
    ++ [
      {
        assertion = !hasPathValue cfg.client.settings;
        message = "services.msksd.client.settings carries a Nix path value — spell file paths as strings: a path value copies its file into the world-readable store (token_file and identity_file point at secrets).";
      }
    ];

    # The service identity (#101, verbatim from the appliance): a
    # dedicated system user; /dev/kvm arrives through the kvm
    # supplementary group (NixOS's udev rule assigns the node to the
    # group). #799: the socket's shared group, when one is named,
    # exists and rides the unit's SupplementaryGroups too — the
    # daemon may only chgrp its socket to a group it belongs to.
    users.groups = {
      msksd = { };
    }
    // lib.optionalAttrs (cfg.socket.group != null) {
      ${cfg.socket.group} = { };
    };
    users.users.msksd = {
      isSystemUser = true;
      group = "msksd";
      home = cfg.stateDir;
      shell = "/run/current-system/sw/bin/nologin";
    };

    # The owner-exec helper's wrapper (#1087, #1080 phase 2a):
    # setuid-root, executable by the daemon's group alone — the
    # privilege boundary the daemon execs a share's virtiofsd
    # through as the workspace's owner uid (the daemon-side wiring
    # is #1088). Built from the in-repo source, so no generation
    # ships a helper that drifted from the source.
    security.wrappers.msks-virtiofsd-as = {
      source = "${virtiofsdAs}/bin/virtiofsd-as";
      owner = "root";
      group = "msksd";
      setuid = true;
      permissions = "u=rwx,g=rx";
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
      # The vendored NFQUEUE binding (#542) dlopens the platform's
      # queue library by soname; the store paths carry it into the
      # service (isolated store dirs, nothing shadowed).
      environment.LD_LIBRARY_PATH = lib.mkDefault "${pkgs.libnetfilter_queue}/lib:${pkgs.libnfnetlink}/lib";
      unitConfig.StartLimitIntervalSec = 0;
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/msksd --config ${settingsFile}";
        User = "msksd";
        Group = "msksd";
        SupplementaryGroups = lib.concatStringsSep " " (
          [ "kvm" ] ++ lib.optional (cfg.socket.group != null) cfg.socket.group
        );
        AmbientCapabilities = lib.concatStringsSep " " capabilities;
        CapabilityBoundingSet = lib.concatStringsSep " " capabilities;
        StateDirectory = "msksd";
        # The socket's home (#799): the unit's runtime directory,
        # /run/msks, owned by the daemon user — the socket binds
        # inside it.
        RuntimeDirectory = lib.mkIf cfg.socket.enable "msks";
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
