# The owner-exec helper's build (#1087, part of #1080 phase 2a):
# one C file (virtiofsd-as.c beside this file) compiled to a
# static binary — no interpreter, no libraries at exec, the shape
# a setuid binary should ship in.
#
# Three consumers share this one definition:
#
#   - the NixOS module (nix/module.nix) builds it against the
#     host's package set, pinning virtiofsd's store path as the
#     exec target, into security.wrappers.msks-virtiofsd-as;
#
#   - the release workflow builds the static musl binaries — one
#     per architecture — through the flake's virtiofsd-as-static
#     and virtiofsd-as-aarch64-static attributes, attached to the
#     release with their checksums in SHASUMS;
#
#   - CI compiles the source (lint.yml, plain cc -Werror) so the
#     C cannot rot between releases.
{
  lib,
  stdenv,
  virtiofsd ? null,
}:
stdenv.mkDerivation {
  pname = "virtiofsd-as";
  version = "1.0.0";

  src = ./virtiofsd-as.c;

  # The fixed virtiofsd path the helper execs — compile-time, so
  # no caller can point the exec at a binary of its choosing. The
  # consuming platform's virtiofsd when given (the NixOS module);
  # the distro install location otherwise (where Debian's and
  # Fedora's virtiofsd packages put it, and where the setup tools
  # find it).
  virtiofsdPath =
    if virtiofsd == null then
      "/usr/libexec/virtiofsd"
    else
      "${virtiofsd}/bin/virtiofsd";

  dontUnpack = true;

  buildPhase = ''
    runHook preBuild
    "$CC" -O2 -D_FORTIFY_SOURCE=2 -fstack-protector-strong \
      -Wall -Wextra -Werror -static \
      -DVIRTIOFSD_PATH=\""$virtiofsdPath"\" \
      -o virtiofsd-as "$src"
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 virtiofsd-as "$out/bin/virtiofsd-as"
    runHook postInstall
  '';

  meta = {
    description = "msks owner-exec helper: exec virtiofsd as a named uid";
    mainProgram = "virtiofsd-as";
    platforms = lib.platforms.linux;
  };
}
