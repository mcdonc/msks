{
  pkgs,
  config,
  lib,
  ...
}:

{
  # msks dev environment: Python 3.14 + cloud-hypervisor toolchain (#2).
  # Mirrors the klangk conventions (AGENTS.md): CI-identical test task,
  # testmon for scoped iteration, xenon rank-A gate via a single script
  # shared with the pre-commit hook.
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

  packages =
    with pkgs;
    [
      bash # explicit bash for shell scripts (CI /bin/sh may be dash)
      cloud-hypervisor # VMM driven by the local backend (#1); ships ch-remote
      curl # unix-socket REST poking during CH debugging
      e2fsprogs # debugfs: seed the bootstrap token onto the state disk
      iproute2 # the appliance bridge/tap (supervisor scripts; host-agnostic)
      nftables # egress chains/NAT for the #52 smoke path
      qemu # qemu-img for rootfs conversion during guest-image experiments
      virtiofsd # the appliance's read-only /nix/store share (#10)
      ruff
      socat # AF_UNIX <-> pty/stdio plumbing for CH socket debugging
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

  # The msks client (#21) targets the local appliance by default, so
  # `msks shell <id>` works from any devenv shell with no exports.
  env.MSKSC_URL = "https://192.168.77.2:8660";
  # The bootstrap token is composed in nix from the file
  # appliance-setup.sh seeds: read at evaluation time, so each
  # `devenv shell` picks up a rotated token. Before the first
  # `devenv processes up` the file does not exist and the variable is
  # empty — the client names the missing env. Explicit exports win.
  env.MSKSC_TOKEN = lib.optionalString (builtins.pathExists ./.appliance/bootstrap-token)
    (lib.removeSuffix "\n" (builtins.readFile ./.appliance/bootstrap-token));

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
    # The complexity gate as a one-word task: delegates to
    # scripts/xenon-gate.sh, the single definition of the invocation
    # (thresholds + graded file set) that the pre-commit hook also runs.
    "msks:xenon" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/xenon-gate.sh" "$@"'';
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
    # Guest VM assets out of the pinned nixpkgs, no manual downloads
    # (#5). ${pkgs.path} is the nixpkgs source the devenv lock itself
    # evaluated — the guest toolchain cannot drift from the dev shell,
    # and the build needs nothing from the host but nix.
    "msks:build-guest" = {
      description = "Build the microvm guest assets (kernel, initrd, ext4 rootfs) into .guest/";
      exec = ''
        exec env MSKS_GUEST_NIXPKGS=${pkgs.path} bash "$DEVENV_ROOT/scripts/build-guest.sh"
      '';
    };
    "msks:build-runner-image" = {
      description = "Build the k8s vm-runner container image archive into .guest/";
      exec = ''
        exec env MSKS_GUEST_NIXPKGS=${pkgs.path} bash "$DEVENV_ROOT/scripts/build-runner-image.sh"
      '';
    };
    "msks:demo-vm" = {
      description = "Boot one microvm from the built guest assets (serial console on this terminal)";
      exec = ''exec bash "$DEVENV_ROOT/scripts/demo-vm.sh"'';
    };
    # The appliance host supervisor (#25): the long-running pieces are
    # devenv PROCESSES, owned by the environment's own process manager
    # (restart on crash, logs, clean teardown) — see `processes` below.
    # The tasks are thin conveniences over the process manager. The one
    # privileged step (bridge/tap create) is a documented one-time sudo
    # inside appliance-setup.sh.
    "msks:appliance-build" = {
      description = "Build the msksd appliance image into .appliance/";
      exec = ''
        exec env MSKS_GUEST_NIXPKGS=${pkgs.path} bash "$DEVENV_ROOT/scripts/build-appliance.sh"
      '';
    };
    "msks:appliance-up" = {
      description = "Start the appliance processes (virtiofsd + the VM), detached";
      exec = ''exec devenv processes up -d'';
    };
    "msks:appliance-down" = {
      description = "Stop the appliance processes (graceful ACPI via the run script's TERM trap)";
      exec = ''exec devenv processes down'';
    };
  };

  # The appliance as ONE supervised process (#25): `devenv processes
  # up` (or the msks:appliance-up task) starts it; the process manager
  # owns restart and teardown. The store-share daemon is a child of
  # the run script, not its own process: virtiofsd is vhost-user 1:1
  # with the VM — it exits when the client disconnects — so the pair
  # shares one lifecycle, and a crash-restart brings both back.
  processes = {
    appliance = {
      exec = ''bash "$DEVENV_ROOT/scripts/appliance-run.sh"'';
      # The run script's stop choreography (ACPI, then a bounded
      # SIGTERM wait) needs up to ~10s; the supervisor's default
      # SIGKILL grace is 5 — a busy guest would be hard-killed
      # mid-poweroff otherwise.
      shutdown.grace = 15;
    };
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
  };
}
