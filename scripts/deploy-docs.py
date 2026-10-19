#!/usr/bin/env python3
"""Deploy versioned documentation to the gh-pages branch with
mike (#573, the klangk pattern).

Zensical has no native versioning support; the official bridge is
squidfunk's fork of mike (installed from GitHub in CI — it is not
published on PyPI). This script drives that fork's Python API:

1. sync the local gh-pages branch from origin (the branch
   itself is created by mike's first deploy — without the sync,
   deploying would clobber every earlier version),
2. build the docs with ``zensical build``, with
   ``MIKE_DOCS_VERSION`` exported so the site prefixes its URLs
   with the version,
3. commit ``site/`` into a versioned subdirectory of gh-pages,
   refresh the ``latest`` alias as HTML redirect pages (GitHub
   Pages does not serve symlinks), and rewrite ``versions.json``,
4. write a root ``index.html`` redirecting to the ``latest``
   alias.

The version comes from the ``MIKE_DOCS_VERSION`` environment
variable (set by the docs workflow from the pushed tag, or
``dev`` for main's in-flight view). Run from the repository root;
the caller pushes gh-pages afterwards.
"""

from __future__ import annotations

import os
import sys

from mike import commands, git_utils, utils

GH_PAGES_BRANCH = "gh-pages"
LATEST_ALIAS = "latest"
DEV_VERSION = "dev"


def sync_gh_pages() -> None:
    """Create or fast-forward the local gh-pages branch from origin."""
    git_utils.update_from_upstream("origin", GH_PAGES_BRANCH)


def deploy_version(version: str) -> None:
    """Build the site and commit it as a versioned gh-pages
    deployment. A release deploy holds the ``latest`` alias; the
    dev view never does — main pushes far outnumber tags, and an
    unconditional alias update would hand the site's root to
    unreleased docs minutes after every merge."""
    cfg = utils.load_config()
    aliases = [] if version == DEV_VERSION else [LATEST_ALIAS]
    with commands.deploy(
        cfg,
        version,
        aliases=aliases,
        update_aliases=True,
        alias_type=commands.AliasType.redirect,
    ):
        utils.build(None, version)


def set_default_redirect(version: str) -> None:
    """Point the gh-pages root index.html at the latest alias —
    or, before any release has created it, at the deployed view:
    the first deploy is main's dev, and the root must land
    somewhere."""
    try:
        commands.set_default(LATEST_ALIAS)
    except ValueError:
        commands.set_default(version)


def main() -> int:
    version = os.environ.get("MIKE_DOCS_VERSION", "")
    if not version:
        print(
            "MIKE_DOCS_VERSION is not set — pass the docs version to "
            "deploy (e.g. 1.2.3, or dev for the in-flight view)",
            file=sys.stderr,
        )
        return 1

    sync_gh_pages()
    try:
        deploy_version(version)
        set_default_redirect(version)
    except git_utils.GitEmptyCommit:
        # Re-deploying unchanged docs makes mike roll back the empty
        # commit; that is a successful no-op, not an error.
        print("docs unchanged since the last deploy — nothing to commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
