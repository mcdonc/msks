# The console identity helper (#63): one static binary that owns
# the vsock listener (replacing the socat EXEC line), negotiates
# the identity prelude, applies the window size, and drops to the
# requested user before exec'ing that user's shell.
#
# pkgsStatic (musl) makes the static link the default, so nothing
# depends on glibc's layout; a guest rootfs and the host nixpkgs
# pin ship different glibcs, and a dynamically linked helper would
# only run on one of them. NSS never matters — the helper parses
# /etc/passwd and /etc/group itself. Sources and lockfile live in
# src/console-helper/; the devenv shell (languages.rust) carries
# the toolchain for local builds and the coverage gate.
#
# Shared by every guest build (#250): the Debian image and the
# NixOS image stage the same derivation.
{ pkgs }:

pkgs.pkgsStatic.rustPlatform.buildRustPackage {
  pname = "msks-console-helper";
  version = "0.1.0";
  src = ../src/console-helper;
  cargoLock.lockFile = ../src/console-helper/Cargo.lock;
  doCheck = false;
}
