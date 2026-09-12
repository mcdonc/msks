# Changelog

All notable changes to msks are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
tagged `vX.Y.Z`.

## \[Unreleased]

### Added

- **The `msksd` daemon and its `/api/v1` API (#8).** msksd now serves versioned endpoints over one HTTPS+WSS listener — public health, hashed bearer-token auth with revocation (`MSKSD_BOOTSTRAP_TOKEN` seeds the first credential), workspace create/list/status/start/stop/delete driving cloud-hypervisor through the microvm seam, and a websocket event channel for lifecycle transitions. TLS is operator-provided (`MSKSD_TLS_CERT`/`KEY`) or a self-signed CA generated on first run whose fingerprint is logged for trust-on-first-use pinning; `--no-tls` serves plain HTTP for development. State lives in an SQLite database under the state dir (`MSKSD_STATE_DIR`), managed by Alembic migrations.
- **Nix-built guest assets (`msks:build-guest`, `msks:demo-vm`, `msks:build-runner-image`).** The devenv now produces everything needed to boot a microvm — kernel, initrd, read-only ext4 rootfs into `.guest/`, plus the k8s vm-runner container archive — from the nixpkgs revision devenv itself pins, on any Linux host with nix; the manual-download flow is gone. Boot tests pick the built artifacts up automatically (explicit `MSKSD_TEST_VMLINUX`/`MSKSD_TEST_ROOTFS`/`MSKSD_TEST_INITRD` variables keep precedence) and skip themselves when the guest was never built or `/dev/kvm` is unusable. `msks:demo-vm` boots one interactive VM from the artifacts with `ch-remote` ready (#5).
