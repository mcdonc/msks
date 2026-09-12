# Changelog

All notable changes to msks are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
tagged `vX.Y.Z`.

## \[Unreleased]

### Added

- **Nix-built guest assets (`msks:build-guest`, `msks:demo-vm`, `msks:build-runner-image`).** The devenv now produces everything needed to boot a microvm — kernel, initrd, read-only ext4 rootfs into `.guest/`, plus the k8s vm-runner container archive — from the nixpkgs revision devenv itself pins, on any Linux host with nix; the manual-download flow is gone. Boot tests pick the built artifacts up automatically (explicit `MSKSD_TEST_VMLINUX`/`MSKSD_TEST_ROOTFS`/`MSKSD_TEST_INITRD` variables keep precedence) and skip themselves when the guest was never built or `/dev/kvm` is unusable. `msks:demo-vm` boots one interactive VM from the artifacts with `ch-remote` ready (#5).
