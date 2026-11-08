#!/usr/bin/env bash
# Push a release cut in the order the docs deploy needs (#805).
#
# The cut's pushes race when they land together: the tag's Deploy
# Docs run (the versioned snapshot and the `latest` alias) and
# main's dev-view runs all queue in the workflow's `gh-pages`
# concurrency group within seconds. The group's
# `cancel-in-progress: false` protects the RUNNING job only — a
# PENDING job is superseded when a newer one queues behind it, and
# the tag run is exactly the pending one a same-second main push
# displaces. The versioned docs then never deploy and the site's
# `latest` keeps pointing at the previous release until someone
# re-runs the cancelled job by hand.
#
# This script makes the order mechanical: push the tag alone, wait
# for its Deploy Docs run to finish green, then push main. The tag
# run enters the group first and runs protected; main's runs queue
# behind it. Re-running the script after an interrupted cut is
# safe: every step is idempotent.
#
# Usage (from the repo root, inside the devenv shell, with the cut
# commit made locally on main and the tag pointing at it):
#   scripts/cut-release.sh vX.Y.Z
set -euo pipefail

die() {
  echo "cut-release: $*" >&2
  exit 1
}

tag=${1:-}
[[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "usage: cut-release.sh vX.Y.Z (got: ${tag:-nothing})"
version=${tag#v}

[[ $(git branch --show-current) == main ]] ||
  die "run from main (on: $(git branch --show-current))"
[[ -z $(git status --porcelain) ]] ||
  die "working tree is dirty; commit or stash first"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null ||
  die "tag $tag does not exist locally; create it on the cut commit"
[[ $(git rev-parse "$tag") == $(git rev-parse HEAD) ]] ||
  die "tag $tag does not point at HEAD; re-create it on the cut commit"

# The release workflow extracts this section from the tagged tree;
# a cut without it silently loses the hand-written notes (#3142's
# lesson, checked here before anything is pushed).
# Both heading forms match: the escaped one the file
# mandates, the bare one the workflow also accepts.
git show "$tag":docs/changes.md |
  grep -qF -e "## \[v$version]" -e "## v$version]" ||
  die "docs/changes.md at $tag carries no '## \[v$version]' section"

echo "cut-release: pushing $tag (the tag alone — main follows)"
git push origin "refs/tags/$tag" 2>/dev/null ||
  echo "cut-release: tag already on origin; continuing"

# The push event takes a moment to register its workflow run; poll
# for the tag's Deploy Docs run, then wait it out.
run_id=""
for _ in $(seq 1 30); do
  run_id=$(gh run list --workflow "Deploy Docs" --limit 10 \
    --json databaseId,headBranch --jq "
      .[] | select(.headBranch == \"$tag\") | .databaseId
    " 2>/dev/null | head -n 1 || true)
  [[ -n $run_id ]] && break
  sleep 2
done
[[ -n $run_id ]] ||
  die "no Deploy Docs run appeared for $tag (check the Actions tab)"

echo "cut-release: watching Deploy Docs run $run_id"
if ! gh run watch "$run_id" --exit-status --interval 10 \
  >/dev/null 2>&1; then
  run_url="https://github.com/$(git remote get-url origin |
    sed -E 's#.*[:/]([^/]+/[^/]+)(\.git)?$#\1#')/actions/runs/$run_id"
  die "the tag's Deploy Docs run did not succeed: $run_url
rerun it (gh run rerun $run_id), then run this script again — \
main is NOT pushed until the versioned docs deploy green"
fi

echo "cut-release: versioned docs are live; pushing main"
git push origin main
echo "cut-release: done — $version's docs deployed, main pushed"
