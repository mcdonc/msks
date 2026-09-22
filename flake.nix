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
        overlays = [ ];
      };
      msksd = pkgs.python314.pkgs.callPackage ./nix/msks-pkg.nix {
        textual = pkgs.python314.pkgs.callPackage ./nix/textual-pkg.nix { };
        netfilterqueue =
          pkgs.python314.pkgs.callPackage ./nix/netfilterqueue-pkg.nix
            { };
      };

      # Eval-only guards (#231): both egress branches of the module
      # evaluate, the unit exists, and the capability grant follows
      # the branch. Evaluated at flake-eval time, asserted in the
      # check's build.
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
      hasTun = c: builtins.any (m: m == "tun") c.boot.kernelModules;
    in
    {
      packages.${system} = {
        msksd = msksd;
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
            case "$capsOff" in
              *CAP_NET_ADMIN*|*CAP_NET_BIND_SERVICE*)
                echo "fail: vsock-only branch kept an egress capability"; exit 1 ;;
              *) ;;
            esac
            test "$tunOn" = "true" || { echo "fail: egress branch missing the tun module"; exit 1; }
            test "$tunOff" = "false" || { echo "fail: vsock-only branch loads the egress modules"; exit 1; }
            test "$fwdOn" = "1" || { echo "fail: egress branch lost ip_forward"; exit 1; }
            touch "$out"
          '';
    };
}
