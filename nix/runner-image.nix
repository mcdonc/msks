# Container image for the k8s backend's vm-runner pods (#5).
#
# One pod runs this image with /dev/kvm attached; the entrypoint execs
# cloud-hypervisor with the VM spec taken from the MSKSD_* environment
# variables the backend's pod template sets (see spec_env in
# src/msks/msks/microvm/k8s.py). The image carries the same
# cloud-hypervisor the devenv shell pins and the same guest assets
# `msks:build-guest` produces, so local and k8s backends boot
# identical guests.
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
    : "''${MSKSD_VMLINUX:=/opt/msks/vmlinux}"
    : "''${MSKSD_INITRD:=/opt/msks/initrd}"
    : "''${MSKSD_ROOTFS:=/opt/msks/rootfs.ext4}"
    : "''${MSKSD_CMDLINE:=console=ttyS0 root=/dev/vda rootfstype=ext4 ro}"
    : "''${MSKSD_API_SOCKET:=/run/msks/api.sock}"
    : "''${MSKSD_CPUS:=1}"
    : "''${MSKSD_MEM_MIB:=512}"
    : "''${MSKSD_SERIAL:=file=/run/msks/serial.log}"

    initramfs_args=()
    if [ -n "$MSKSD_INITRD" ]; then
      initramfs_args+=(--initramfs "$MSKSD_INITRD")
    fi

    mkdir -p "$(dirname "$MSKSD_API_SOCKET")" "$(dirname "$MSKSD_SERIAL")"
    exec cloud-hypervisor \
      --api-socket "$MSKSD_API_SOCKET" \
      --kernel "$MSKSD_VMLINUX" \
      "''${initramfs_args[@]}" \
      --disk "path=$MSKSD_ROOTFS,readonly=on" \
      --cmdline "$MSKSD_CMDLINE" \
      --cpus "boot=$MSKSD_CPUS" \
      --memory "size=''${MSKSD_MEM_MIB}M" \
      --serial "$MSKSD_SERIAL" \
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
      "MSKSD_VMLINUX=/opt/msks/vmlinux"
      "MSKSD_INITRD=/opt/msks/initrd"
      "MSKSD_ROOTFS=/opt/msks/rootfs.ext4"
      "MSKSD_CMDLINE=console=ttyS0 root=/dev/vda rootfstype=ext4 ro"
      "MSKSD_API_SOCKET=/run/msks/api.sock"
      "MSKSD_CPUS=1"
      "MSKSD_MEM_MIB=512"
      "MSKSD_SERIAL=file=/run/msks/serial.log"
    ];
  };
}
