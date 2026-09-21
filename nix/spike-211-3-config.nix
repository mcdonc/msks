# Spike 211-3 for #211/#205: the deployed update path. This file IS
# the NixOS configuration — nixos-rebuild evaluates it directly:
#
#   SPIKE3_GEN=B nixos-rebuild boot \
#     --target-host root@192.168.77.2 \
#     -I nixpkgs=<pin> -I nixos-config=nix/spike-211-3-config.nix
#
# SPIKE3_GEN selects the generation shape (A/B = marker-only change,
# C = pulls a new package, broken = unreachable network), and the
# wrapper nix/spike-211-3.nix builds the first-boot artifacts from the
# same module. The store shape is spike 2's: erofs lower with shipped
# database, ext4 store volume upper, /nix/store the overlay.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  gen =
    let
      env = builtins.getEnv "SPIKE3_GEN";
    in
    if env != "" then env else "A";

  # The overlay store URI every update-path command must see. sshd's
  # SetEnv injects it into root's ssh sessions, so nixos-rebuild's
  # remote side (nix-copy-closure, switch-to-configuration) operates
  # on the overlay store: reads consult both databases, writes land
  # only in the upper layer, and delta computation knows the lower
  # paths (a plain local store would re-send the whole base closure).
  overlayStoreUri = "local-overlay://?real=/nix/store&lower-store=local%3Froot%3D%2Fnix%2F.ro-store%26read-only%3Dtrue&upper-layer=/nix/.upper-volume/store&state=/nix/.upper-volume/nix-var&check-mount=false";
in
{
  imports = [
    (builtins.getFlake "github:microvm-nix/microvm.nix/187b0a390ee054028106a674e7b01b1cb940cbba")
    .nixosModules.microvm
  ];

  microvm = {
    hypervisor = "cloud-hypervisor";
    storeOnDisk = true;
    storeDiskType = "erofs";
    writableStoreOverlay = "/nix/.upper-volume";
    registerClosure = false;
    shares = [ ];
    volumes = [
      {
        image = "spike211-store-volume.img";
        label = "SPIKE211UPPER";
        mountPoint = "/nix/.upper-volume";
        autoCreate = false;
      }
    ];
  };

  boot.kernelParams = [ "console=ttyS0" ];
  boot.initrd.availableKernelModules = [
    "virtio_blk"
    "virtio_pci"
    "ext4"
    "overlay"
  ];

  fileSystems."/nix/store".overlay.lowerdir = lib.mkForce [
    "/nix/.ro-store/nix/store"
  ];

  # switch-to-configuration must exist for nixos-rebuild --target-host
  # boot; microvm.nix disables it by default when the host store is not
  # a share — this system's store is its own overlay, and updates
  # arrive over ssh, so switching stays enabled.
  system.switch.enable = true;

  # The generation the harness verifies over ssh.
  environment.etc."spike3-generation".text = gen;
  environment.systemPackages = lib.optionals (gen == "C") [ pkgs.hello ];

  # The broken generation: sshd listens where the bridge will never
  # route, so the harness's boot-timeout fallback path fires.
  services.openssh = {
    enable = true;
    listenAddresses = [
      {
        addr = if gen == "broken" then "192.168.77.99" else "192.168.77.2";
        port = 22;
      }
    ];
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
    # The key arrives via the spike3-ssh-key entry in NIX_PATH (a
    # file path); sshd generates its host keys at activation — they
    # live on the tmpfs root, so every fresh volume boots new ones and
    # the harness connects with key checking off.
    extraConfig = ''
      SetEnv NIX_REMOTE=${overlayStoreUri}
    '';
  };
  users.users.root.openssh.authorizedKeys.keys =
    let
      # <spike3-ssh-key> itself throws when the search-path entry is
      # absent, so scan nixPath: the harness always provides it; an
      # empty key list leaves root locked out over ssh (safe default).
      keyEntry = lib.findFirst (
        p: p.prefix == "spike3-ssh-key"
      ) null builtins.nixPath;
    in
    lib.optionals (keyEntry != null) [ (builtins.readFile keyEntry.path) ];

  # Static bridge address, no gateway (host-only link for updates).
  networking.usePredictableInterfaceNames = false;
  systemd.network = {
    enable = true;
    networks."10-eth0" = {
      matchConfig.Name = "eth0";
      address = [ "192.168.77.2/24" ];
      DHCP = "no";
    };
  };

  nix.enable = true;
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
    "local-overlay-store"
    "read-only-local-store"
  ];
  documentation.enable = false;

  # /nix/var/nix on the tmpfs root would lose profiles on every
  # reboot; the store volume owns them.
  systemd.tmpfiles.rules = [
    "L+ /nix/var/nix - - - - /nix/.upper-volume/nix-var"
  ];

  system.stateVersion = lib.trivial.release;
}
