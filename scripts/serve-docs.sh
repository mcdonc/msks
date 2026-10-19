#!/usr/bin/env bash
# Serve the docs site locally (#591): what the Deploy Docs
# workflow would publish, re-rendered in realtime while the
# docs change.
#
# The site is the MAIN branch's docs — the same content the
# deployed dev view carries — so the script serves only from a
# checkout whose HEAD is main (a worktree on a feature branch
# refuses with the rule named; check out main, or merge, first).
# With the tree on main, `zensical serve` watches the docs and
# the config and rebuilds on every change, and the browser tab
# reloads itself: edit a chapter, save, watch the page rerender.
#
# zensical is a docs tool, not a project dependency, so it lives
# in a script-managed venv under the devenv state dir — pinned
# to the version .github/workflows/docs.yml pins, created on
# first run and reused after.
#
# Environment:
#   DOCS_SERVE_PORT  the port to bind (default 8923)
set -euo pipefail

root="${DEVENV_ROOT:?run me from the devenv shell (msks-docs)}"
port="${DOCS_SERVE_PORT:-8923}"

# The site is main's docs: refuse anything else, naming the rule
# (the deployed dev view is built from main, and so is this).
branch="$(git -C "$root" rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "main" ]; then
  echo "msks-docs: the docs site serves the main branch's docs" \
    "(this checkout is on '$branch')" >&2
  echo "             check out main (git checkout main && git pull)" \
    "or merge this branch first" >&2
  exit 1
fi

# The toolchain venv: pinned beside the devenv's other tool
# state, keyed to the pinned version (a version bump rebuilds
# it on the next run).
version="0.0.71"
venv="$root/.devenv/state/docs-venv"
if [ ! -x "$venv/bin/zensical" ] ||
  ! "$venv/bin/zensical" --version 2>/dev/null | grep -q "$version"; then
  echo "msks-docs: preparing the zensical==$version venv under $venv" >&2
  rm -rf "$venv"
  # The base interpreter, not the shell's active venv: a nested
  # venv inherits the parent's site-packages in pip's resolver,
  # and the docs toolchain owns nothing of the project's.
  base="$(python3 -c 'import sys; print(sys.base_prefix)')"
  "$base/bin/python3" -m venv "$venv"
  "$venv/bin/pip" install --quiet "zensical==$version"
fi

cd "$root"
echo "msks-docs: building (strict — a broken link fails here)" >&2
"$venv/bin/zensical" build >/dev/null

echo "msks-docs: serving main's docs at http://127.0.0.1:$port/" \
  "(edits re-render in realtime; Ctrl-C stops)" >&2
exec python3 "$root/scripts/serve-docs.py" \
  "$root/site" "$root/docs" \
  "[\"$venv/bin/zensical\", \"build\"]" "$port"
