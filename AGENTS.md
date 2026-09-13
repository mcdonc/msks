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

## Coverage gates

Local `unit-tests` reproduces the CI coverage gate exactly at the
same tree (#27). Run it on the tree being pushed: commit first, or
confirm `git status --porcelain` is empty. Any change after the last
run — including a post-review `--amend` — requires re-running it. If
CI reports a gap local runs missed, the gap is real: check out the
failing commit and write the pinning test.

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

