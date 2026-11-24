"""The one-command bootstrap wrappers (#1119, phase 4b of #1080).

The wrappers are release assets the release job attaches
verbatim, so the suite pins the invariants that keep them
coherent with the code they install: POSIX syntax, the root
gate, the tool each wrapper selects and the os-release IDs it
accepts (the profiles' own), the pins (uv's release matches the
venv phase's ``UV_PIN``; the VMM's release and aarch64 digest
match the macOS tool's pinned upstream), and the checksum
record's shape — every artifact name a wrapper can fetch
carries one sha256 line, and the release job appends exactly
that file to SHASUMS beside the wrappers. The job's own
checksum step runs over a staged dist/: each line carries a
bare asset name, the name a wrapper fetches (#1136).

The root-and-network path itself never executes here: fetching,
verification, and the tool run need root on a targeted host.
The live proof is the container run recorded on the PR, and
the stock-host acceptance the issue names stays a by-hand
step.
"""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from msks.server.hostsetup.profile import DEBIAN_PROFILE
from msks.server.hostsetup.venv import UV_PIN
from msks.server.hostsetup_fedora import FEDORA_PROFILE
from msks.server.hostsetup_macos.base import CH_SHA256, CH_URL

ROOT = Path(__file__).resolve().parents[3]

WRAPPERS = {
    "debian": ROOT / "bootstrap" / "msks-setup-debian.sh",
    "fedora": ROOT / "bootstrap" / "msks-setup-fedora.sh",
}

PROFILES = {"debian": DEBIAN_PROFILE, "fedora": FEDORA_PROFILE}

PINNED_SUMS = ROOT / "bootstrap" / "pinned-sums"

RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

#: Every upstream artifact a wrapper can fetch, by name: uv's
#: tarball and the VMM's static binary, one per architecture.
PINNED_NAMES = {
    "uv-x86_64-unknown-linux-gnu.tar.gz",
    "uv-aarch64-unknown-linux-gnu.tar.gz",
    "cloud-hypervisor-static",
    "cloud-hypervisor-static-aarch64",
}

LINE_SHAPE = re.compile(r"^([0-9a-f]{64})  (\S+)$")


def text(name: str) -> str:
    return WRAPPERS[name].read_text(encoding="utf-8")


def detection_loop(name: str) -> str:
    """The wrapper's real flag-detection loop, sliced verbatim
    from the asset — the block a behavioral probe runs under
    ``sh`` with argv of the test's choosing."""
    body = text(name)
    start = body.index("# --wheel and --uninstall detection")
    end = body.index("\ndone\n", start) + len("\ndone\n")
    return body[start:end]


def pinned_lines() -> list[tuple[str, str]]:
    """The pin file's (digest, name) pairs, refusing a malformed
    line — the record must stay pure sha256sum text, because a
    `sha256sum -c` over a downloaded SHASUMS refuses anything
    else."""
    pairs = []
    for line in PINNED_SUMS.read_text(encoding="utf-8").splitlines():
        match = LINE_SHAPE.match(line)
        assert match is not None, f"not a sha256sum line: {line!r}"
        pairs.append((match.group(1), match.group(2)))
    return pairs


#: The release job's step that writes the checksum record
#: (#1136): the suite runs it over a staged dist/.
RECORD_STEP = "Record the checksums"

#: A cut release's dist/ in miniature: the pure wheel, one
#: manylinux wheel, and the sdist.
STAGED_ASSETS = (
    "msks-0.0.0-py3-none-any.whl",
    "msks-0.0.0-py3-none-manylinux_2_17_x86_64.whl",
    "msks-0.0.0.tar.gz",
)

#: The release job hashes on its Linux runner; a macOS host
#: ships no sha256sum, so the record's probes wait for a host
#: that has one.
needs_sha256sum = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="the sha256sum binary is absent"
)


