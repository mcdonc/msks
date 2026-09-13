# Changelog

All notable changes to msks are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
tagged `vX.Y.Z`.

## \[Unreleased]

### Added

- **The msksd appliance (`msks:appliance-build`/`msks:appliance-up`/`msks:appliance-down`, #10).** The daemon now ships as a bootable appliance: a pure-nixpkgs direct-kernel-boot image (kernel, busybox init, module tree with nested-KVM + virtiofs support) whose heavy runtime — the nix-built msksd closure and the workspace VMM — resolves through a read-only virtiofs share of the host's `/nix/store`, so workspace assets built by `msks:build-guest` flow in with zero copying. The host supervisor is the devenv itself (bridge/tap via one documented `sudo`, virtiofsd and cloud-hypervisor from the pinned shell) — any Linux distro with nix + KVM, no NixOS anywhere. Verified end-to-end: appliance boots, serves the API over HTTPS (TOFU fingerprint on its serial log), and runs a workspace microvm under nested KVM driven through the API.
- **Appliance hardening fixes found by the e2e and the fresh-eyes review (#10).** The local driver now honors the absent-VM contract on stop/kill (a stale api socket behind a dead VMM reported ECONNREFUSED and delete-after-stop 500'd), the rootfs disk is declared `readonly` + `image_type: Raw` in `vm.create` (v52's autodetection otherwise disables sector-0 writes, and O_RDWR on the read-only store share fails), and `tls._write` loops `os.write` (a short write through virtio-backed storage left a truncated CA key).

- **The `msksd` daemon and its `/api/v1` API (#8).** msksd now serves versioned endpoints over one HTTPS+WSS listener — public health, hashed bearer-token auth with revocation (`MSKSD_BOOTSTRAP_TOKEN` seeds the first credential), workspace create/list/status/start/stop/delete driving cloud-hypervisor through the microvm seam, and a websocket event channel for lifecycle transitions. TLS is operator-provided (`MSKSD_TLS_CERT`/`KEY`) or a self-signed CA generated on first run whose fingerprint is logged for trust-on-first-use pinning; `--no-tls` serves plain HTTP for development. State lives in an SQLite database under the state dir (`MSKSD_STATE_DIR`), managed by Alembic migrations.
- **Nix-built guest assets (`msks:build-guest`, `msks:demo-vm`, `msks:build-runner-image`).** The devenv now produces everything needed to boot a microvm — kernel, initrd, read-only ext4 rootfs into `.guest/`, plus the k8s vm-runner container archive — from the nixpkgs revision devenv itself pins, on any Linux host with nix; the manual-download flow is gone. Boot tests pick the built artifacts up automatically (explicit `MSKSD_TEST_VMLINUX`/`MSKSD_TEST_ROOTFS`/`MSKSD_TEST_INITRD` variables keep precedence) and skip themselves when the guest was never built or `/dev/kvm` is unusable. `msks:demo-vm` boots one interactive VM from the artifacts with `ch-remote` ready (#5).
