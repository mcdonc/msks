# Container image for the k8s backend's vm-runner pods (#5).
#
# One pod runs this image with /dev/kvm attached; the entrypoint execs
# cloud-hypervisor with the VM spec taken from MSKS_* environment
# variables (the pod template may override any of them). The image
# carries the same cloud-hypervisor the devenv shell pins and the same
# guest assets `msks:build-guest` produces, so local and k8s backends
# boot identical guests.
{
  lib,
  pkgs,
  guest,
}:

let
  # The VMM exactly matching the devenv shell's cloud-hypervisor: the
  # pinned nixpkgs channel decides both.
  vmm = pkgs.cloud-hypervisor;

  entrypoint = pkgs.writeShellScriptBin "msks-vm-runner" ''
    set -euo pipefail
    : "''${MSKS_VMLINUX:=/opt/msks/vmlinux}"
    : "''${MSKS_INITRD:=/opt/msks/initrd}"
    : "''${MSKS_ROOTFS:=/opt/msks/rootfs.ext4}"
    : "''${MSKS_CMDLINE:=console=ttyS0 root=/dev/vda rootfstype=ext4 ro}"
    : "''${MSKS_API_SOCKET:=/run/msks/api.sock}"
    : "''${MSKS_CPUS:=1}"
    : "''${MSKS_MEMORY:=512}"
    : "''${MSKS_SERIAL:=file=/run/msks/serial.log}"

    mkdir -p "$(dirname "$MSKS_API_SOCKET")" "$(dirname "$MSKS_SERIAL")"
    exec cloud-hypervisor \
      --api-socket "$MSKS_API_SOCKET" \
      --kernel "$MSKS_VMLINUX" \
      --initramfs "$MSKS_INITRD" \
      --disk "path=$MSKS_ROOTFS,readonly=on" \
      --cmdline "$MSKS_CMDLINE" \
      --cpus "boot=$MSKS_CPUS" \
      --memory "size=''${MSKS_MEMORY}M" \
      --serial "$MSKS_SERIAL" \
      --console off
  '';
in
pkgs.dockerTools.buildImage {
  name = "msks-vm-runner";
  tag = "dev";

  copyToRoot = pkgs.buildEnv {
    name = "msks-vm-runner-root";
    paths = [
      vmm
      entrypoint
      # The entrypoint's bash interpreter.
      pkgs.bash
    ];
  };

  # Guest assets at fixed, env-overridable paths.
  extraCommands = ''
    mkdir -p opt/msks
    cp ${guest}/vmlinux ${guest}/initrd ${guest}/rootfs.ext4 opt/msks/
  '';

  config = {
    Entrypoint = [ "/bin/msks-vm-runner" ];
    Env = [
      "MSKS_VMLINUX=/opt/msks/vmlinux"
      "MSKS_INITRD=/opt/msks/initrd"
      "MSKS_ROOTFS=/opt/msks/rootfs.ext4"
      "MSKS_CMDLINE=console=ttyS0 root=/dev/vda rootfstype=ext4 ro"
      "MSKS_API_SOCKET=/run/msks/api.sock"
      "MSKS_CPUS=1"
      "MSKS_MEMORY=512"
      "MSKS_SERIAL=file=/run/msks/serial.log"
    ];
  };
}
