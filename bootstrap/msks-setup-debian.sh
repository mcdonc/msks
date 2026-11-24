#!/bin/sh
# The one-command Debian install (#1119, phase 4b of #1080):
#
#   curl -LsSf https://github.com/mcdonc/msks/releases/latest/download/msks-setup-debian.sh | sudo sh
#
# This file is a release asset, and it is the whole installer: it
# fetches the release's platform wheel, verifies every artifact
# it fetches against the release's checksum record (SHASUMS)
# before use, provisions uv when the host holds none, builds a
# throwaway venv from the wheel, and runs msks-setup-debian-host
# from it as root. The tool lands /opt/msks/venv and every
# host-side piece (#1118's phases); the throwaway venv is
# discarded afterwards. The trust profile is the by-hand
# chapter's: shipped code over TLS plus pinned checksums, and
# nothing stronger — the record itself is fetched over the
# same TLS.
#
# Flags after `-s --` pass through to the tool (--no-service,
# --uninstall); --wheel <path> installs a wheel in hand and
# skips the release fetch, in the throwaway and the real venv
# both. The three download bases answer to environment overrides
# (MSKS_RELEASE_URL, MSKS_VMM_URL, MSKS_UV_URL) for mirrors.
set -eu

self=msks-setup-debian.sh
tool=msks-setup-debian-host
platform_ids="debian ubuntu linuxmint"

release_url=${MSKS_RELEASE_URL:-https://github.com/mcdonc/msks/releases/latest/download}
vmm_url=${MSKS_VMM_URL:-https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/v54.0}
uv_url=${MSKS_UV_URL:-https://github.com/astral-sh/uv/releases/download/0.9.7}

say() { printf '%s: %s\n' "$self" "$1"; }
die() {
  printf '%s: %s\n' "$self" "$1" >&2
  exit 1
}

[ "$(id -u)" = 0 ] ||
  die "run me as root: curl -LsSf <release-url>/$self | sudo sh"

[ -r /etc/os-release ] ||
  die "no /etc/os-release to read — this wrapper targets Debian-derived hosts"
set +u
# shellcheck disable=SC1091
. /etc/os-release
set -u
case " $platform_ids " in
*" $ID "*) ;;
*)
  die "this wrapper runs $tool on Debian-derived hosts (os-release says '$ID')"
  ;;
esac

command -v curl >/dev/null 2>&1 ||
  die "curl is required — the wrapper fetches every artifact over HTTPS"
command -v tar >/dev/null 2>&1 ||
  die "tar is required — the wrapper unpacks uv's tarball"

case "$(uname -m)" in
x86_64)
  wheel_arch=x86_64
  vmm_name=cloud-hypervisor-static
  uv_name="uv-x86_64-unknown-linux-gnu.tar.gz"
  ;;
aarch64)
  wheel_arch=aarch64
  vmm_name=cloud-hypervisor-static-aarch64
  uv_name="uv-aarch64-unknown-linux-gnu.tar.gz"
  ;;
*)
  die "this wrapper carries no artifacts for $(uname -m) hosts"
  ;;
esac

# --wheel and --uninstall detection only: every argument
# forwards to the tool verbatim, and the tool owns each flag's
# own refusals. The two detections change what the wrapper
# itself does: a wheel in hand skips the release fetch, and an
# uninstall run takes no --wheel append (the tool refuses the
# pair) and no VMM fetch (it would land a binary only to
# remove it).
wheel_in_hand=
uninstall_asked=0
next_is_wheel_value=0
for argument in "$@"; do
  if [ "$next_is_wheel_value" = 1 ]; then
    wheel_in_hand=$argument
    next_is_wheel_value=0
  fi
  case $argument in
  --wheel) next_is_wheel_value=1 ;;
  --wheel=*) wheel_in_hand=${argument#--wheel=} ;;
  --uninstall) uninstall_asked=1 ;;
  esac
done

