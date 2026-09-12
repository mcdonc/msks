# k3s development cluster for msks (#1): the k8s backend's control
# plane on a single dev box. msksd talks to it through the standard
# kubeconfig (MSKSD_KUBECONFIG=/etc/rancher/k3s/k3s.yaml); create a
# dedicated token-based ServiceAccount for msksd rather than using the
# admin kubeconfig directly.
#
# Usage in a host config:
#   imports = [ /path/to/msks/nixos/k3s-dev.nix ];
#   services.msks-dev.k3s.enable = true;
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.msks-dev.k3s;
in
{
  options.services.msks-dev.k3s = {
    enable = lib.mkEnableOption "a single-node k3s cluster for msks development";
  };

  config = lib.mkIf cfg.enable {
    services.k3s = {
      enable = true;
      role = "server";
      # The dev box is the only node; no agent registration token needed.
      extraFlags = "--disable traefik --disable servicelb";
    };
    environment.systemPackages = [ config.services.k3s.package ];
  };
}
