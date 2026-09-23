{
  pkgs,
  config,
  lib,
  ...
}:
let
  # The LLVM tools for the Rust coverage gate (#63), taken from the
  # SAME rust-overlay nightly the toolchain pins: the compiler's
  # llvm-profdata/llvm-cov understand its instrumented profile format
  # exactly (an nixpkgs LLVM of the "same" major can be an -rc with a
  # different raw-profile revision, which merges into garbage).
  rustOverlay = config.lib.getInput { name = "rust-overlay"; };
  rustPkgs = import pkgs.path {
    overlays = [ rustOverlay.overlays.default ];
    config = { };
  };
  rustLlvmTools = rustPkgs.rust-bin.nightly.latest.llvm-tools;

  # jscpd token-clone scanner (#71, ported from klangk #2904): 5.x ships
  # a prebuilt Rust binary via platform-specific npm packages
  # (esbuild-style), so it is not in nixpkgs; pin the binary per platform
  # with fixed hashes (the fmtk pattern). One pinned version keeps clone
  # reports reproducible across contributors and CI. The `msks-jscpd`
  # script and the pre-commit gate hook run it over the backend. The
  # linux branches go beyond klangk verbatim: the arm64-gnu tarball is
  # pinned too (same package family), and any other platform fails at
  # eval time instead of silently installing a foreign-arch binary.
  jscpdBinaryVersion = "5.0.16";
  jscpd = pkgs.stdenv.mkDerivation {
    pname = "jscpd";
    version = jscpdBinaryVersion;
    src = pkgs.fetchurl {
      url =
        if pkgs.stdenv.isDarwin then
          "https://registry.npmjs.org/jscpd-darwin-"
          + (if pkgs.stdenv.hostPlatform.darwinArch == "arm64" then "arm64" else "x64")
          + "/-/jscpd-darwin-"
          + (if pkgs.stdenv.hostPlatform.darwinArch == "arm64" then "arm64" else "x64")
          + "-${jscpdBinaryVersion}.tgz"
        else if pkgs.stdenv.hostPlatform.isx86_64 then
          "https://registry.npmjs.org/jscpd-linux-x64-gnu/-/jscpd-linux-x64-gnu-${jscpdBinaryVersion}.tgz"
        else if pkgs.stdenv.hostPlatform.isAarch64 then
          "https://registry.npmjs.org/jscpd-linux-arm64-gnu/-/jscpd-linux-arm64-gnu-${jscpdBinaryVersion}.tgz"
        else
          throw "jscpd: no prebuilt binary for ${pkgs.stdenv.hostPlatform.system}";
      hash =
        if pkgs.stdenv.isDarwin then
          (
            if pkgs.stdenv.hostPlatform.darwinArch == "arm64" then
              "sha256-vntXwMkns8HqtHwVzxthzun0tpRAe755YKB5k4c3Wqg="
            else
              "sha256-X2hK+EAgrXGRLUymdo5qgTuWoFQ3U0cz9J064UFQppM="
          )
        else if pkgs.stdenv.hostPlatform.isx86_64 then
          "sha256-+6PhbDzUn0e4sQgsUs/kF0C5HlMOixZvlNolO9a4VdI="
        else
          "sha256-hTlIJMcf3vi8qbJLKR0Txc1a5BOYYjkPc6YWTwCiEEc=";
    };
    sourceRoot = ".";
    dontConfigure = true;
    dontBuild = true;
    dontStrip = true;
    installPhase = ''
      install -Dm555 -t $out/bin package/bin/jscpd
    '';
  };
  # The secretspec CLI (#198): the SecretSpec Python SDK's native
  # ABI exposes only resolve/report — every write path (set/delete)
  # lives in the CLI — so the secret store shells out to the binary,
  # the house pattern for external tools (MSKSD_SECRET_STORE_CLI
  # names it at runtime). Pinned to the v0.20.0 release tarballs
  # (cargo-dist builds; same fetchurl-pinning as jscpd above) for
  # the two linux arches; other platforms fail at eval time. 0.20
  # reads `set` values from piped stdin (text-trimmed), so secrets
  # ride stdin, never argv; the exact-bytes `--from-file` flag lands
  # in 0.21+ and the pin can move then.
  secretspec = pkgs.stdenv.mkDerivation {
    pname = "secretspec";
    version = "0.20.0";
    src = pkgs.fetchurl {
      url =
        if pkgs.stdenv.isx86_64 then
          "https://github.com/cachix/secretspec/releases/download/v0.20.0/secretspec-x86_64-unknown-linux-gnu.tar.xz"
        else if pkgs.stdenv.isAarch64 then
          "https://github.com/cachix/secretspec/releases/download/v0.20.0/secretspec-aarch64-unknown-linux-gnu.tar.xz"
        else
          throw "secretspec: no prebuilt binary for ${pkgs.stdenv.hostPlatform.system}";
      hash =
        if pkgs.stdenv.isx86_64 then
          "sha256-NNNMFIxGXICd9UdSYWmClb2RF+UnfWeT7HlwJtQmIRQ="
        else
          "sha256-Jbg9vHuFG7NA84QaGdBGoWOQKfGRhY4LPxHi67YjNvI=";
    };
    sourceRoot = ".";
    dontConfigure = true;
    dontBuild = true;
    dontStrip = true;
    installPhase = ''
      install -Dm555 -t $out/bin secretspec-*/secretspec
    '';
  };