work=$(mktemp -d "${TMPDIR:-/tmp}/msks-setup.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

say "installing msks on $ID ${VERSION_ID:-unknown} ($(uname -m))"

sums="$work/SHASUMS"
curl -LsSf -o "$sums" "$release_url/SHASUMS" ||
  die "the checksum record download failed ($release_url/SHASUMS)"

record_digest() {
  # The record's sha256 for one artifact name — the sha256sum
  # line shape matched whole, so a longer name cannot answer for
  # a shorter one.
  line=$(grep -E "^[0-9a-f]{64}[[:space:]]+$2\$" "$1" | head -n 1)
  [ -n "$line" ] || die "the checksum record carries no $2 line"
  printf '%s\n' "$line" | cut -d' ' -f1
}

fetch_verified() {
  # <base-url> <name>: fetch one artifact to the work dir and
  # verify it against the record before anything uses it — the
  # install steps below run only on a matching digest.
  base=$1
  name=$2
  expected=$(record_digest "$sums" "$name")
  curl -LsSf -o "$work/$name" "$base/$name" ||
    die "the $name download failed ($base/$name)"
  actual=$(sha256sum "$work/$name" | cut -d' ' -f1)
  [ "$actual" = "$expected" ] ||
    die "the $name checksum did not match — expected $expected, got $actual; re-run, or install by hand (docs/installation.md)"
  printf '%s\n' "$work/$name"
}

if [ -n "$wheel_in_hand" ]; then
  say "installing the wheel at $wheel_in_hand (--wheel)"
  wheel=$wheel_in_hand
else
  # The record names the wheel — its filename carries the
  # version, which this wrapper does not know ahead of it. The
  # manylinux wheel for this host's arch is the one to take: it
  # carries the virtiofsd-as helper inside the package (#1087),
  # and /opt/msks/venv installs the exact bytes verified here.
  wheel_line=$(grep -E "msks-[^ ]*-py3-none-manylinux_2_17_${wheel_arch}\.whl$" "$sums" | head -n 1)
  [ -n "$wheel_line" ] ||
    die "the checksum record names no manylinux_2_17_${wheel_arch} wheel"
  wheel_name=$(printf '%s\n' "$wheel_line" | awk '{print $2}')
  wheel=$(fetch_verified "$release_url" "$wheel_name")
  say "fetched and verified $wheel_name"
fi

# The VMM lands verified before the tool runs, so the tool's own
# unverified download never fires in this flow: it finds the
# binary present and leaves it alone. A present binary stays
# untouched (an operator's own install, a pinned release, wins).
# An uninstall run fetches nothing here: the tool's mirror
# removes a landed VMM, so fetching one would only add work.
if [ "$uninstall_asked" = 1 ]; then
  say "skipping the VMM fetch (--uninstall removes what is there)"
elif command -v cloud-hypervisor >/dev/null 2>&1; then
  say "cloud-hypervisor already present (left untouched)"
else
  vmm=$(fetch_verified "$vmm_url" "$vmm_name")
  install -m 0755 "$vmm" /usr/local/bin/cloud-hypervisor
  /usr/local/bin/cloud-hypervisor --version >/dev/null 2>&1 ||
    die "the verified cloud-hypervisor does not run on this host"
  say "landed /usr/local/bin/cloud-hypervisor (verified, $vmm_name)"
fi

if command -v uv >/dev/null 2>&1; then
  uv=uv
  uv_dir=
else
  uv_archive=$(fetch_verified "$uv_url" "$uv_name")
  mkdir -p "$work/uv"
  tar -xzf "$uv_archive" -C "$work/uv" --strip-components=1
  uv="$work/uv/uv"
  uv_dir="$work/uv"
  say "provisioned uv from $uv_name (no uv on this host)"
fi

# The managed interpreter parks under /opt/msks/python — the same
# directory the tool's venv phase provisions into (#1118) — so
# the throwaway venv and the real one share one interpreter and
# one download, and the interpreter survives the throwaway. 022
# keeps every provisioned file world-readable: the msksd account
# traverses the whole chain (#1080's geometry).
umask 022
mkdir -p /opt/msks
chmod 0755 /opt/msks
export UV_PYTHON_INSTALL_DIR=/opt/msks/python
export UV_PYTHON_PREFERENCE=only-managed

say "building the throwaway venv (uv-managed Python 3.14)"
"$uv" venv --python 3.14 "$work/venv"
"$uv" pip install --python "$work/venv/bin/python" "$wheel"

if [ -z "$wheel_in_hand" ] && [ "$uninstall_asked" = 0 ]; then
  # The verified wheel forwards as --wheel, so /opt/msks/venv
  # installs the exact bytes the record pinned. An uninstall
  # run takes no --wheel: the tool refuses the pair.
  set -- "$@" --wheel "$wheel"
fi

say "running $tool — the throwaway venv goes when it exits"
set +e
PATH="${uv_dir:+$uv_dir:}$PATH" "$work/venv/bin/$tool" "$@"
status=$?
set -e
exit "$status"
