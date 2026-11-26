{
  description = "msks: workspaces as microvms — the msksd package and its NixOS module";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/e21fec321d73511849707a7585e4ea155ee4bf40";

  outputs =
    {
      self,
      nixpkgs,
    }:
    let
      system = "x86_64-linux";
      floor = import ./nix/certifi-floor.nix;
      basePkgs = import nixpkgs {
        inherit system;
        config = { };
      };
      # certifi above the pinned nixpkgs while the pin sits below
      # the pyproject floor (#606): the overlay applies only while
      # the pinned set needs it (#785) — a set that already clears
      # the floor keeps its own certifi, and the python closure
      # comes from the binary cache — and nix/certifi-gate.nix
      # names a composed set that misses the floor at eval time.
      pkgs = import ./nix/certifi-gate.nix (
        if floor.metBy basePkgs then
          basePkgs
        else
          import nixpkgs {
            inherit system;
            config = { };
            overlays = [ (import ./nix/certifi-overlay.nix) ];
          }
      );
      msksd = pkgs.python314.pkgs.callPackage ./nix/msks-pkg.nix {
        textual = pkgs.python314.pkgs.callPackage ./nix/textual-pkg.nix { };
        litellm = pkgs.python314.pkgs.callPackage ./nix/litellm-pkg.nix { };
      };

      # The release artifacts (#537), the flake half of the devenv
      # outputs split: the same wheel builds, the same image
      # archives (Debian and NixOS flavors) — consumable with plain
      # nix (`nix build .#server`, `.#guest-image-debian`,
      # `.#guest-image-nixos`), no devenv in the environment. The
      # flake and devenv.lock pin the same nixpkgs revision, so each
      # attribute evaluates to the same derivation its
      # `devenv build outputs.<name>` twin builds.
      #
      # One closure for everything (#793): the wheel installs every
      # console script, so the server output carries the client CLI
      # in the daemon closure. There is no client-only build: two
      # buildPythonPackage derivations over the same wheel cannot
      # coexist in one environment (both would be pname "msks"),
      # and the lean install had no users — macOS installs the
      # wheel, a NixOS module host runs the daemon anyway.
      server = pkgs.python314.withPackages (_: [ msksd ]);
      guestImages = import ./nix/guest.nix { inherit pkgs; };
      guest-image-debian = guestImages.image-archive-debian;
      guest-image-nixos = guestImages.image-archive-nixos;

      # The owner-exec helper's module-shaped build (#1087, #1080
      # phase 2a): the same expression nix/module.nix builds into
      # security.wrappers.msks-virtiofsd-as — static musl, the
      # host's virtiofsd pinned as the exec target. Exposed for
      # the nix workflow's build gate. The release assets do not
      # build here: release.yml compiles them with each runner's
      # native gcc -static on the two runner architectures.
      virtiofsd-as = pkgs.pkgsStatic.callPackage ./nix/virtiofsd-as.nix {
        virtiofsd = pkgs.virtiofsd;
      };

      # Eval-only guards (#231): both egress branches of the module
      # evaluate, the unit exists, and the capability grant follows
      # the branch. #796 adds the PATH semantics: enabling puts the
      # daemon package on the system PATH, and the disabled module
      # contributes nothing to it. #801 adds the client half of the
      # module (programs.msks): a host that runs the client alone,
      # and the listener a daemon host feeds its own client. The
      # harness takes the whole module configuration, both
      # namespaces. Evaluated at flake-eval time, asserted in the
      # check's build.
      evalConfig =
        module-config:
        (import (pkgs.path + "/nixos") {
          system = system;
          configuration =
            { ... }:
            {
              imports = [ ./nix/module.nix ];
              fileSystems."/".device = "nodev";
              fileSystems."/".fsType = "ext4";
              boot.loader.grub.device = "nodev";
            }
            // module-config;
        }).config;
      daemon = {
        enable = true;
        egress.uplink = "eno4";
      };
      on = evalConfig { services.msksd = daemon; };
      off = evalConfig {
        services.msksd = {
          enable = true;
          egress = {
            enable = false;
            uplink = null;
          };
        };
      };
      inert = evalConfig { services.msksd.enable = false; };
      # #799: the socketed shape — socket alone (tcp off) with
      # the shared group, and the both-on shape beside it.
      socketed = evalConfig {
        services.msksd = {
          enable = true;
          socket = {
            enable = true;
            group = "msksops";
          };
          tcp.enable = false;
          egress = {
            enable = false;
            uplink = null;
          };
        };
      };
      both = evalConfig {
        services.msksd = {
          enable = true;
          socket.enable = true;
          tcp.enable = true;
          egress.uplink = "eno4";
        };
      };
      # #801: the settings layers — a server.settings list and a
      # list of mappings (the two shapes the scalar-only type
      # refused), an int, a float, and a bool beside them, and the
      # client layer: the operator's keys over the listener the
      # daemon host feeds in.
      layered = evalConfig {
        services.msksd = daemon // {
          server.settings = {
            seed_images = [ "/var/lib/msksd/seeds/debian-trixie.tar" ];
            llm_models = [
              {
                model_name = "gpt-4o";
                litellm_params.model = "openai/gpt-4o";
              }
            ];
            storage_warn_pct = 85;
            egress_dns_timeout_s = 2.5;
            nested = true;
          };
        };
        programs.msks.settings = {
          token_file = "/etc/msks/token";
          daemons.lab.url = "https://hv-1.lab.example.com:8660";
        };
      };
      # #801: a host that runs the client alone — programs.msks, no
      # daemon: the client on PATH, its file and variable in place,
      # and no unit.
      clientOnly = evalConfig {
        programs.msks = {
          enable = true;
          settings = {
            url = "https://hv-1.lab.example.com:8660";
            cafile = "/etc/msks/hv-1-ca.pem";
            token_file = "/etc/msks/token";
          };
        };
      };
      # #801: the client layer opted out under a daemon — the unit
      # serves, and the client file, the variable, and the PATH
      # entry go.
      optedOut = evalConfig {
        services.msksd = daemon;
        programs.msks.enable = false;
      };
      # The renamed option (#801): services.msksd.settings, the
      # v0.10.x spelling, still flows into server.settings — the
      # file carries the key, and the eval records the rename
      # warning (config.warnings; nixos-rebuild prints it).
      renamed = evalConfig {
        services.msksd = daemon // {
          settings.storage_warn_pct = 90;
        };
      };
      renameWarned = pkgs.lib.any (
        w: pkgs.lib.hasInfix "has been renamed to" w
      ) renamed.warnings;
      # The two refusals (#801) as negative evals: a false assertion
      # is a value here, not a throw, so the check reads the
      # assertion lists directly — a module-generated key under the
      # old name, and a path value in the client layer — and forces
      # each refused message, so a message that throws fails here.
      refused = pkgs.lib.any (a: !a.assertion && builtins.isString a.message) (
        (evalConfig {
          services.msksd = daemon // {
            settings.port = 1;
          };
        }).assertions
      );
      pathRefused = pkgs.lib.any (a: !a.assertion && builtins.isString a.message) (
        (evalConfig {
          programs.msks = {
            enable = true;
            settings.token_file = ./flake.nix;
          };
        }).assertions
      );
      # A null layer (#801): the yaml type admits null, and the
      # module reads it as an unset layer — the generated keys stay,
      # and the client file still renders. (A null programs.msks
      # settings under a daemon is the type's own refusal: the
      # listener defaults are definitions of the same option.)
      nulled = evalConfig {
        services.msksd = daemon // {
          server.settings = null;
        };
      };
      clientNulled = evalConfig {
        programs.msks = {
          enable = true;
          settings = null;
        };
      };
      hasTun = c: builtins.any (m: m == "tun") c.boot.kernelModules;
      # Package names compared as strings in the check's build:
      # the enabled eval's PATH must carry the exact package the
      # unit execs. The inert eval's PATH is not empty — a minimal
      # NixOS config carries its own defaults (systemd, sudo,
      # nixos-rebuild) — and inertness means the module adds no
      # msks derivation to it, so the assert matches the wheel's
      # package name instead of the whole list. `.name`, not
      # toString: a name carries no context, so the check's build
      # inputs stay the handful the ExecStart strings already
      # pulled in.
      pkgOn = on.services.msksd.package.name;
      pathOn = pkgs.lib.concatStringsSep " " (
        map (p: p.name) on.environment.systemPackages
      );
      pathInert = pkgs.lib.concatStringsSep " " (
        map (p: p.name) inert.environment.systemPackages
      );
    in
    {
      packages.${system} = {
        msksd = msksd;
        inherit
          server
          guest-image-debian
          guest-image-nixos
          virtiofsd-as
          ;
        default = msksd;
      };

      nixosModules.msks = import ./nix/module.nix;
      nixosModules.default = self.nixosModules.msks;

      checks.${system}.module =
        pkgs.runCommand "msks-module-eval-check"
          {
            execOn = on.systemd.services.msksd.serviceConfig.ExecStart;
            execOff = off.systemd.services.msksd.serviceConfig.ExecStart;
            capsOn = on.systemd.services.msksd.serviceConfig.AmbientCapabilities;
            capsOff = off.systemd.services.msksd.serviceConfig.AmbientCapabilities;
            tunOn = pkgs.lib.boolToString (hasTun on);
            tunOff = pkgs.lib.boolToString (hasTun off);
            fwdOn = on.boot.kernel.sysctl."net.ipv4.ip_forward" or null;
            # #799: the generated settings file, read from the
            # store — the yaml the ExecStart names — pins the
            # listener wiring per branch.
            execSocketed = socketed.systemd.services.msksd.serviceConfig.ExecStart;
            execBoth = both.systemd.services.msksd.serviceConfig.ExecStart;
            execLayered = layered.systemd.services.msksd.serviceConfig.ExecStart;
            # #801: the host's client config — the enabled branch's
            # file (empty client.settings) and the layered branch's,
            # both read from the store through environment.etc, and
            # the session variable pointing every user's msks at it.
            clientCfgOn = toString on.environment.etc."msks/msks.yaml".source;
            clientCfgLayered = toString layered.environment.etc."msks/msks.yaml".source;
            clientCfgNulled = toString nulled.environment.etc."msks/msks.yaml".source;
            cfgDir = on.environment.sessionVariables.MSKSC_CONFIG_DIR or "";
            # The inert branch contributes neither the file nor the
            # variable (both `or ""`: an absent entry must stay empty).
            clientCfgInert = toString (inert.environment.etc."msks/msks.yaml".source or "");
            cfgDirInert = inert.environment.sessionVariables.MSKSC_CONFIG_DIR or "";
            execRenamed = renamed.systemd.services.msksd.serviceConfig.ExecStart;
            execNulled = nulled.systemd.services.msksd.serviceConfig.ExecStart;
            renameWarned = pkgs.lib.boolToString renameWarned;
            oldNameRefused = pkgs.lib.boolToString refused;
            pathRefused = pkgs.lib.boolToString pathRefused;
            # #801: the listener a daemon host feeds its client, per
            # branch; the client-only branch (its own file, the
            # package on PATH, no unit); and the opt-out under a
            # daemon (no file, no variable, no PATH entry).
            clientCfgSocketed = toString socketed.environment.etc."msks/msks.yaml".source;
            clientCfgClientOnly =
              toString
                clientOnly.environment.etc."msks/msks.yaml".source;
            clientCfgClientNulled =
              toString
                clientNulled.environment.etc."msks/msks.yaml".source;
            cfgDirClientOnly =
              clientOnly.environment.sessionVariables.MSKSC_CONFIG_DIR or "";
            execClientOnly =
              clientOnly.systemd.services.msksd.serviceConfig.ExecStart or "";
            pathClientOnly = pkgs.lib.concatStringsSep " " (
              map (p: p.name) clientOnly.environment.systemPackages
            );
            execOptedOut = optedOut.systemd.services.msksd.serviceConfig.ExecStart;
            clientCfgOptedOut = toString (
              optedOut.environment.etc."msks/msks.yaml".source or ""
            );
            cfgDirOptedOut = optedOut.environment.sessionVariables.MSKSC_CONFIG_DIR or "";
            pathOptedOut = pkgs.lib.concatStringsSep " " (
              map (p: p.name) optedOut.environment.systemPackages
            );
            runtimeDir = on.systemd.services.msksd.serviceConfig.RuntimeDirectory or "";
            socketRuntimeDir =
              socketed.systemd.services.msksd.serviceConfig.RuntimeDirectory or "";
            groupExists = pkgs.lib.boolToString (socketed.users.groups ? msksops);
            suppGroups = socketed.systemd.services.msksd.serviceConfig.SupplementaryGroups;
            inherit pkgOn pathOn pathInert;
            # The owner-exec wrapper (#1087): setuid-root and
            # root:msksd, the exact privilege contract the daemon's
            # owner-tracked shares exec through.
            wrapperSetuid = pkgs.lib.boolToString on.security.wrappers.msks-virtiofsd-as.setuid;
            wrapperGroup = on.security.wrappers.msks-virtiofsd-as.group;
            wrapperSource = toString on.security.wrappers.msks-virtiofsd-as.source;
          }
          ''
            test -n "$execOn" && test -n "$execOff" || {
              echo "fail: msksd unit missing in an egress branch";
              exit 1;
            }
            case "$capsOn" in
              *CAP_NET_ADMIN*) ;;
              *) echo "fail: egress branch lost CAP_NET_ADMIN"; exit 1 ;;
            esac
            case "$capsOn" in
              *CAP_NET_BIND_SERVICE*) ;;
              *)
                echo "fail: egress branch lost CAP_NET_BIND_SERVICE (the stack binds DHCP 67 and DNS 53)"
                exit 1
                ;;
            esac
            case "$capsOff" in
              *CAP_NET_ADMIN*|*CAP_NET_BIND_SERVICE*)
                echo "fail: vsock-only branch kept an egress capability"; exit 1 ;;
              *) ;;
            esac
            test "$tunOn" = "true" || { echo "fail: egress branch missing the tun module"; exit 1; }
            test "$tunOff" = "false" || { echo "fail: vsock-only branch loads the egress modules"; exit 1; }
            test "$fwdOn" = "1" || { echo "fail: egress branch lost ip_forward"; exit 1; }
            test "$wrapperSetuid" = "true" || {
              echo "fail: msks-virtiofsd-as wrapper is not setuid"; exit 1; }
            test "$wrapperGroup" = "msksd" || {
              echo "fail: msks-virtiofsd-as wrapper is not group msksd"; exit 1; }
            test -n "$wrapperSource" || {
              echo "fail: msks-virtiofsd-as wrapper has no source"; exit 1; }
            case " $pathOn " in
              *" $pkgOn "*) ;;
              *) echo "fail: enabled branch lost the msksd package on the system PATH"; exit 1 ;;
            esac
            case " $pathInert " in
              *-msks-*)
                echo "fail: inert branch put the msks package on the system PATH"
                exit 1
                ;;
            esac
            # #799: the default branch serves TCP alone — the
            # yaml carries the address and the socket's empty
            # disable spelling, and the unit claims no
            # RuntimeDirectory; the socketed branch flips both
            # (the socket path, the empty host, the share, and
            # /run/msks), and the both branch carries the two
            # listeners together. The file is pkgs.formats.yaml's
            # block style (#801): one `key: value` line per key (a
            # long value continues on an indented line), an empty
            # string as two single quotes — spelled ''' here, the
            # escape a Nix indented string needs for them.
            cfg_on=''${execOn##* --config }
            grep -qxF 'host: 127.0.0.1' "$cfg_on" || {
              echo "fail: default branch lost the listen address in msksd.yaml"
              exit 1
            }
            grep -qxF "socket: '''" "$cfg_on" || {
              echo "fail: default branch bound the socket — socket must be empty in msksd.yaml"
              exit 1
            }
            test "$runtimeDir" = "" || {
              echo "fail: default branch claimed RuntimeDirectory without the socket"
              exit 1
            }
            cfg_socketed=''${execSocketed##* --config }
            grep -qxF "host: '''" "$cfg_socketed" || {
              echo "fail: socketed branch serves TCP — host must be empty in msksd.yaml"
              exit 1
            }
            grep -qxF 'socket: /run/msks/msksd.sock' "$cfg_socketed" || {
              echo "fail: socketed branch lost the socket path in msksd.yaml"
              exit 1
            }
            test "$socketRuntimeDir" = "msks" || {
              echo "fail: socketed branch lost RuntimeDirectory=msks for the socket"
              exit 1
            }
            test "$groupExists" = "true" || {
              echo "fail: socket.group never created its group"
              exit 1
            }
            case " $suppGroups " in
              *" msksops "*) ;;
              *) echo "fail: the unit lost the socket group in SupplementaryGroups"; exit 1 ;;
            esac
            grep -qxF 'socket_group: msksops' "$cfg_socketed" || {
              echo "fail: socketed branch lost socket_group in msksd.yaml"
              exit 1
            }
            cfg_both=''${execBoth##* --config }
            grep -qxF 'host: 127.0.0.1' "$cfg_both" || {
              echo "fail: both branch lost the listen address in msksd.yaml"
              exit 1
            }
            grep -qxF 'socket: /run/msks/msksd.sock' "$cfg_both" || {
              echo "fail: both branch lost the socket path in msksd.yaml"
              exit 1
            }
            # #801: the yaml-typed layers. The server file carries
            # the list, the list of mappings, the int, the float,
            # and the bool, under the yaml's own spellings, beside
            # the module's generated keys. The client file carries
            # its keys through environment.etc in both the empty and
            # the layered branch, and the session variable points at
            # /etc/msks.
            cfg_layered=''${execLayered##* --config }
            grep -qxF 'seed_images:' "$cfg_layered" || {
              echo "fail: server.settings.seed_images never reached msksd.yaml"
              exit 1
            }
            grep -qxF -- '- /var/lib/msksd/seeds/debian-trixie.tar' "$cfg_layered" || {
              echo "fail: seed_images lost its list entry in msksd.yaml"
              exit 1
            }
            grep -qxF 'llm_models:' "$cfg_layered" || {
              echo "fail: server.settings.llm_models never reached msksd.yaml"
              exit 1
            }
            grep -qxF -- '- litellm_params:' "$cfg_layered" || {
              echo "fail: llm_models lost its mapping entries in msksd.yaml"
              exit 1
            }
            grep -qxF '    model: openai/gpt-4o' "$cfg_layered" || {
              echo "fail: llm_models lost its nested mapping in msksd.yaml"
              exit 1
            }
            grep -qxF 'storage_warn_pct: 85' "$cfg_layered" || {
              echo "fail: server.settings int never reached msksd.yaml"
              exit 1
            }
            grep -qxF 'egress_dns_timeout_s: 2.5' "$cfg_layered" || {
              echo "fail: server.settings float never reached msksd.yaml"
              exit 1
            }
            grep -qxF 'nested: true' "$cfg_layered" || {
              echo "fail: server.settings bool never reached msksd.yaml"
              exit 1
            }
            grep -qxF 'host: 127.0.0.1' "$cfg_layered" || {
              echo "fail: server.settings dropped a module-generated key from msksd.yaml"
              exit 1
            }
            # #801: the client layer under the daemon — the listener
            # fed in as defaults (url and cafile under TCP, the
            # socket alone under the socketed branch), the operator's
            # keys merging over them (the layered branch keeps the
            # default url and cafile beside its own token_file and
            # alias), and the variable pointing at /etc/msks.
            test -s "$clientCfgOn" || {
              echo "fail: the enabled branch renders no /etc/msks/msks.yaml"
              exit 1
            }
            grep -qxF 'url: https://127.0.0.1:8660' "$clientCfgOn" || {
              echo "fail: the daemon's TCP listener never reached /etc/msks/msks.yaml as the url default"
              exit 1
            }
            grep -qxF 'cafile: /var/lib/msksd/msks-ca.pem' "$clientCfgOn" || {
              echo "fail: the daemon's CA never reached /etc/msks/msks.yaml as the cafile default"
              exit 1
            }
            grep -qxF 'socket: /run/msks/msksd.sock' "$clientCfgSocketed" || {
              echo "fail: the socketed branch's client file lost the socket path"
              exit 1
            }
            if grep -qxE 'url: .*' "$clientCfgSocketed"; then
              echo "fail: the socketed branch's client file dials TCP"
              exit 1
            fi
            test -s "$clientCfgLayered" || {
              echo "fail: programs.msks.settings rendered no /etc/msks/msks.yaml"
              exit 1
            }
            grep -qxF 'url: https://127.0.0.1:8660' "$clientCfgLayered" || {
              echo "fail: the operator's programs.msks.settings dropped the listener's url default"
              exit 1
            }
            grep -qxF 'cafile: /var/lib/msksd/msks-ca.pem' "$clientCfgLayered" || {
              echo "fail: the operator's programs.msks.settings dropped the listener's cafile default"
              exit 1
            }
            grep -qxF 'token_file: /etc/msks/token' "$clientCfgLayered" || {
              echo "fail: programs.msks.settings.token_file never reached /etc/msks/msks.yaml"
              exit 1
            }
            grep -qxF '    url: https://hv-1.lab.example.com:8660' "$clientCfgLayered" || {
              echo "fail: programs.msks.settings.daemons never reached /etc/msks/msks.yaml"
              exit 1
            }
            test "$cfgDir" = "/etc/msks" || {
              echo "fail: the enabled branch lost MSKSC_CONFIG_DIR in the session variables"
              exit 1
            }
            test -s "$clientCfgClientNulled" || {
              echo "fail: a null programs.msks.settings rendered no /etc/msks/msks.yaml"
              exit 1
            }
            # A host that runs the client alone: the package on
            # PATH, its own keys in the file, the variable, and no
            # daemon unit.
            test -z "$execClientOnly" || {
              echo "fail: the client-only branch grew an msksd unit"
              exit 1
            }
            case " $pathClientOnly " in
              *" $pkgOn "*) ;;
              *)
                echo "fail: the client-only branch lost the msks package on the system PATH"
                exit 1
                ;;
            esac
            grep -qxF 'url: https://hv-1.lab.example.com:8660' "$clientCfgClientOnly" || {
              echo "fail: the client-only branch's url never reached /etc/msks/msks.yaml"
              exit 1
            }
            grep -qxF 'token_file: /etc/msks/token' "$clientCfgClientOnly" || {
              echo "fail: the client-only branch's token_file never reached /etc/msks/msks.yaml"
              exit 1
            }
            test "$cfgDirClientOnly" = "/etc/msks" || {
              echo "fail: the client-only branch lost MSKSC_CONFIG_DIR in the session variables"
              exit 1
            }
            # programs.msks.enable = false under a daemon: the unit
            # serves, and the client file, the variable, and the
            # PATH entry are gone.
            test -n "$execOptedOut" || {
              echo "fail: the opted-out branch lost the msksd unit"
              exit 1
            }
            test -z "$clientCfgOptedOut" || {
              echo "fail: the opted-out branch contributed /etc/msks/msks.yaml"
              exit 1
            }
            test -z "$cfgDirOptedOut" || {
              echo "fail: the opted-out branch contributed MSKSC_CONFIG_DIR"
              exit 1
            }
            case " $pathOptedOut " in
              *-msks-*)
                echo "fail: the opted-out branch put the msks package on the system PATH"
                exit 1
                ;;
            esac
            # The inert branch (nothing enabled) contributes neither
            # the file nor the variable.
            test -z "$clientCfgInert" || {
              echo "fail: the inert branch contributed /etc/msks/msks.yaml"
              exit 1
            }
            test -z "$cfgDirInert" || {
              echo "fail: the inert branch contributed MSKSC_CONFIG_DIR"
              exit 1
            }
            # The renamed option still flows into the server file,
            # with the warning, and its generated-key refusal holds
            # under the old name; a path value in the client layer
            # refuses.
            cfg_renamed=''${execRenamed##* --config }
            grep -qxF 'storage_warn_pct: 90' "$cfg_renamed" || {
              echo "fail: the renamed services.msksd.settings never reached msksd.yaml"
              exit 1
            }
            test "$renameWarned" = "true" || {
              echo "fail: the renamed services.msksd.settings raised no rename warning"
              exit 1
            }
            test "$oldNameRefused" = "true" || {
              echo "fail: a module-generated key under the renamed services.msksd.settings was not refused"
              exit 1
            }
            test "$pathRefused" = "true" || {
              echo "fail: a Nix path value in client.settings was not refused"
              exit 1
            }
            # A null layer leaves the generated keys and the client
            # file in place.
            cfg_nulled=''${execNulled##* --config }
            grep -qxF 'host: 127.0.0.1' "$cfg_nulled" || {
              echo "fail: a null server.settings dropped a generated key from msksd.yaml"
              exit 1
            }
            test -s "$clientCfgNulled" || {
              echo "fail: a null client.settings rendered no /etc/msks/msks.yaml"
              exit 1
            }
            touch "$out"
          '';
    };
}
