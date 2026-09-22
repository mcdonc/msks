# msks agent instructions

Project-specific guidance for coding agents working in this repo.

## Prefix most commands with `devenv --quiet shell --`

Msks uses [devenv](https://devenv.sh) (Nix-based) for its dev environment. **Every command that touches the project toolchain must be run through the devenv shell**, including git. The toolchain — Python venv, Node, Dart/Flutter, podman, pre-commit hooks, etc. — only exists inside the shell.

```bash
devenv --quiet -- git commit -m "..."
devenv --quiet -- pytest
```

The flags: `--quiet` suppresses noisy devenv output; `-O dotenv.enable:bool false` prevents devenv from loading `.env` (which can interfere with test environments that set their own env vars via monkeypatch). `shell --` launches an ephemeral shell with the full environment, runs the command, and exits — this is the pattern agents should use for one-off commands. (`devenv shell` with no `--` drops into an interactive shell; not useful for non-interactive agents.) This applies to **all** commands: builds, tests, lint, `git`, `podman`, `flutter`, `gh`.

A long-running interactive `devenv up` (backend + proxy + workspace image build) is a human-facing workflow; agents generally don't run it. If you need the backend up for something, ask.

## Never run project Python on the ambient interpreter

`python3` outside the devenv shell is whatever the host happens to
provide — here 3.13 — while the project pins 3.14 (`devenv.nix`,
`languages.python.package`). The gap is not cosmetic: 3.14 changed the
grammar (PEP 758 permits `except A, B:` without parentheses), so code
that imports and runs on the project interpreter is a `SyntaxError` on
3.13. A bare `python3 -c` (or `python3 - <<EOF`) used to inspect,
parse, or run anything from this repo — even a one-liner like
`ast.parse(open(f).read())` — yields false failures or validates against
the wrong semantics. Every Python invocation, however small, goes
through the shell:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- python -c "..."
```

Inside the shell, `python` and `python3` both resolve to the venv's
3.14 with the project's dependencies installed.

## Process manager: devenv 2.x native (not process-compose)

`devenv processes up` / `devenv up` use **devenv 2.x's built-in process manager**,
not process-compose. Consequences when debugging a managed stack:

- `devenv processes list|status|logs|restart <NAME>` work without a separate
  `process-compose` daemon running — there is no `process-compose` binary or
  socket to look for. `ps` will **not** show a `process-compose` process; the
  manager is devenv itself.
- A crashed process is restarted by devenv's own supervisor (the journal shows
  `Process exited (Failure), restarting` / `Restarted (attempt N)`), and after
  enough attempts the whole `devenv processes up` invocation exits.
- On hosts that run the stack under systemd, the unit's `ExecStart` is
  `devenv processes up` (foreground, `DEVENV_TUI=false`); a crash loop in one
  process takes the unit down. Debug by running the suspect process directly
  under the devenv shell (bypassing the supervisor) to see its real stderr.

Background lifecycle semantics (all verified live):

**The supervised process is the dev-mode msksd (#231)** — `processes
msksd`, nothing else. The process exec runs
`scripts/dev-daemon.sh` (also the `msks-dev` hand entry point): it
seeds the worktree's daemon state (bootstrap token, API port, the
port-derived egress subnet) and execs msksd through the host's
`msks-caps` capability wrapper. `devenv processes up` runs it
attached (foreground — the user never backgrounds it), and
`devenv processes up/down/restart/logs msksd` manage the same
process, with a 90s shutdown grace covering a running workspace's
stop cycle. A state-dir `flock` refuses a second daemon on the same
catalog.

- `devenv processes up -d` starts the manager detached — it survives
  the shell that launched it, and a second `up -d` is a no-op.
- `devenv processes down` (from any fresh shell) stops gracefully:
  the manager TERMs the msksd process, whose shutdown path stops
  running workspaces inside the 90s grace.
- The client env presets to the dev daemon (`MSKSC_URL`/`TOKEN`/
  `CAFILE` from the worktree's daemon state dir,
  `.devenv/state/msksd` by default — `MSKSD_STATE_DIR` relocates
  it, and the daemon writes `msks-ca.pem` there on first serve, so
  a fresh shell verifies).
- A crashed msksd crash-restarts under the supervisor; a
  repeatedly-failing process reaches `gave_up` after five restarts
  (`devenv processes logs` shows why).
- If the manager daemon itself dies while msksd runs, the
  per-process **scope guardian** (its config lives under
  `.devenv/run/processes/guardians/`) TERMs the whole process tree
  with the process's grace — the daemon stops gracefully, it does
  NOT keep running unsupervised (probe-verified on devenv 2.3.1 in a
  throwaway project: a SIGKILL'd manager took the tree down within
  seconds). `devenv processes down` afterwards reports "No process
  manager is running" because nothing is left.

## Naming: no leading underscores on helper functions

Module-level helper functions are named without a leading underscore
(`resolve_boot`, not `_resolve_boot`). The prefix buys nothing at
module scope — there are no star imports and the modules are small —
and the complexity-gate splits create many single-use helpers where
it reads as ceremony. Underscores stay on names that genuinely
shadow or collude with builtins.

## Coverage gates

Local `unit-tests` reproduces the CI coverage gate exactly at the
same tree (#27). Run it on the tree being pushed: commit first, or
confirm `git status --porcelain` is empty. Any change after the last
run — including a post-review `--amend` — requires re-running it. If
CI reports a gap local runs missed, the gap is real: check out the
failing commit and write the pinning test.

## Pre-flight the commit gates before the first commit attempt

The pre-commit hooks run their gates over the staged tree at commit
time; each rejection costs a full edit → test → commit round, and
reading one gate's failure at a time grows a one-offender-per-round
loop. `msks-preflight` delivers the same feedback before the first
commit attempt, all offenders at once:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- msks-preflight
```

It prints every ruff violation, every deferred import, every xenon
block above rank A, and the jscpd clone report over the full tree.
When anything under `src/msks/` differs from the fork point on
`origin/main` (committed or working tree), it then runs the gated
suite — the `unit-tests` suite under the same coverage gate, with
quiet output — so a green coverage section satisfies the rule above
— and prints every missing line and
branch arc for the changed sources (`scripts/covgaps.py` reads the
`.coverage` the suite leaves; `bash scripts/preflight.sh --fast`
skips the suite for an instant lint/complexity pass).

The working rule: run the pre-flight after writing code, fix
EVERYTHING it names in one editing pass, re-run, then commit. A green
pre-flight means the Python gates hold at commit time; the doc, nix,
shell, and yaml hooks still run there, and their failures name one
file and one rule apiece — a single extra round settles them.

Triage a coverage gap at write time, not after a red gate:

- Reach for deterministic cover first: a barrier/threading fixture
  for concurrency-order branches, rigged filesystem state for error
  paths (the suite has precedent for both).
- A branch the test host cannot reach (platform-specific, or only
  schedulable through a race the suite cannot arrange) takes
  `# pragma: no cover` with a comment naming the reason.

## TUI spatial navigation (no focus traps)

The textual TUI must use **spatial navigation** — arrow keys move focus
between logical areas (tab strips, lists, form fields) without requiring
Tab/Shift-Tab to cross boundaries. Never create **focus traps** (a
composite widget that swallows all keys and won't release focus without
Tab). Specifically:

- Down from a tab strip or section header enters the list/pane below it.
- Up from the first row of a list returns focus to the tab strip above.
- Left/Right move between sibling columns or tab pages.
- Tab/Shift-Tab still works as a fallback, but arrows must always be
  sufficient to reach any element in reading order (top-to-bottom,
  left-to-right).
- When implementing a new screen or widget, add the key bridges that let
  arrows cross its boundaries.

## FIPS readiness: crypto choices are settings and platform defaults (#115)

FIPS certification is a future requirement for some deployments. Msks
compiles stock components, and every crypto choice ships as a setting
or a platform default — a later certification effort then changes
configuration and defaults, not architecture. Work that touches
crypto keeps these rules:

- **Algorithm selection belongs to the platform's crypto library**
  (the OpenSSL 3 line Debian ships). msks pins nothing: configs,
  dropins, shipped docs, and product tests stay free of
  `Ciphers`, `KexAlgorithms`, `MACs`, `HostKeyAlgorithms`, and
  `PubkeyAcceptedAlgorithms` lists. Policy config — authentication,
  addresses, timeouts — is the kind that ships.
- **Key types are `MSKSD_*` settings, and defaults may change.**
  #111's minted identity defaults to Ed25519 (#138: FIPS 186-5
  approves EdDSA, and the type is accepted by ssh clients
  restricted to the common `ssh-ed25519,ssh-rsa` set), with ECDSA
  P-256 and RSA as choices for validated crypto modules that
  predate EdDSA. Filenames, storage
  paths, and wire formats treat the key type as opaque — changing
  the setting is the whole of a key-type change.