in
{
  # msks dev environment: Python 3.14 + cloud-hypervisor toolchain (#2).
  # Mirrors the klangk conventions (AGENTS.md): CI-identical test task,
  # testmon for scoped iteration, xenon rank-A gate via a single script
  # shared with the pre-commit hook.
  # Rust for the guest-side console helper (#63): src/console-helper
  # builds locally with this toolchain (cargo test, the coverage
  # gate) and in the guest image via rustPlatform + static glibc
  # (nix/guest-debian.nix). Nightly because branch coverage
  # (-Z coverage-options=branch) is nightly-only; the pin comes from
  # the rust-overlay input in devenv.lock. The LLVM 23 tools pair
  # with the pinned rustc's LLVM for the coverage gate's
  # llvm-profdata/llvm-cov.
  env.MSKS_RUST_LLVM_TOOLS = "${rustLlvmTools}/lib/rustlib/x86_64-unknown-linux-gnu/bin";

  languages.rust = {
    enable = true;
    channel = "nightly";
    components = [
      "rustc"
      "cargo"
      "clippy"
      "rustfmt"
      "llvm-tools"
    ];
  };

  languages.python = {
    enable = true;
    # Pinned to the channel's python314 rather than the `python3` alias —
    # the toolchain version is a project decision, not an accident of the
    # pinned nixpkgs channel's default minor (klangk #2844 precedent).
    package = pkgs.python314;
    venv.enable = true;
    uv = {
      enable = true;
      # sync.enable left off: devenv's sync gate fingerprints only the root
      # pyproject.toml and not uv.lock, so lock-only bumps (uv lock
      # --upgrade) skip sync and the venv goes stale. msks:uv-sync below
      # owns dependency sync (klangk workaround, see their devenv.nix).
    };
    directory = ".";
  };

  packages = with pkgs; [
    bash # explicit bash for shell scripts (CI /bin/sh may be dash)
    # Cargo plugin kept for ad-hoc local coverage reports
    # (`cargo llvm-cov --branch`); the gate itself drives the LLVM
    # tools directly (scripts/rust-coverage.sh) because nightly
    # cargo's build layout breaks this tool's object discovery.
    cargo-llvm-cov
    cloud-hypervisor # VMM driven by the local backend (#1); ships ch-remote
    curl # unix-socket REST poking during CH debugging
    e2fsprogs # resize2fs/e2fsck: grow and check workspace volumes
    cdrtools # genisoimage: the #41 cidata seed disks (iso9660)
    iproute2 # taps and addresses for the dev daemon's workspaces
    iptables # diagnose foreign FORWARD drops (docker's policy on CI runners)
    # that block the egress forward path the nft rules accept (#75/#52)
    jscpd # token-clone scanner (#71), pinned rust binary (see above)
    nftables # egress chains/NAT for the #52 smoke path
    conntrack-tools # the consent-revocation tool the daemon execs (net/conntrack.py, #52)
    openssh # host-side ssh client: the forward-path smoke (#110) and
    # the documented ssh workflow (#112) run over `msks forward`
    qemu # qemu-img for rootfs conversion during guest-image experiments
    rsync # host-side rsync over the forward (#110's sync path)
    secretspec # the #198 secret store's CLI (pinned release binary)
    ruff
    socat # AF_UNIX <-> pty/stdio plumbing for CH socket debugging
    tcpdump # packet-level debugging of the egress path (tap vs uplink)
    # cyclomatic-complexity gate tool: built against python3.14 because
    # nixpkgs' top-level xenon runs on an older python whose parser can
    # reject syntax ruff format writes for a 3.14 codebase, silently
    # skipping files (klangk #3411/#3415 precedent). scripts/xenon-gate.sh
    # turns any such skip into a hard failure.
    (pkgs.callPackage (pkgs.path + "/pkgs/by-name/xe/xenon/package.nix") {
      python3 = pkgs.python314;
    })
    (python314Packages.radon) # complexity introspection (radon cc)
    # egress consent's NFQUEUE binding (#69): the C libraries the
    # netfilterqueue wheel links (the queue library and its
    # nfnetlink substrate), present so `uv sync` builds it in every
    # dev/CI shell — the binding is a base dependency and the flags
    # below point its build and import at these store paths.
    libnetfilter_queue
    libnfnetlink
  ];

  env.UV_PYTHON = config.languages.python.package;
  # The wheel build (CFLAGS/LDFLAGS) and the runtime import
  # (LD_LIBRARY_PATH) both resolve against the nix store — each
  # library carries its .so and headers in one output.
  env.CFLAGS = "-I${pkgs.libnetfilter_queue}/include -I${pkgs.libnfnetlink}/include";
  env.LDFLAGS = "-L${pkgs.libnetfilter_queue}/lib -L${pkgs.libnfnetlink}/lib";
  env.LD_LIBRARY_PATH = "${pkgs.libnetfilter_queue}/lib:${pkgs.libnfnetlink}/lib";

  # The nixpkgs source the devenv lock pins — the revision every
  # guest build compiles against. Exported to every devenv context
  # (shells, processes, tasks, scripts), so the build scripts'
  # MSKS_GUEST_NIXPKGS requirement holds however they are reached:
  # the msksd process exec or a hand-run script from a shell.
  env.MSKS_GUEST_NIXPKGS = pkgs.path;

  # The msks client (#21) targets the DEV DAEMON (#231) by default,
  # so `msks ls` / `msks ssh` work from any devenv shell with no
  # exports. The URL, token, and CA all resolve per shell in
  # enterShell, from the daemon's state dir — the API port included,
  # which the msksd process seeds into that dir (each worktree its
  # own stable port). Until the daemon's first boot those files do
  # not exist, so the presets stay unset (a value exported before
  # entering the shell survives) and the client warns it does not
  # verify on first connect.

  tasks = {
    # WORKAROUND (klangk pattern): devenv's uv sync gate only hashes the
    # root pyproject.toml, never uv.lock — lock-only changes skip sync and
    # the venv silently goes stale. This task runs `uv sync` unconditionally:
    # on a current venv it is a ~0.1s no-op, cheaper than getting the gate
    # right. `after` pins ordering: the venv must exist before sync, or
    # devenv:python:virtualenv would `rm -rf` the freshly-synced deps.
    "msks:uv-sync" = {
      exec = ''
        cd "$DEVENV_ROOT"
        uv sync -p "$UV_PYTHON" --group dev
      '';
      after = [ "devenv:python:virtualenv" ];
      before = [ "devenv:enterShell" ];
    };
    # WORKAROUND (#32, klangk #3444 pattern): devenv 2.3.x's RunMode::All
    # scheduler adds the prerequisites of every visited task — including
    # the skipped devenv:enterTest (it sits `after` enterShell), whose
    # prerequisite devenv:git-hooks:run is the full pre-commit suite —
    # so a failing hook aborts `devenv shell` before it opens. Clearing
    # the `before` edge keeps that task out of the shell's task graph
    # (mkForce replaces the upstream list; a plain `before = [ ]`
    # concatenates with it and changes nothing). The commit-time hook
    # keeps enforcing the suite on `git commit`. Remove this override
    # once an upstream release stops scheduling prerequisites of
    # skipped tasks.
    "devenv:git-hooks:run" = lib.mkIf config.git-hooks.enable {
      before = lib.mkForce [ ];
    };
  };

  # The deployment-host daemon, dev shape (#231): msksd runs
  # FIRST-LEVEL on this host — cloud-hypervisor on the real /dev/kvm,
  # with per-VM taps and the egress consent stack in this kernel.
  # Lifecycle: `devenv processes up` (the managed
  # FOREGROUND process below — attached, Ctrl-C stops) or `msks-dev`
  # (the same script by hand); scripts/dev-daemon.sh is the one
  # source of truth both exec. The detached daemon mode also works;
  # the nohup+pidfile variant was tried and dropped. The two ambient
  # caps arrive through the host's capability wrapper
  # (security.wrappers.msks-caps): net_admin for per-VM taps and
  # nftables chains, net_bind_service for the egress stack's DHCP 67
  # and DNS 53 — the wrapper is the whole grant (devenv's
  # linux.capabilities broker was tried and dropped: a root helper on
  # a rotating store path plus wildcard sudoers for the same grant).
  # More than one worktree runs its own instance: each seeds a
  # stable API port into its own state dir (guarded by a state-dir
  # lock — one daemon per catalog), and the egress subnet derives
  # from the port (a distinct private 10.x/16 each) so concurrent
  # instances allocate disjoint /30 pools.
  processes = {
    # The dev-mode daemon as a managed foreground process (#231):
    # `devenv processes up` (no -d) runs it attached; the manager
    # supervises restarts and owns the graceful stop (grace below).
    # The exec IS the msks-dev script — same seeding, same wrapper
    # chain, one source of truth.
    msksd = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/dev-daemon.sh"'';
      # A workspace's stop cycle needs its window — the
      # page-cache-only-commit data-loss lesson (#146).
      shutdown.grace = 90;
    };
  };

  # --- msks command scripts (#166) ---
  # Plain scripts on the shell's PATH: invoked directly from a devenv
  # shell (`msks-xenon`) or from outside (`devenv shell -- msks-xenon`).
  # No task DAG and no per-invocation devenv CLI startup — devenv bakes
  # each entry into an executable. They live here (rather than as
  # standalone files under scripts/) because several need the
  # Nix-interpolated pinned nixpkgs path (${pkgs.path}, which moves
  # with devenv.lock). The one true msks task left is `msks:uv-sync`
  # above — it needs `after`/`before` ordering against the
  # devenv-managed venv and shell tasks. The build scripts run their
  # nix-build unconditionally: with unchanged inputs the nix cache
  # makes that a quick no-op, which replaces devenv's execIfModified
  # task cache without its stale-hit failure modes (#160).

  scripts.msks-xenon = {
    description = "Complexity gate: rank A everywhere";
    exec = ''exec bash "$DEVENV_ROOT/scripts/xenon-gate.sh" "$@"'';
  };

  scripts.msks-jscpd = {
    description = "Token-clone gate over the backend";
    exec = ''exec bash "$DEVENV_ROOT/scripts/jscpd-gate.sh" "$@"'';
  };

  scripts.msks-preflight = {
    description = "All pre-commit offenders in one pass, before the commit attempt (--fast skips the suite)";
    exec = ''exec bash "$DEVENV_ROOT/scripts/preflight.sh" "$@"'';
  };

  # Guest VM assets out of the pinned nixpkgs, no manual downloads
  # (#5). ${pkgs.path} is the nixpkgs source the devenv lock itself
  # evaluated — the guest toolchain cannot drift from the dev shell,
  # and the build needs nothing from the host but nix.
  scripts.msks-build-guest = {
    description = "Build the microvm guest assets (kernel, initrd, ext4 rootfs) into the guest state dir (.devenv/state/guest; MSKS_GUEST_DIR relocates it; `msks-build-guest nixos` builds the NixOS guest into .devenv/state/guest-nixos)";
    exec = ''exec bash "$DEVENV_ROOT/scripts/build-guest.sh" "$@"'';
  };

  # The dev-mode daemon by hand (#231): `msks-dev` runs the same
  # script the msksd process above execs, in a kept-open terminal;
  # Ctrl-C stops it. See the block comment at `processes = {` above.
  scripts.msks-dev = {
    description = "Run the deployment-host dev daemon in the foreground (msksd through the msks-caps wrapper; Ctrl-C stops)";
    exec = ''exec bash "$DEVENV_ROOT/scripts/dev-daemon.sh"'';
  };

  scripts.msks-demo-vm = {
    description = "Boot one microvm from the built guest assets (serial console on this terminal)";
    exec = ''exec bash "$DEVENV_ROOT/scripts/demo-vm.sh" "$@"'';
  };

  # The workspace image archive, alone (#141): the bare-host dev
  # daemon's default image. Same derivation tree as msks-build-guest
  # (pinned nixpkgs, guest-assets expression, the Rust
  # console-helper baked into the workspace image), but built as
  # ONE artifact and landed as a symlink the daemon imports on its
  # first boot — no kernel/rootfs copies, no guest asset dir.
  scripts.msks-build-guest-archive = {
    description = "Build the workspace image archive into the bare-host daemon state (.devenv/state/msksd/default-image; MSKSD_STATE_DIR relocates it)";
    exec = ''
      # devenv's script wrapper adds no errexit — a failed build below
      # must not print success and exit 0.
      set -euo pipefail
      root="$DEVENV_ROOT"
      state="''${MSKSD_STATE_DIR:-$root/.devenv/state/msksd}"
      # Anchor a relative value below the repo root; the DAEMON
      # resolves a relative MSKSD_STATE_DIR against its own CWD
      # (settings.py) — an absolute path moves both identically
      # (README says the same).
      case "$state" in
      /*) ;;
      *) state="$root/$state" ;;
      esac
      mkdir -p "$state"
      echo "msks: building the workspace image archive into $state (idempotent — unchanged inputs are a cached no-op)"
      out=$(
        nix-build --no-out-link -I nixpkgs=${pkgs.path} \
          "$root/nix/guest.nix" -A image-archive
      )
      ln -sfn "$out" "$state/default-image"
      echo "msks: image archive at $out (linked as $state/default-image)"
    '';
  };

  # The bare-host daemon's state convergence (#141): the idempotent
  # half a keyed task cannot own — a deleted symlink or token file
  # with unchanged sources would make a cached archive build skip
  # (the #140 manifest lesson), so this ALWAYS runs and heals the
  # residue: the archive build above re-lands the image pointer every
  # time, and the token block below mints only when missing.
  scripts.msks-dev-ready = {
    description = "Converge the bare-host dev daemon state (the daemon state dir's token + image pointer; .devenv/state/msksd by default, MSKSD_STATE_DIR relocates it)";
    exec = ''
      # devenv's script wrapper adds no errexit — a failed archive
      # build must not mint a token and print success.
      set -euo pipefail
      root="$DEVENV_ROOT"
      state="''${MSKSD_STATE_DIR:-$root/.devenv/state/msksd}"
      # See msks-build-guest-archive's case block.
      case "$state" in
      /*) ;;
      *) state="$root/$state" ;;
      esac
      mkdir -p "$state"
      MSKSD_STATE_DIR="$state" msks-build-guest-archive
      if [ ! -s "$state/bootstrap-token" ]; then
        # 256 bits of urandom, hex — the same shape the dev
        # daemon's own seeding uses. Stable across restarts — the daemon inserts
        # it into its catalog once and keeps it valid. temp+rename:
        # a concurrent daemon start must never `cat` a half-written
        # token (the daemon would insert the truncated value as a
        # valid row, and every later login 401s). The flock closes
        # the two-writer window: a manual `msks-dev-ready` racing
        # the daemon's own start cannot mint two tokens where the
        # file keeps one and the daemon booted with the other (a
        # 401 until restart, otherwise).
        (
          flock 9
          [ -s "$state/bootstrap-token" ] && exit 0
          tok=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
          printf '%s' "$tok" >"$state/.bootstrap-token.tmp"
          chmod 600 "$state/.bootstrap-token.tmp"
          mv "$state/.bootstrap-token.tmp" "$state/bootstrap-token"
          echo "msks: minted $state/bootstrap-token"
        ) 9>"$state/.lock"
      fi
      echo "msks: dev state ready at $state (bootstrap-token + default-image -> $(readlink "$state/default-image"))"
    '';
  };

  # CI-identical full suite: -n auto is how CI runs it — never optional
  # (sysmon branch coverage under-counts in a single-process run; klangk
  # AGENTS.md has the full story). addopts in pyproject.toml carry the
  # coverage flags; the conftest pins COVERAGE_CORE=sysmon.
  scripts.unit-tests.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest src/msks/tests -v -n auto "$@"
  '';

  # Scoped run: re-run only tests whose coverage touches changed source
  # lines (pytest-testmon). Inert on CI — CI runs the full suite via
  # `test`; this is the local tight-loop accelerator. First run on a
  # clean tree baselines the line->test map into .testmondata at the
  # repo root (pytest rootdir = repo root; gitignored); delete it after
  # a large refactor to re-baseline.
  #
  # COVERAGE_CORE=ctrace: testmon maps lines->tests via dynamic
  # contexts (switch_context per test), which the sysmon core does not
  # support — coverage>=7.15 warns and the mapping is degraded. The C
  # tracer supports contexts, so scoped runs pin it explicitly (the
  # conftest's setdefault does not override a preset env var). The
  # gated `test` run stays on sysmon per conftest.py.
  scripts.testmon.exec = ''
    cd $DEVENV_ROOT
    exec env COVERAGE_CORE=ctrace python -m pytest src/msks/tests -v -n auto --no-cov --testmon "$@"
  '';

  # --- Pre-commit hooks ---
  git-hooks.hooks = {
    # Python: ruff lint + format
    ruff-lint = {
      enable = true;
      name = "ruff check";
      entry = "${pkgs.ruff}/bin/ruff check --fix";
      files = "\\.py$";
      language = "system";
      pass_filenames = true;
    };
    ruff-format = {
      enable = true;
      name = "ruff format";
      entry = "${pkgs.ruff}/bin/ruff format";
      files = "\\.py$";
      language = "system";
      pass_filenames = true;
    };
    # Rust (console-helper, #63): formatting, linting, and the 100%
    # line+branch coverage gate. Each hook fires only when the crate
    # (or the gate itself) is part of the commit; pass_filenames is
    # off because each command grades the whole crate.
    rustfmt = {
      enable = true;
      name = "cargo fmt (console-helper)";
      entry = "cargo fmt --manifest-path src/console-helper/Cargo.toml --check";
      files = "^src/console-helper/.*\\.rs$";
      language = "system";
      pass_filenames = false;
    };
    rust-clippy = {
      enable = true;
      name = "cargo clippy (console-helper)";
      entry = "cargo clippy --manifest-path src/console-helper/Cargo.toml --all-targets -- -D warnings";
      files = "^src/console-helper/.*\\.(rs|toml|lock)$";
      language = "system";
      pass_filenames = false;
    };
    rust-coverage = {
      enable = true;
      name = "rust coverage gate (console-helper)";
      entry = "scripts/rust-coverage.sh";
      files = "^src/console-helper/|^scripts/rust-coverage\\.sh$";
      language = "system";
      pass_filenames = false;
    };
    # Complexity gate: rank A everywhere. pass_filenames = false — the
    # hook grades the full tree via scripts/xenon-gate.sh (a staged
    # subset's average can exceed 5 while the whole tree passes);
    # `files` stays as the run trigger.
    xenon = {
      enable = true;
      name = "xenon";
      entry = "scripts/xenon-gate.sh";
      files = "^src/msks/msks/.*\\.py$|^scripts/.*\\.py$";
      language = "system";
      pass_filenames = false;
    };
    # Token-clone gate: no exact clone of >= 70 tokens in the backend.
    # pass_filenames = false — cross-file clones only show when the whole
    # tree is scanned (a staged subset can hide them); `files` stays as the
    # run trigger.
    jscpd = {
      enable = true;
      name = "jscpd";
      entry = "scripts/jscpd-gate.sh";
      files = "^src/msks/msks/.*\\.py$";
      language = "system";
      pass_filenames = false;
    };
    # Deferred-imports gate (#72, klangk's AST checker): imports live
    # at module scope. Plain top-level, ``if TYPE_CHECKING:`` blocks,
    # and module-scope ``try/except ImportError`` guards are exempt;
    # ``# allow-deferred-import`` suppresses an individual import (on
    # the line or the comment line above). Staged files are mapped to
    # their package roots, so the hook scans whole packages. The
    # interpreter is the pinned python from `languages.python` by
    # store path, NOT a bare `python3`: pre-commit prepends its own
    # interpreter's bin to hook PATHs (the system 3.13 here), which
    # cannot parse the project's PEP 758 ``except X, Y:`` syntax —
    # under a bare `python3` the checker silently skipped the whole
    # backend and passed vacuously (found via the review of #84).
    # require_serial: one invocation with all files — pass_filenames
    # maps them to package roots, and chunked invocations would
    # rescan (and re-print) the same packages once per chunk.
    deferred-imports = {
      enable = true;
      name = "deferred-imports";
      entry = "${config.languages.python.package}/bin/python scripts/check_deferred_imports.py";
      files = "\\.py$";
      language = "system";
      pass_filenames = true;
      require_serial = true;
    };
    # Shell (#72, klangk settings): format + static analysis + the
    # shebang guard on executable text files.
    shfmt.enable = true;
    shfmt.settings.indent = 2;
    check-executables-have-shebangs.enable = true;
    shellcheck.enable = true;
    # Markdown lint (#72, klangk rules). Division of labor with the
    # prettier hook: prettier owns formatting, markdownlint stays a
    # lint-only gate (no --fix) over rules prettier either enforces
    # itself (blank lines around headings/lists, single blank runs,
    # final newline) or never touches (code-fence languages, heading
    # text, duplicate siblings). The three disabled rules are the
    # prettier-owned ones — MD013 line length (prettier preserves
    # prose wrapping and pads table rows past 80), MD034 bare URLs
    # (prettier wraps them in <>), MD060 table-pipe alignment
    # (prettier realigns pipes) — so a prettier-formatted file always
    # passes markdownlint and the two hooks cannot ping-pong. Keep
    # new rules inside that invariant. Passed inline as JSON config;
    # this git-hooks pin takes a structured settings attrset, so no
    # generated .markdownlint.yaml is needed.
    markdownlint.enable = true;
    markdownlint.settings.configuration = {
      MD013 = false;
      MD024.siblings_only = true;
      MD034 = false;
      MD060 = false;
    };
    # GitHub Actions workflows (#72).
    actionlint.enable = true;
    # Secrets (#72): trufflehog over the staged file contents. The
    # pin's stock hook runs `git --since-commit HEAD`, which scans
    # commits strictly newer than HEAD — the empty set at commit time
    # and on any clean checkout, so it can never fail (verified with a
    # canary commit this branch carried briefly). This entry scans the
    # files pre-commit passes — staged files at commit time, all
    # tracked files under --all-files (what CI runs). Only credentials
    # that verify live fail the commit (--results=verified --fail),
    # so key-shaped test fixtures stay green and offline runs degrade
    # to a pass (verification errors land in `unknown`).
    trufflehog = {
      enable = true;
      name = "trufflehog";
      entry = "${pkgs.trufflehog}/bin/trufflehog filesystem --fail --results=verified";
      language = "system";
      pass_filenames = true;
    };
    # Nix (#72, klangk width).
    nixfmt.enable = true;
    nixfmt.settings.width = 80;
    # TOML (#72): every staged TOML file must parse.
    check-toml.enable = true;
    # YAML (#72, klangk rules): relaxed preset, lines up to 200
    # columns. Warnings stay non-fatal (strict = false) — klangk's
    # generated-config hook ran plain yamllint, failing on errors
    # only; this pin's structured settings replace that file.
    yamllint.enable = true;
    yamllint.settings = {
      configuration = ''
        extends: relaxed
        rules:
          line-length:
            max: 200
      '';
      strict = false;
    };
    # JS/TS/JSON/YAML/Markdown formatting (#72, klangk settings):
    # rewrite in place. Unknown file types (.py, .nix, .sh, .lock)
    # are skipped (--ignore-unknown is this pin's default); the
    # excludes keep lock files out of the file set regardless. Hook
    # ids sort lexicographically in the generated manifest, so this
    # runs after markdownlint/nixfmt but before the ruff hooks — the
    # one-run rewrite dance is harmless either way, because prettier
    # (--ignore-unknown) and ruff touch disjoint file sets. A run
    # that rewrites fails once with "files were modified"; the
    # re-staged run validates the final bytes (see the markdownlint
    # comment for why those bytes always pass).
    prettier = {
      enable = true;
      settings.write = true;
      excludes = [ "\\.lock$" ];
    };
  };

  # Generated (not committed) formatter configs (#72, klangk
  # pattern): enterShell writes .prettierignore so hand-run prettier
  # invocations skip the same trees the hook excludes. The lint
  # configs that klangk generated here (.markdownlint.yaml,
  # .yamllint.yml) are expressed natively in git-hooks settings with
  # this (newer) pin — no files needed.
  enterShell = ''
    cat > "$DEVENV_ROOT/.prettierignore" <<'PRETTIER'
    # Lock files are machine-managed (devenv/uv regenerate them);
    # prettier's --ignore-unknown also skips them, this keeps direct
    # prettier runs quiet too.
    *.lock
    .devenv/
    PRETTIER
    # The client presets (#231): resolved here, per shell entry,
    # from the DEV DAEMON's state dir — ".devenv/state/msksd" by
    # default; MSKSD_STATE_DIR relocates it (the same documented var
    # the daemon script, msks-dev-ready, and the image builder
    # honor). Per-shell resolution, not env.*: the
    # token rotates, the CA materializes after the daemon's first
    # boot. A file that does not exist yet leaves its variable
    # untouched, so a value exported before entering the shell
    # survives; otherwise the preset wins (unset it inside the
    # shell to override).
    state="''${MSKSD_STATE_DIR:-$DEVENV_ROOT/.devenv/state/msksd}"
    # A relative override resolves below the repo root — the
    # exported MSKSC_CAFILE must be absolute, or the client would
    # resolve it against its own CWD.
    case "$state" in
    /*) ;;
    *) state="$DEVENV_ROOT/$state" ;;
    esac
    if [ -s "$state/bootstrap-token" ]; then
      export MSKSC_TOKEN="$(cat "$state/bootstrap-token")"
    fi
    if [ -s "$state/port" ]; then
      export MSKSC_URL="https://127.0.0.1:$(cat "$state/port")"
    fi
    if [ -s "$state/msks-ca.pem" ]; then
      export MSKSC_CAFILE="$state/msks-ca.pem"
    fi
    # Per-checkout client state (#251): the client's host-key cache
    # and minted identities default to the user's XDG roots, which
    # every checkout shares — a stale entry one tree wrote, another
    # tree reads (the #251 failure shape: an unstamped known_hosts
    # from an older tree refuses a newer tree's ssh). Both variables
    # name the msks root itself; the client creates what it needs
    # below them. Unlike the daemon-material presets above (the
    # preset wins), these are directories with nothing to wait for,
    # so a non-empty value exported before entering the shell
    # survives (an empty one counts as unset, the same rule the
    # client applies) and the per-worktree default fills the rest.
    # Workspaces created before this preset keep their minted
    # halves under the old root; their identities move with the
    # worktree's state — deleting the worktree deletes them along
    # with the daemon catalog they belong to.
    : "''${MSKSC_CACHE_DIR:=$DEVENV_ROOT/.devenv/state/msksc/cache}"
    : "''${MSKSC_DATA_DIR:=$DEVENV_ROOT/.devenv/state/msksc/data}"
    export MSKSC_CACHE_DIR MSKSC_DATA_DIR
    # Tidy the state tree (#156): every `devenv shell --` /
    # `devenv tasks run` invocation writes a one-shot wrapper
    # (shell-<hash>.sh, ~150KB) at the top of .devenv/ and leaves it
    # there. The wrapper execs away within milliseconds, so only the
    # just-written current one is ever young — anything past an hour
    # is stale by any measure and goes.
    find "$DEVENV_ROOT/.devenv" -maxdepth 1 -name 'shell-*.sh' -mmin +60 \
      -delete 2>/dev/null || true
  '';
}
