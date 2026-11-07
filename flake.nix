{
  description = "msks: workspaces as microvms — the msksd package and its NixOS module";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/21a67dc470149f337cecafbe965d8d252a390518";

  outputs =
    {
      self,
      nixpkgs,
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config = { };
        # certifi above the pinned channel's (#606): nix/certifi-overlay.nix
        # replaces it across the python package set so every dependent in
        # the msksd closure resolves the one wheel the pyproject floor names.
        overlays = [ (import ./nix/certifi-overlay.nix) ];
      };
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

      # Eval-only guards (#231): both egress branches of the module
      # evaluate, the unit exists, and the capability grant follows
      # the branch. #796 adds the PATH semantics: enabling puts the
      # daemon package on the system PATH, and the disabled module
      # contributes nothing to it. Evaluated at flake-eval time,
      # asserted in the check's build.
      evalConfig =
        msksd-config:
        (import (pkgs.path + "/nixos") {
          system = system;
          configuration =
            { ... }:
            {
              imports = [ ./nix/module.nix ];
              services.msksd = msksd-config;
              fileSystems."/".device = "nodev";
              fileSystems."/".fsType = "ext4";
              boot.loader.grub.device = "nodev";
            };
        }).config;
      on = evalConfig {
        enable = true;
        egress.uplink = "eno4";
      };
      off = evalConfig {
        enable = true;
        egress = {
          enable = false;
          uplink = null;
        };
      };
      inert = evalConfig { enable = false; };
      # #799: the TCP-enabled shape — socket and TCP both serve
      # (remote clients need the TLS+token surface).
      tcp = evalConfig {
        enable = true;
        tcp.enable = true;
        egress.uplink = "eno4";
      };
      grouped = evalConfig {
        enable = true;
        socket.group = "msksops";
        egress = {
          enable = false;
          uplink = null;
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
            execTcp = tcp.systemd.services.msksd.serviceConfig.ExecStart;
            execGrouped = grouped.systemd.services.msksd.serviceConfig.ExecStart;
            runtimeDir = on.systemd.services.msksd.serviceConfig.RuntimeDirectory or "";
            groupExists = pkgs.lib.boolToString (grouped.users.groups ? msksops);
            suppGroups = grouped.systemd.services.msksd.serviceConfig.SupplementaryGroups;
            inherit pkgOn pathOn pathInert;
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
            # #799: the default branch serves the socket alone —
            # the yaml names the socket path and an empty host —
            # and the unit owns /run/msksd for it; the tcp branch
            # carries the address.
            cfg_on=''${execOn##* --config }
            grep -qF '"socket":"/run/msksd/msksd.sock"' "$cfg_on" || {
              echo "fail: default branch lost the socket path in msksd.yaml"
              exit 1
            }
            grep -qF '"host":""' "$cfg_on" || {
              echo "fail: default branch serves TCP — host must be empty in msksd.yaml"
              exit 1
            }
            test "$runtimeDir" = "msksd" || {
              echo "fail: the unit lost RuntimeDirectory=msksd for the socket"
              exit 1
            }
            cfg_tcp=''${execTcp##* --config }
            grep -qF '"host":"127.0.0.1"' "$cfg_tcp" || {
              echo "fail: tcp branch lost the listen address in msksd.yaml"
              exit 1
            }
            grep -qF '"socket":"/run/msksd/msksd.sock"' "$cfg_tcp" || {
              echo "fail: tcp branch lost the socket path in msksd.yaml"
              exit 1
            }
            cfg_grouped=''${execGrouped##* --config }
            test "$groupExists" = "true" || {
              echo "fail: socket.group never created its group"
              exit 1
            }
            case " $suppGroups " in
              *" msksops "*) ;;
              *) echo "fail: the unit lost the socket group in SupplementaryGroups"; exit 1 ;;
            esac
            grep -qF '"socket_group":"msksops"' "$cfg_grouped" || {
              echo "fail: grouped branch lost socket_group in msksd.yaml"
              exit 1
            }
            touch "$out"
          '';
    };
}
