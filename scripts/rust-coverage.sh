#!/usr/bin/env bash
# The Rust coverage gate (#63): 100% line AND branch coverage on the
# console helper's library, or the run fails.
#
# Why not cargo-llvm-cov: nightly cargo (required for branch coverage,
# -Z coverage-options=branch) moved build artifacts under
# build/<pkg>/<hash>/out/, where cargo-llvm-cov's object discovery does
# not look — its reports silently drop the test binaries' counters.
# This script drives the same LLVM tools (llvm-profdata, llvm-cov from
# the toolchain's llvm-tools component, version-matched to rustc) over
# every instrumented object it can find, layout-agnostic.
#
# Exclusions: src/main.rs — the vsock socket creation and fork plumbing
# cannot be exercised in a test environment; everything it calls is
# gate-covered, and the integration tests drive the real binary through
# its --test-listen-fd mode (see lib.rs).
set -euo pipefail

root="${DEVENV_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
crate="$root/src/console-helper"
# From nixpkgs' LLVM 23 (devenv packages), version-matched to the
# pinned nightly rustc's LLVM — a mismatched llvm-profdata cannot
# read the newer raw-profile format and the merge fails loudly.
# Version-matched to the pinned nightly rustc (the toolchain's
# llvm-tools component; an nixpkgs LLVM of the same major can be an
# -rc whose profdata reads the profiles as garbage).
tools="${MSKS_RUST_LLVM_TOOLS:-}"
if [ -n "$tools" ] && [ -x "$tools/llvm-profdata" ]; then
  profdata="$tools/llvm-profdata"
  cov="$tools/llvm-cov"
else
  profdata="$(command -v llvm-profdata)"
  cov="$(command -v llvm-cov)"
fi

cd "$crate"
target_dir="target/coverage"
# Everything profile-related is resolved absolutely: tests may chdir
# (the privilege-drop exercise), and a relative LLVM_PROFILE_FILE
# would scatter the exit-time writes.
crate_dir="$(pwd)"
# A clean slate, not just the profiles: stale binaries from any
# earlier build with different RUSTFLAGS carry mismatched counter
# sections, and llvm-cov silently drops their profiles.
rm -rf "$target_dir"
out="$crate_dir/$target_dir/profiles"
mkdir -p "$out"

# Build instrumented for branch coverage, then run the test binaries
# directly: nightly cargo's own test-runner invocation suppresses the
# profile write (manual execution of the same binary writes it — see
# the comment history in #63), so the gate cannot rely on `cargo
# test` running them. The %m-%p pattern lets the integration tests'
# helper subprocesses merge their counters (each process gets its own
# file).
export RUSTFLAGS="--cfg coverage -C instrument-coverage -Z coverage-options=branch -C link-dead-code"
cargo test --tests --no-run --target-dir "$target_dir"

# Stage the test binaries out of cargo's target tree: nightly cargo
# may replace them after --no-run (fingerprint churn under the new
# build layout), which silently orphans the profiles their runs
# wrote. Running and reporting the staged copies keeps the
# object/profile pair identical.
stage="$crate_dir/$target_dir/stage"
mkdir -p "$stage"
mapfile -d '' test_bins < <(
  find "$target_dir" -type f -executable \
    \( -name 'msks_console_helper-*' -o -name 'integration-*' -o -name 'unit-*' \) \
    ! -name '*.d' -print0
)
staged_bins=()
for bin in "${test_bins[@]}"; do
  copy="$stage/$(basename "$bin")"
  cp "$bin" "$copy"
  staged_bins+=("$copy")
done
# One patterned profile path per process: forked children inherit
# the runtime's startup snapshot, so a plain path would have every
# child overwrite the parent — the %p pattern gives each process its
# own file (the integration tests' helper subprocesses rewrite theirs
# to the same shape).
for bin in "${staged_bins[@]}"; do
  LLVM_PROFILE_FILE="$out/run-%m-%p.profraw" "$bin"
