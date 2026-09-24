# The agent toolchain shared by both guest images (#266, #268):
# the digest-pinned upstream artifacts and the offline builds,
# factored out of the Debian image when the NixOS image came to
# ship the same tools. One file owns every pin, so a bump moves
# both images together.
#
# The derivations are pin-agnostic about staging: pi and Claude
# Code build into npm's global layout (lib/node_modules) without
# bin links — the Debian overlay and the NixOS profile each make
# their own, at the published bin (dist/bundle/cli.js for pi, the
# wrapper's bin stub for Claude Code). herdr installs the release's
# static binary with its license. Each image lands them its own
# way — Debian under /usr/local from the overlay tree, NixOS
# through the system profile. Node itself is the one pin each image
# owns separately: the Debian image stages the official standalone
# tarball (Debian's own Node is older than pi's engines floor),
# while the NixOS image uses nixpkgs' Node — the platform's own
# packaging where it exists is the rule, and on NixOS it exists.
#
# Everything here stays pure derivations: pi's dependency closure
# is prefetched against its shrinkwrap (npmDepsHash) and installed
# offline, and no build step runs Node or npm scripts.
{ pkgs }:

let
  # The registry tarball behind the pi pin itself.
  piTarball = pkgs.fetchurl {
    url =
      "https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/"
      + "pi-coding-agent-0.87.1.tgz";
    hash = "sha256-FCPuPGHnyWRk4cvzyNwk0wVss0EJlcNnGpjD7MUnVA8=";
  };

  # The herdr pin (#266): the terminal workspace manager for AI
  # coding agents (herdr.dev), as the pinned release's static
  # x86-64 build — a digest-pinned upstream artifact, needing
  # nothing from the image beyond the file itself.
  agentHerdrBinary = pkgs.fetchurl {
    url =
      "https://github.com/ogulcancelik/herdr/releases/download/"
      + "v0.9.1/herdr-linux-x86_64";
    hash = "sha256-KgL+0WvrZR7wBuHUPwSPZSyk3FitBTzS1ERQVj1cVLc=";
  };

  # herdr's license, pinned to the same tag the binary came from:
  # Apache-2.0 wants the notice to travel with redistribution, and
  # the release binary alone carries none.
  agentHerdrLicense = pkgs.fetchurl {
    url =
      "https://raw.githubusercontent.com/ogulcancelik/herdr/" + "v0.9.1/LICENSE";
    hash = "sha256-xx0jnfkXJvxRnG63LTGOxlggYnIysveWIZ6H3PNdCrQ=";
  };

  # The Claude Code pin (#266): the npm wrapper package plus the
  # linux-x64 native-binary package, both digest-pinned. The
  # wrapper's own postinstall links the platform binary over its
  # bin stub; the staged package below does that wiring at build
  # time instead — a symlink standing in for the link — so the
  # image build runs no Node and no npm scripts.
  agentClaudeWrapper = pkgs.fetchurl {
    url =
      "https://registry.npmjs.org/@anthropic-ai/claude-code/-/"
      + "claude-code-2.1.281.tgz";
    hash = "sha256-WNaCuYqB1qI77iv/2Y8WVMS/e75ESHP1IadUcd5tkpk=";
  };
  agentClaudeBinary = pkgs.fetchurl {
    url =
      "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/"
      + "claude-code-linux-x64-2.1.281.tgz";
    hash = "sha256-sNo8XYzhnBCEmFvKLkmqTG54rQYGwrSsfeYH2aMd10o=";
  };

  # Claude Code in npm's global layout (#266): the wrapper at
  # lib/node_modules/@anthropic-ai/claude-code with the platform
  # package nested as its optional dependency and the bin stub
  # pointed at the platform binary — the exact tree
  # `npm install -g` leaves behind. The global bin link stays
  # absent: the Debian overlay links /usr/local/bin/claude itself,
  # and the NixOS loader-patched variant below carries its own.
  claudePackage =
    pkgs.runCommand "agent-claude-code" { nativeBuildInputs = [ pkgs.gnutar ]; }
      ''
        set -eu
        mods=$out/lib/node_modules/@anthropic-ai
        mkdir -p $mods/claude-code/bin
        mkdir -p \
          $mods/claude-code/node_modules/@anthropic-ai/claude-code-linux-x64
        tar -xzf ${agentClaudeWrapper} \
          -C $mods/claude-code --strip-components=1
        rm -f $mods/claude-code/bin/claude.exe
        tar -xzf ${agentClaudeBinary} \
          -C $mods/claude-code/node_modules/@anthropic-ai/claude-code-linux-x64 \
          --strip-components=1
        ln -s \
          ../node_modules/@anthropic-ai/claude-code-linux-x64/claude \
          $mods/claude-code/bin/claude.exe
      '';

  # The pi pin's source (#266): the registry tarball with the
  # shrinkwrap integrity gaps closed, ready for the offline npm
  # build. nix/pi-shrinkwrap-patch.py (unit-tested in
  # test_guestassets.py) closes the integrity gaps the published
  # shrinkwrap leaves and strips the devDependencies the pruned
  # lock no longer carries.
  patchedPiSource =
    pkgs.runCommand "pi-coding-agent-src"
      {
        nativeBuildInputs = [
          pkgs.gnutar
          pkgs.python3
        ];
      }
      ''
        set -eu
        mkdir -p $out
        tar -xzf ${piTarball} -C $out --strip-components=1
        chmod -R u+w $out
        python3 ${./pi-shrinkwrap-patch.py} \
          $out/npm-shrinkwrap.json $out/package.json \
          ${./pi-shrinkwrap-integrity.json}
      '';

  # The pi pin (#266): the npm package built offline from its own
  # shrinkwrap — npmDepsHash pins the whole dependency closure, so
  # the build is reproducible and no network touches the sandbox.
  # The output is the npm tree each image stages its own way; the
  # bin link here serves the profile consumers (the NixOS image's
  # systemPackages) at the published bin, the same target the
  # Debian overlay links itself. pi's shebangs carry
  # `env node` — a workspace's PATH must hold Node wherever pi is
  # reachable.
  piPackage = pkgs.buildNpmPackage {
    pname = "pi-coding-agent";
    version = "0.87.1";
    src = patchedPiSource;
    npmDepsHash = "sha256-g7xLIxQbKLO/l09bQKE+knAF5+tgO3hzn3UXgFIJZrg=";
    # The published package ships dist/ prebuilt; there is nothing
    # to compile.
    buildPhase = ''
      runHook preBuild
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      mkdir -p $out/lib/node_modules/pi-coding-agent $out/bin
      cp -r ./. $out/lib/node_modules/pi-coding-agent/
      ln -s ../lib/node_modules/pi-coding-agent/dist/bundle/cli.js \
        $out/bin/pi
      runHook postInstall
    '';
    # stdenv's fixup patchShebangs rewrites `env node` (and `env
    # bash`) shebangs into BUILD-time store paths — paths no guest
    # carries, and a rewrite that otherwise drags the build Node in
    # as a phantom runtime dependency. Sweep every touched file
    # back to the portable `env` form (the interpreter's basename
    # is the whole difference: every image that can reach these
    # tools has Node and bash on its login PATH by construction),
    # then fail the build if any store interpreter survives — the
    # #36 bug class: an upstream layout change must fail here,
    # not a workspace's first `pi`.
    postFixup = ''
      # grep exits nonzero when a batch finds nothing, and the
      # builder runs pipefail: a find|xargs|grep line would die on
      # the first all-negative batch. Read the matches through a
      # process substitution instead, with the no-match exit
      # absorbed.
      while IFS= read -r -d "" f; do
        sed -i '1s|^#!/nix/store/\([^ ]*/\([^/]*\)\)$|#!/usr/bin/env \2|' "$f"
      done < <(grep -rlZ '^#!/nix/store/' "$out" || true)
      left=$(grep -rl '^#!/nix/store/' "$out" || true)
      if [ -n "$left" ]; then
        echo "$left" >&2
        echo "pi tree still carries build-time store interpreters" >&2
        exit 1
      fi
    '';
  };

  # herdr installed (#266): the static binary, executable as-is,
  # with its license beside it, in the layout every image's staging
  # understands.
  herdrPackage = pkgs.runCommand "agent-herdr" { } ''
    set -eu
    install -D -m 0755 ${agentHerdrBinary} $out/bin/herdr
    install -D -m 0644 ${agentHerdrLicense} \
      $out/share/doc/herdr/LICENSE
  '';

  # The NixOS-side Claude Code (#268): the staged tree with the
  # platform binary's ELF interpreter pointed at nixpkgs' glibc,
  # plus the bin link the profile needs (the pristine tree stays
  # bin-less for the Debian overlay to link itself). The binary as
  # published wants /lib64/ld-linux-x86-64.so.2 — a stock NixOS
  # ships no such loader — and needs nothing else beyond glibc, so
  # once the loader resolves the binary runs.
  claudeLoaderPatched =
    pkgs.runCommand "agent-claude-code-nixos"
      {
        inherit claudePackage;
        loader = pkgs.stdenv.cc.bintools.dynamicLinker;
        nativeBuildInputs = [ pkgs.patchelf ];
      }
      ''
        set -eu
        cp -a ${claudePackage}/. $out/
        chmod -R u+w $out
        patchelf --set-interpreter "$loader" \
          $out/lib/node_modules/@anthropic-ai/claude-code/node_modules/@anthropic-ai/claude-code-linux-x64/claude
        mkdir -p $out/bin
        ln -s \
          ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe \
          $out/bin/claude
      '';

in
{
  inherit
    claudePackage
    claudeLoaderPatched
    herdrPackage
    piPackage
    ;
}
