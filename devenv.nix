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
in
{
  # msks dev environment: Python 3.14 + cloud-hypervisor toolchain (#2).
  # Mirrors the klangk conventions (AGENTS.md): CI-identical test task,
  # testmon for scoped iteration, xenon rank-A gate via a single script
  # shared with the pre-commit hook.
  # Rust for the guest-side console helper (#63): src/console-helper
  # builds locally with this toolchain (cargo test, the coverage
  # gate) and in the guest image via rustPlatform + static glibc
  # (nix/guest-assets.nix). Nightly because branch coverage
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
    e2fsprogs # debugfs: seed the bootstrap token onto the state disk
    cdrtools # genisoimage: the #41 cidata seed disks (iso9660)
    iproute2 # the appliance bridge/tap (supervisor scripts; host-agnostic)
    iptables # diagnose foreign FORWARD drops (docker's policy on CI runners)
    # that block the egress forward path the nft rules accept (#75/#52)
    jscpd # token-clone scanner (#71), pinned rust binary (see above)
    nftables # egress chains/NAT for the #52 smoke path
    openssh # host-side ssh client: the forward-path smoke (#110) and
    # the documented ssh workflow (#112) run over `msks forward`
    qemu # qemu-img for rootfs conversion during guest-image experiments
    rsync # host-side rsync over the forward (#110's sync path)
    virtiofsd # the appliance's read-only /nix/store share (#10)
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
  ];

  env.UV_PYTHON = config.languages.python.package;

  # The msks client (#21) targets the APPLIANCE (#146) by default,
  # so `msks ls` / `msks ssh` work from any devenv shell with no
  # exports. The token and CA are resolved per shell in enterShell
  # (below), from the appliance state dir — `.devenv/state/appliance`
  # by default, relocated with MSKS_APPLIANCE_DIR. Until the
  # appliance's first boot the token and CA files do not exist, so
  # those presets stay unset (a value exported before entering the
  # shell survives), the client warns it does not verify, and the
  # TOFU fingerprint on the serial log covers the first connect.
  env.MSKSC_URL = "https://192.168.77.2:8660";

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

  # The bare-host daemon by HAND (#146): no managed process — run
  # `msksd` from a devenv shell when the appliance is not wanted
  # (no KVM, API/client-only work). The `msks-dev-ready` script
  # converges a workable state under the daemon state dir first (token,
  # default image; .devenv/state/msksd by default, MSKSD_STATE_DIR
  # relocates it); egress stays off (that is the appliance's job, #101) and the
  # client env presets target the appliance, so point the client at
  # the bare daemon with explicit exports (README has the recipe).
  processes = {
    # The appliance is THE process (#146): `devenv processes up`
    # boots the deployed shape — egress, the bridge, nested
    # workspaces, and (with MSKS_DEV_TREE set) the live dev tree of
    # #144 — and the client env below presets to it. The bare-host
    # msksd is no longer a managed process at all; run it by hand
    # when the appliance is not wanted (README has the one-liner).
    appliance = {
      exec = ''
        # One startup, one build, one boot (#166): the build script runs
        # directly — no nested `devenv tasks run` CLI (whose own
        # lock/shell evaluation doubled the startup output and cost a
        # second, sometimes cold, devenv evaluation) and no task cache
        # to go stale. The script is idempotent: unchanged inputs make
        # it a quick no-op (cached nix eval + build check, seconds), and
        # a pull, an edit to the sources, or the nix expressions
        # rebuilds the image (minutes from a cold store, ~20s warm) —
        # `devenv processes up` is the whole update story. The script
        # always re-lands every artifact INCLUDING the image symlink,
        # so a deleted or half-deleted appliance state dir heals on the
        # next boot (#160) instead of crash-looping on a missing or
        # bogus symlink. The export feeds the initial build AND the
        # drift watcher inside the run script.
        export MSKS_GUEST_NIXPKGS=${pkgs.path}
        bash "$DEVENV_ROOT/scripts/build-appliance.sh"
        exec bash "$DEVENV_ROOT/scripts/appliance-run.sh"
      '';
      # A persistent failure inside this exec (the build above all)
      # crash-restarts up to the manager's default five attempts
      # before gave_up — five consecutive multi-minute build tries
      # where the task era failed once. The loud loop in
      # `processes logs appliance` is the accepted diagnostic.
      #
      # The run script's stop choreography is ACPI-first with a
      # 60s window (a workspace running inside the appliance needs
      # its own nested stop cycle; a shorter window lost
      # page-cache-only sqlite commits, observed live). The grace
      # must cover that window plus margin, or a busy guest is
      # hard-killed mid-poweroff — the data-loss class again.
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
    description = "Build the microvm guest assets (kernel, initrd, ext4 rootfs) into the guest state dir (.devenv/state/guest; MSKS_GUEST_DIR relocates it)";
    exec = ''exec env MSKS_GUEST_NIXPKGS=${pkgs.path} bash "$DEVENV_ROOT/scripts/build-guest.sh" "$@"'';
  };

  scripts.msks-build-runner-image = {
    description = "Build the k8s vm-runner container image archive into the guest state dir (.devenv/state/guest; MSKS_GUEST_DIR relocates it)";
    exec = ''exec env MSKS_GUEST_NIXPKGS=${pkgs.path} bash "$DEVENV_ROOT/scripts/build-runner-image.sh" "$@"'';
  };

  scripts.msks-demo-vm = {
    description = "Boot one microvm from the built guest assets (serial console on this terminal)";
    exec = ''exec bash "$DEVENV_ROOT/scripts/demo-vm.sh" "$@"'';
  };

  scripts.msks-appliance-build = {
    description = "Build the msksd appliance image into the appliance state dir (.devenv/state/appliance; MSKS_APPLIANCE_DIR relocates it)";
    exec = ''exec env MSKS_GUEST_NIXPKGS=${pkgs.path} bash "$DEVENV_ROOT/scripts/build-appliance.sh" "$@"'';
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
        # 256 bits of urandom, hex: the same shape the appliance's
        # setup seeds. Stable across restarts — the daemon inserts
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

  # The appliance lifecycle scripts (#146): thin wrappers over the
  # process manager, which owns the appliance process below — `up -d`
  # detached (a second up while the manager lives is a no-op;
  # environment surgery reaches the VM through the manager's
  # inherited environment, verified live), `down` as the graceful
  # stop. The host network (bridge, tap, forwarding, NAT) is
  # installed once as root by scripts/appliance-host-setup.sh and
  # re-armed at every host boot by its systemd unit;
  # appliance-setup.sh only verifies it, so starting the appliance
  # needs no sudo.
  scripts.msks-appliance-up = {
    description = "Start the appliance under the process manager, detached (conditional build first)";
    exec = ''
      # The lifecycle has ONE owner: the process manager (#146).
      # This script is the detached entry point — the manager runs
      # the appliance process (idempotent build, then the run
      # script) in its own session; a second up is a no-op while
      # the manager lives. Foreground alternative: `devenv
      # processes up appliance`.
      echo "msks: starting the appliance detached (idempotent build first, then the run script)"
      exec devenv processes up -d
    '';
  };

  scripts.msks-appliance-down = {
    description = "Stop the appliance through the process manager (graceful ACPI)";
    exec = ''
      # The manager TERMs the appliance process; the run script's
      # ACPI-first trap owns the teardown inside the process's
      # 90s shutdown grace. Exit codes: 0 stopped a live manager;
      # 1 with "No process manager is running" when nothing is up
      # (the first down also stops the manager, so a second down
      # reports that — callers treat it as stopped).
      echo "msks: stopping the appliance (graceful ACPI through the process manager)"
      exec devenv processes down
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
    # The client presets (#146, #156): resolved here, per shell
    # entry, from the appliance state dir — ".devenv/state/appliance"
    # by default; MSKS_APPLIANCE_DIR relocates it (every appliance
    # script resolves the same way, so the presets follow). Per-shell
    # resolution, not env.*: the token file rotates, the CA
    # materializes after the appliance's first boot (the run script
    # extracts the guest's msks-ca.pem from the state disk), and a
    # rebuild swaps the image symlink without re-evaluating nix —
    # baked presets would go stale on all three. A file that does
    # not exist yet leaves its variable untouched, so a value
    # exported before entering the shell survives; otherwise the
    # preset wins (unset it inside the shell to override).
    app_dir="''${MSKS_APPLIANCE_DIR:-$DEVENV_ROOT/.devenv/state/appliance}"
    # A relative override resolves below the repo root — the
    # exported MSKSC_CAFILE must be absolute, or the client would
    # resolve it against its own CWD.
    case "$app_dir" in
    /*) ;;
    *) app_dir="$DEVENV_ROOT/$app_dir" ;;
    esac
    if [ -s "$app_dir/bootstrap-token" ]; then
      export MSKSC_TOKEN="$(cat "$app_dir/bootstrap-token")"
    fi
    if [ -s "$app_dir/msks-ca.pem" ]; then
      export MSKSC_CAFILE="$app_dir/msks-ca.pem"
    fi
    # The appliance-image drift check (#160): what THIS checkout's
    # appliance state dir points at — `msks ls` compares it with the
    # running daemon's reported image (its /health) and names the
    # drift with the fix.
    export MSKSC_EXPECTED_IMAGE="$(readlink -f "$app_dir/image" 2>/dev/null || true)"
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