def record_step() -> str:
    """The release job's checksum step as the workflow runs
    it."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    named = [
        step
        for step in workflow["jobs"]["release"]["steps"]
        if step.get("name") == RECORD_STEP
    ]
    assert len(named) == 1, f"the release job carries one {RECORD_STEP} step"
    return named[0]["run"]


def staged_record(tmp_path: Path) -> Path:
    """The record step run as the workflow runs it, over a
    staged tree: the assets in dist/, the pin file beside it.
    Returns dist/, the record inside it."""
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in STAGED_ASSETS:
        (dist / name).write_bytes(name.encode("utf-8"))
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap" / "pinned-sums").write_text(
        PINNED_SUMS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    done = subprocess.run(
        ["sh", "-ec", record_step()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return dist


def wheel_lookup(name: str) -> str:
    """The wrapper's record lookup for the manylinux wheel,
    sliced verbatim from the asset: the lines from the grep to
    the name the fetch uses."""
    body = text(name)
    start = body.index("  wheel_line=$(grep")
    end = body.index("\n  wheel=$(fetch_verified", start)
    return body[start:end]


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_the_wrappers_parse_as_posix_sh(name):
    done = subprocess.run(
        ["sh", "-n", str(WRAPPERS[name])],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_a_wrapper_refuses_when_not_root(name):
    """The first gate is the root check — running the asset
    unprivileged refuses before any read or fetch, with the
    curl-through-sudo spelling the operator needs."""
    if os.geteuid() == 0:
        pytest.skip("the host runs the suite as root")
    done = subprocess.run(
        ["sh", str(WRAPPERS[name])],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert done.returncode == 1
    assert "run me as root" in done.stderr
    assert "sudo sh" in done.stderr


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_each_wrapper_selects_its_platforms_tool(name):
    """The wrapper's own gate mirrors the tool's: the same
    os-release IDs the profile accepts, the tool the profile
    names — a wrapper that drifts from its tool refuses a host
    the tool serves, or serves one the tool refuses."""
    body = text(name)
    tool = re.search(r"^tool=(\S+)$", body, re.M)
    ids = re.search(r'^platform_ids="([^"]+)"$', body, re.M)
    assert tool is not None and ids is not None
    assert tool.group(1) == PROFILES[name].tool
    assert set(ids.group(1).split()) == set(PROFILES[name].os_ids)


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_the_uv_pin_is_the_venv_phases(name):
    """One pin, one tool (#1118): the wrapper provisions uv from
    the release the shared venv phase's ``UV_PIN`` names, so the
    throwaway venv and /opt/msks speak the same uv."""
    pinned = re.search(r"MSKS_UV_URL:-\S+/download/([^/}]+)", text(name))
    assert pinned is not None
    assert pinned.group(1) == UV_PIN


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_the_vmm_pin_is_the_macos_tools(name):
    """The wrapper and the macOS bootstrap pin the same upstream
    VMM release (#1080's one-pool-of-artifacts shape): same
    version, and the aarch64 digest is the Mac tool's own."""
    pinned = re.search(r"MSKS_VMM_URL:-\S+/download/(v[0-9.]+)", text(name))
    macos = re.search(r"/download/(v[0-9.]+)/", CH_URL)
    assert pinned is not None and macos is not None
    assert pinned.group(1) == macos.group(1)
    digests = dict((n, d) for d, n in pinned_lines())
    assert digests["cloud-hypervisor-static-aarch64"] == CH_SHA256


def test_the_record_covers_every_artifact_the_wrappers_fetch():
    """The pin file carries one well-formed line per fetchable
    upstream artifact and nothing else — a name a wrapper can
    ask for must answer, and a line no wrapper reads is a stale
    pin the record should not carry."""
    names = {name for _digest, name in pinned_lines()}
    assert names == PINNED_NAMES


@pytest.mark.parametrize(
    ("argv", "wheel", "uninstall"),
    [
        ([], "none", "0"),
        (["--no-service"], "none", "0"),
        (["--wheel", "/tmp/a.whl", "--no-service"], "/tmp/a.whl", "0"),
        (["--wheel=/tmp/b.whl"], "/tmp/b.whl", "0"),
        (["--uninstall"], "none", "1"),
        (["--uninstall", "--no-service"], "none", "1"),
        (["--uninstall", "--wheel", "/tmp/c.whl"], "/tmp/c.whl", "1"),
        (["--uninstall", "--wheel=/tmp/d.whl"], "/tmp/d.whl", "1"),
    ],
)
@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_the_detection_loop_reads_the_real_argv(name, argv, wheel, uninstall):
    """The loop the asset actually ships, run under sh with the
    argv of each case — both --wheel spellings, --uninstall, and
    the plain pass-throughs it must leave alone."""
    probe = detection_loop(name) + (
        '\nprintf "wheel=%s uninstall=%s\\n" "${wheel_in_hand:-none}" '
        '"$uninstall_asked"\n'
    )
    done = subprocess.run(
        ["sh", "-c", probe, "wrapper", *argv],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"wheel={wheel} uninstall={uninstall}\n"


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_the_wrappers_forward_flags_and_pin_the_fetched_wheel(name):
    """The flag vocabulary is the tool's own: every argument
    forwards verbatim, and a fetched wheel forwards as
    ``--wheel`` (so /opt/msks/venv installs the exact verified
    bytes) — but an uninstall run takes no --wheel append (the
    tool refuses the pair) and no VMM fetch (the tool's mirror
    would only remove it), and a mismatch names the artifact
    with its expected digest."""
    body = text(name)
    assert '"$@"' in body
    assert '[ -z "$wheel_in_hand" ] && [ "$uninstall_asked" = 0 ]' in body
    assert 'set -- "$@" --wheel "$wheel"' in body
    assert "skipping the VMM fetch" in body
    assert "checksum did not match" in body
    assert "expected $expected" in body


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_the_wheel_pattern_is_the_release_tag(name):
    """The wrapper greps the record for the manylinux wheel the
    release workflow retags (#1087's platform wheels carry the
    helper), keyed to the host's architecture."""
    body = text(name)
    assert "manylinux_2_17_${wheel_arch}" in body
    for arch in ("x86_64", "aarch64"):
        assert f"wheel_arch={arch}" in body


def test_the_release_job_attaches_the_wrappers_and_the_record():
    """A cut release carries both wrapper assets, and SHASUMS —
    the checksum record the wrappers fetch — is the wheels'
    digests plus exactly the pin file."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    for wrapper in ("msks-setup-debian.sh", "msks-setup-fedora.sh"):
        assert f"bootstrap/{wrapper}" in workflow
    assert "cat bootstrap/pinned-sums >> dist/SHASUMS" in workflow


@needs_sha256sum
def test_the_record_names_each_asset_by_its_download_name(tmp_path):
    """The record step, run over a staged dist/ (#1136): one
    well-formed line per asset and per pin, each under its bare
    name. A wrapper fetches its second field at
    <release-url>/<name> and a release asset downloads by its
    basename; the dist/ the step hashes from is a workspace
    path no download has."""
    record = (staged_record(tmp_path) / "SHASUMS").read_text(encoding="utf-8")
    names = []
    for line in record.splitlines():
        match = LINE_SHAPE.match(line)
        assert match is not None, f"not a sha256sum line: {line!r}"
        assert "/" not in match.group(2), line
        names.append(match.group(2))
    assert sorted(names) == sorted([*STAGED_ASSETS, *PINNED_NAMES])


@needs_sha256sum
@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_the_wheel_lookup_names_an_asset_the_release_holds(name, tmp_path):
    """The wrapper's lookup, sliced verbatim from the asset and
    run over the staged record: the wheel it goes on to fetch
    is a file the release holds, under that exact name."""
    dist = staged_record(tmp_path)
    record = shlex.quote(str(dist / "SHASUMS"))
    probe = (
        'die() { echo "$1" >&2; exit 1; }\n'
        f"sums={record}\n"
        "wheel_arch=x86_64\n"
        f"{wheel_lookup(name)}\n"
        'printf "%s\\n" "$wheel_name"\n'
    )
    done = subprocess.run(["sh", "-c", probe], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    wheel_name = done.stdout.rstrip("\n")
    assert (dist / wheel_name).is_file(), wheel_name


@needs_sha256sum
def test_the_by_hand_check_passes_beside_the_assets(tmp_path):
    """The installation chapter's by-hand check over the staged
    record: `sha256sum -c SHASUMS --ignore-missing`, run where
    the downloaded assets sit beside the record, verifies each
    one."""
    dist = staged_record(tmp_path)
    done = subprocess.run(
        ["sha256sum", "-c", "SHASUMS", "--ignore-missing"],
        cwd=dist,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    for name in STAGED_ASSETS:
        assert f"{name}: OK" in done.stdout