- **The guest's crypto libraries are Debian's own.** The rsync deb,
  not a statically linked third-party build, is the pattern: a static
  crypto build inside the image would step outside the platform's
  certification path.
- **Docs describe behavior, not algorithms.** Where an example names
  a key type, it names the current default and presents it as the
  default, not a requirement.

Token hashing is sha256 today — FIPS-approved, no action. The
daemon's TLS (#8) uses Python's `ssl` defaults; a pinned suite list
would undo the same posture. The acceptance check is a tree-wide
grep for the five ssh directives above: every match is a violation
except this section itself, which names them to make the check
copy-pasteable.

## Prose writing: state what the feature does

In docs, docstrings, config comments, GitHub issue and PR bodies, and
review or issue comments that document operator-facing behavior,
describe what a mode or setting **does**, not a list of what it lacks.
"No public name needed, no ACME account, no ports 80/443, no
redirect" is negative parallelism — the reader must reconstruct the
behavior from its absences, and the list drifts into jargon
compression ("no ACME account" instead of "msks does not contact a
certificate authority"). Write the positive statements instead:

```markdown
Wrong: - **No HTTP→HTTPS redirect** — the outer proxy owns port 80
and does its own redirecting; msks disables the automatic
one.
Right: - **The HTTPS listener binds `listen:port`, and that is the
only port involved.** The automatic HTTP→HTTPS redirect stays
off: your outer proxy already sends browsers to HTTPS, and an
enabled redirect would try to bind port 80 — a bind that
fails when msksd runs as an unprivileged user.
```

When an absence _is_ the behavior (a redirect that stays off, a
setting that is unused), state it as a fact and give the reason —
not as a headline "No X" bullet. Use concrete nouns over shorthand:
"certificates are stored in `<state_dir>/caddy-storage`", not
"leaves live under the storage path". Applies to `docs/` prose, the
env-var reference rows, the first-run config template comments, and
issue/PR bodies and comments alike. Docs-chapter diffs and PR
descriptions that introduce a run of "No …" bullets are a review
flag — rewrite before merging.

## Changelog (`docs/changes.md`)

**Entries are frozen for the duration of the #229 deployment-host
rework.** While #229's sub-issues are in flight, leave
`docs/changes.md` untouched: the parallel branches all append to the
same `## \[Unreleased]` section, so every landing produces a conflict
to resolve by hand, and per-PR entries written mid-rework describe a
tree the rework is about to delete. When the rework lands, cut a
single `Changed` entry covering the whole arc (the appliance layer removed,
deployment-host module added, k8s retired) in the PR that closes
issue #229. The rules below govern entries outside the freeze.

`docs/changes.md` is the single source of truth for human-authored release notes,
formatted as [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version
headings escape the opening bracket (`## \[Unreleased]`, `## \[v1.2.3] - date`)
so the docs build does not parse them as Markdown link references (#3142); they
render as plain `## [Unreleased]`-style headings. The file has two
rendering surfaces:

- **Docs site** — the whole file renders as one page at `/changes/`, sidebar entry
  "Changelog" (nav is in `zensical.toml`). Includes the `## [Unreleased]`
  section, so in-flight work is visible.
- **Release tab** — when a `v*` tag is pushed, `release.yml` checks out the code
  **at the tag**, extracts that version's `## [<version>]` section, and prepends it
  to GitHub's auto-generated notes (PR list + compare link).

### When to add an entry

Add a bullet under `## \[Unreleased]` **in the same PR that introduces the change**
(not as an afterthought, not after merge). Use the matching subsection:

- **Added** — new feature, config var, CLI flag, endpoint.
- **Changed** — change to existing behavior, default, or signature.
- **Deprecated** — soon-to-be-removed.
- **Removed** — now removed.
- **Fixed** — notable bug fix.
- **Security** — vulnerability fix.
- **Breaking** — sub-section under any version for changes requiring operator/integrator
  action on upgrade. Call out the migration.

Each entry must be **2–4 sentences max**. Lead with the **bold setting or feature
name**, then the issue number in parens. State what changed and what operators
need to know. Link to docs if they exist. Do not include internal justification
("because X was broken"), migration history, implementation notes (module names,
internal APIs, code paths), or test infrastructure detail. Only explain old
behavior if the operator must act (Breaking section).

Example:

```markdown
- **`MSKSD_DNS_SEARCH` (#2055).** Comma-separated DNS search domains
  passed to workspace containers via `--dns-search`. Reloadable on SIGHUP.
```

Add an entry for anything an **operator, integrator, or end user** would notice:
new/changed config or defaults, behavior changes, security fixes, notable fixes,
new features.

**Skip** entries for: pure internal refactors (moving code between modules,
renaming internal classes/variables, restructuring state objects), test/CI/doc
churn with no user-visible effect, and dependency bumps that don't change
behavior. Internal architecture changes (e.g. "X is now a class instead of free
functions", "Y now takes app instead of app_state") are invisible to users and
create noise — do not add changelog entries for them.

### When to garden for a release

Right before pushing the tag — do this as its own commit on `main`:

1. Rename `## \[Unreleased]` → `## \[vX.Y.Z] - YYYY-MM-DD`
   (today's date). The `v` prefix and bracket form **must match the tag exactly**;
   the `- YYYY-MM-DD` date suffix is optional but conventional. Escape the
   opening bracket (`\[`) as shown — it keeps the docs build warning-free
   (#3142). The workflow matches the section heading as a prefix and strips
   backslashes first, so `## \[v1.0.5] - 2026-07-07` matches tag `v1.0.5` (a
   bare `## [v1.0.5]` would still match, but don't write it that way).
2. Insert a fresh, empty `## \[Unreleased]` heading directly above it.
3. Commit, e.g. `chore(changelog): cut vX.Y.Z`.
4. Tag and push: `devenv --quiet -O dotenv.enable:bool false shell -- git tag vX.Y.Z && devenv --quiet -O dotenv.enable:bool false shell -- git push origin vX.Y.Z`.

**Critical sequencing:** `release.yml` checks out `docs/changes.md` at the tagged
commit, so the `[Unreleased]` → `[vX.Y.Z]` rename **must land in (or before) the
commit you tag**. If you tag a commit that still has the changes under
`[Unreleased]`, the workflow finds no `## [vX.Y.Z]` section and the release body
falls back to pure auto-generated notes — the human-authored section is silently lost.

### After a release

Nothing to do in `docs/changes.md` itself — the `[Unreleased]` heading you created
at cut time is already in place for the next cycle's entries. Just start adding new
entries under it.

## Worktrees

- When asked to create a worktree, put the worktree inside the repository
  root's `.worktrees` subdirectory. When using a worktree, do not commit
  anything to the main branch or use the main repository to commit anything —
  all commits go on the worktree's own branch within the worktree.
- Worktrees should have a directory name no longer than 16 characters.

## Pull request titles

- Every PR created to address a GitHub issue must include that issue's
  number in the PR title, as a parenthesized suffix — e.g.
  `Add DNS search domains (#2055)`. GitHub appends the PR's own number
  at squash-merge time, so the issue number in the title is what ties
  the PR back to its originating issue in lists, history, and
  notifications.
- This applies to every PR opened from a `gh issue` (via `/workon`,
  `/stackon`, or directly) — check the title before `gh pr create`.

## Closing the issue on merge

- The PR body must contain the exact phrase `Closes #<issue>` (or
  `Fixes`/`Resolves #<issue>`) for the originating issue. GitHub closes the
  issue automatically only on those keyword forms; near-misses such as
  "closing #3447" do nothing, and the issue stays open after the merge
  until someone closes it by hand.
- Put the phrase in its own sentence in the Summary (e.g. "Closes #2055."),
  and check the issue state after merging; if it is still open, close it
  with a comment recording the landing commits.