done

"$profdata" merge -sparse "$out"/*.profraw -o "$out/merged.profdata"
# The staged test binaries are the objects for their runs' profiles;
# the package binary maps the helper children's counters (its session
# children are forked copies of it). The rlib carries no loadable
# coverage data on this toolchain and is left out — the test binaries
# link the same library code.
mapfile -d '' objects < <(
  find "$stage" -type f -print0
  find "$target_dir" -type f -name 'msks-console-helper' -print0
)

# Session children of the integration helpers exit a moment after
# their clients detach; their counters land only at that exit. Give
# the stragglers a beat before grouping, or the report races them.
sleep 2

# Profiles are grouped by the module hash (%m) in their names: the
# test binaries, the package binary, and its helper children each
# carry their own compilation of the library, and merging raws across
# compilations zeroes the conflicting counter records. Each group is
# merged alone; each object is exported against every group and keeps
# the export where it maps the most covered lines (its own group).
declare -a group_datas=()
for group in $(
  find "$out" -maxdepth 1 -name 'run-*.profraw' -printf '%f\n' |
    sed -n 's/^run-\(.*\)_[0-9]*-[0-9]*\.profraw$/\1/p' | sort -u
); do
  group_out="$out/group-$group.profdata"
  "$profdata" merge -sparse "$out"/run-"$group"_*.profraw -o "$group_out"
  group_datas+=("$group_out")
done

# Every (object, group) pair contributes its mapping to the union: a
# group's counters only map onto the compilation they came from, so
# pairs from other groups contribute nothing rather than wrong data.
: >"$out/reports.txt"
for object in "${objects[@]}"; do
  for group_data in "${group_datas[@]}"; do
    report="$out/$(basename "$object").$(basename "$group_data").lcov"
    if "$cov" export --format=lcov \
      --instr-profile "$group_data" \
      --ignore-filename-regex 'src/main\.rs|/tests/|/\.cargo/|/rustc/' \
      "$object" >"$report" 2>/dev/null; then
      echo "$report" >>"$out/reports.txt"
    fi
  done
done

python3 - "$out/reports.txt" <<'PY'
import sys

lines = {}     # (file, line) -> hit count (union = max)
branches = {}  # (file, line, col, block) -> taken (union)

def as_int(text):
    try:
        return int(text)
    except ValueError:
        return 0

for path in open(sys.argv[1]):
    path = path.strip()
    if not path:
        continue
    file = None
    for record in open(path):
        record = record.strip()
        if record.startswith("SF:"):
            file = record[3:]
        elif record.startswith("DA:") and file:
            line, count = record[3:].split(",")[:2]
            key = (file, int(line))
            lines[key] = max(lines.get(key, 0), as_int(count))
        elif record.startswith("BRDA:") and file:
            line, col, block, taken = record[5:].split(",")
            key = (file, int(line), int(col), int(block))
            hit = taken != "-" and as_int(taken) > 0
            branches[key] = branches.get(key, False) or hit

line_total = len(lines)
line_covered = sum(1 for v in lines.values() if v > 0)
branch_total = len(branches)
branch_covered = sum(branches.values())
print(f"lines:    {line_covered}/{line_total}")
print(f"branches: {branch_covered}/{branch_total}")
missing = []
if line_total and line_covered < line_total:
    uncovered = sorted(k for k, v in lines.items() if not v)
    detail = ", ".join(f"{k[0].split('/')[-1]}:{k[1]}" for k in uncovered[:20])
    missing.append(f"lines {line_covered}/{line_total} ({len(uncovered)} uncovered: {detail})")
if branch_total and branch_covered < branch_total:
    missing.append(f"branches {branch_covered}/{branch_total} "
                   f"({branch_total - branch_covered} uncovered)")
if missing:
    sys.exit("; ".join(missing))
print("coverage gate: 100% lines and branches")
PY
