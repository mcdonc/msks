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
# behind it.
#
# Re-running after an interrupted cut is safe: a tag already on
# origin at the same commit continues (HEAD may have moved past
# it — the version bump lands next), anything else dies loudly,
# and main is never pushed until the versioned docs deploy green.
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

branch=$(git branch --show-current)
[[ $branch == main ]] ||
  die "run from main (on: ${branch:-detached HEAD})"
[[ -z $(git status --porcelain) ]] ||
  die "working tree is dirty; commit or stash first"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null ||
  die "tag $tag does not exist locally; create it on the cut commit"

# Origin's word on this tag, read once and trusted everywhere
# after: a remote tag pointing anywhere but the local one is a
# release about to run on the wrong tree, not an idempotent
# retry.
on_origin=$(git ls-remote origin "refs/tags/$tag" | cut -f1)
tagged=$(git rev-parse "$tag")
if [[ $tagged != "$(git rev-parse HEAD)" ]]; then
  # The one legal off-HEAD shape: a retry after main moved past
  # the cut (the version bump lands next), with origin already
  # holding this tag at the cut commit.
  [[ -n $on_origin && $on_origin == "$tagged" ]] ||
    die "tag $tag does not point at HEAD; re-create it on the cut commit"
  git merge-base --is-ancestor "$tag" HEAD ||
    die "tag $tag is not an ancestor of HEAD; re-create it on the cut commit"
fi

# The release workflow extracts this section from the tagged tree
# and refuses the release without it (#564, #3142's silent-loss
# lesson) — the same matcher runs here, before anything is
# pushed, so a cut missing its notes dies locally.
notes=$(mktemp)
trap 'rm -f "$notes"' EXIT
git show "$tag":docs/changes.md >"$notes"
python scripts/release_notes.py "$tag" "$notes" >/dev/null ||
  die "docs/changes.md at $tag carries no release-notes section"

if [[ -n $on_origin ]]; then
  [[ $on_origin == "$tagged" ]] ||
    die "origin's $tag points at $on_origin, not $tagged; delete the remote tag or re-create yours"
  echo "cut-release: $tag already on origin at the cut commit; continuing"
else
  echo "cut-release: pushing $tag (the tag alone — main follows)"
  git push origin "refs/tags/$tag"
fi

# The push event takes a moment to register its workflow run; the
# server-side branch filter names the tag's runs (a tag push's
# headBranch is the tag), immune to any number of dev deploys
# queued in between.
run_id=""
for _ in $(seq 1 30); do
  run_id=$(
    gh run list --workflow "Deploy Docs" --branch "$tag" --limit 1 \
      --json databaseId --jq ".[0].databaseId" 2>/dev/null || true
  )
  [[ -n $run_id ]] && break
  sleep 2
done
[[ -n $run_id ]] ||
  die "no Deploy Docs run appeared for $tag (check the Actions tab)"

# The slug for run links, from the remote's own spelling: both the
# scp form (git@github.com:o/r.git) and the URL form
# (https://github.com/o/r.git) reduce to o/r.
remote=$(git remote get-url origin)
remote=${remote%.git}
slug="$(basename "$(dirname "$remote")")/$(basename "$remote")"
run_url="https://github.com/$slug/actions/runs/$run_id"
echo "cut-release: watching Deploy Docs run $run_id ($run_url)"
if ! gh run watch "$run_id" --exit-status --interval 10 >/dev/null 2>&1; then
  die "the tag's Deploy Docs run did not succeed: $run_url
rerun it (gh run rerun $run_id), then run this script again — \
main is NOT pushed until the versioned docs deploy green"
fi

echo "cut-release: versioned docs are live; pushing main"
git push origin main
echo "cut-release: done — ${tag#v}'s docs deployed, main pushed"
