"""Phase 3: the in-VM bootstrap.

The script that installs the wheel inside the VM and stands the
daemon up — pinned uv, cloud-hypervisor from the upstream
release, the daemon's own config and unit — with the dev-build
refusal (#699).
"""

from __future__ import annotations

import os
from pathlib import Path

from ...spec.version import __version__
from .base import (
    CH_SHA256,
    CH_URL,
    TOOL,
    UV_PIN,
    VM_CONF,
    VM_NAME,
    VM_PACKAGES,
    VM_PORT,
    VM_PREFIX,
    VM_STATE,
    VM_UNIT,
    MacSetupError,
    run,
    say,
    vm_shell,
)

# --- phase 3: the in-VM bootstrap ------------------------------------


def vm_version() -> str:
    """The wheel release the bootstrap installs: the PyPI release
    matching this tool's own version — the Mac's client and the
    VM's daemon ship from one build."""
    return __version__


def bootstrap_script(wheel: str | None, home: str) -> str:
    """The in-VM bootstrap: a uv-managed Python 3.14 venv (the
    wheel targets the project's floor; Debian trixie ships 3.13,
    so uv provisions the standalone interpreter the repo itself
    pins), the wheel (a release from PyPI, or the file this tool
    copied in for a build in hand), the config (the forwarded
    port, host-share mode prefix — the reserved-prefix geometry
    the template mounted — and the invoking user's home the
    default allowed root), the unit, and the start. Idempotent:
    the token and the state survive a rerun; the venv reinstall
    is a no-op at the same version. Coherence guards between the
    steps (#706): the installed daemon's version must equal the
    tool's, and it must parse the config this same script writes
    — the #704 crash loop refuses here, with the version pair
    named, ahead of the unit restart."""
    install = f"/tmp/{Path(wheel).name}" if wheel else f"msks=={vm_version()}"
    return f"""set -eux
apt-get update -qq
apt-get install -y -qq {VM_PACKAGES} >/dev/null
# The egress stack's kernel setting (#101): the daemon verifies
# it at startup and refuses an egress workspace on 0.
echo net.ipv4.ip_forward=1 >/etc/sysctl.d/99-msksd-egress.conf
sysctl -w net.ipv4.ip_forward=1 >/dev/null
# The VMM: pinned, verified, and left alone once present. Each
# failure names itself (#712's family): the download's URL, the
# checksum's mismatch — never a bare trace the refusal tail
# would bury the reason under.
ch_url={CH_URL}
ch_sha={CH_SHA256}
ch_bin=/usr/local/bin/cloud-hypervisor
if ! [ -x "$ch_bin" ]; then
  if ! curl -sSfL -o "$ch_bin" "$ch_url"; then
    set +x
    echo "the cloud-hypervisor download failed" \
      " ($ch_url); re-run the tool" >&2
    exit 1
  fi
  if ! echo "$ch_sha  $ch_bin" | sha256sum -c - >/dev/null; then
    set +x
    echo "the cloud-hypervisor download's checksum did not" \
      " match ($ch_url); re-run the tool" >&2
    exit 1
  fi
  chmod +x "$ch_bin"
fi
python3 -m venv /opt/uv-bootstrap
/opt/uv-bootstrap/bin/pip install -q uv=={UV_PIN}
/opt/uv-bootstrap/bin/uv venv -q --python 3.14 {VM_PREFIX}
uv_bin=/opt/uv-bootstrap/bin/uv
$uv_bin pip install -q --python {VM_PREFIX}/bin/python {install}
# The coherence guard, version half (#706, #704): everything
# after this line assumes the installed daemon is this tool's
# own build. A dev tree whose pyproject.toml still names an
# already-released number installs that release from PyPI under
# the same word — the version pair here catches a resolution
# that landed anything else; the same-numbered stale release is
# the config half's catch below.
ver_probe="import importlib.metadata as m; print(m.version('msks'))"
probe_err=/tmp/msksd-probe.err
if ! installed=$({VM_PREFIX}/bin/python -c "$ver_probe" 2>"$probe_err"); then
  # #712's family: an unquieted failure here leaves the
  # assignment's own trace as stderr's last line — the probe's
  # reason reaches the operator instead.
  set +x
  echo "the installed-version probe failed:" \
    " $(tail -1 "$probe_err")" >&2
  exit 1
fi
if [ "$installed" != "{vm_version()}" ]; then
  # set +x first (#712): under the script's xtrace the exit's own
  # trace would land as stderr's last line, and vm_shell's refusal
  # tail would carry "+ exit 1" instead of the message above it.
  set +x
  echo "the bootstrap installed msks $installed, not this" \
    "tool's {vm_version()} — pass --wheel with a build of this tree" >&2
  exit 1
fi
mkdir -p {VM_STATE}
token_file={VM_STATE}/bootstrap-token
if [ ! -s "$token_file" ]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" >"$token_file"
  chmod 600 "$token_file"
fi
token=$(cat "$token_file")
cat >{VM_CONF} <<CONF
# Written by {TOOL} (#676); re-run the tool to refresh this file.
# The bootstrap token and the state dir survive reruns. The file
# is flat — every key top-level, the config loader's shape.
host: 127.0.0.1
port: {VM_PORT}
socket: ""
bootstrap_token: "$token"
state_dir: {VM_STATE}
# Workspaces are networked by default; the daemon's egress stack
# arms at startup when the setting says so.
egress_enabled: true
# The reserved-prefix geometry the VM's template mounted (#671):
# share roots live under /mnt/msks, host paths translate by
# composition, and the invoking user's home is the default
# allowed root (#672's policy gate).
mount_host_mode: prefix
mount_host_roots: ["{home}"]
CONF
# The coherence guard, config half (#706, #704): the config
# this tool writes must parse under the daemon this bootstrap
# installed — the #704 shape (a same-numbered stale release that
# rejects keys newer than itself) refused here, with the version
# pair named, ahead of the unit restart and its crash loop.
parse_probe="from msks.config import load_settings; "
parse_probe+="load_settings('{VM_CONF}')"
if ! {VM_PREFIX}/bin/python -c "$parse_probe" 2>/tmp/msksd-config.err; then
  # set +x first (#712): same as the version half — the message,
  # not the exit's trace, is stderr's last line.
  set +x
  echo "the daemon this bootstrap installed ($installed) rejects" \
    " the config this tool ({vm_version()}) writes (a stale" \
    " release can carry the same number): pass --wheel with a" \
    " build of this tree; $(tail -1 /tmp/msksd-config.err)" >&2
  exit 1
fi
cat >{VM_UNIT} <<'UNIT'
# Written by {TOOL} (#676); re-run the tool to refresh this file.
[Unit]
Description=msksd (the msks daemon VM's service)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart={VM_PREFIX}/bin/msksd serve --config {VM_CONF}
Restart=on-failure
StateDirectory=msksd
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable msksd.service
# A rewrite of the config must reach the running daemon: enable
# alone leaves a live service on the old file (the CA and the
# state then land where the old config said).
systemctl restart msksd.service
"""


def refuse_dev_build(wheel: str | None) -> None:
    """#704: a development build has no release to install — the
    PyPI path resolves ``msks==<this version>`` to nothing, and
    falling back to the same-numbered released wheel silently
    installs a daemon that predates the config this tool writes
    (the crash-loop shape). The refusal preflights the run, after
    the platform gate and before the mutating phases — the VM
    creation that follows would otherwise spend minutes building
    a machine a rerun cannot finish."""
    if wheel is None and ".dev" in vm_version():
        raise MacSetupError(
            f"this tool is a development build ({vm_version()}); the "
            "VM installs a release from PyPI or a wheel in hand — "
            "build one (uv build) and pass --wheel"
        )


def bootstrap_vm(wheel: str | None, home: str) -> None:
    """Copy the wheel in when one is named, then run the
    bootstrap script inside the VM."""
    if wheel is not None:
        say(f"copying {wheel} into the VM")
        run(["limactl", "copy", os.path.abspath(wheel), f"{VM_NAME}:/tmp/"])
    vm_shell(bootstrap_script(wheel, home))


# --- phase 4: the verify pass and the preset --------------------------
