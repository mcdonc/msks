"""``msks-setup-macos-host`` (#676): the macOS host setup — the
tool that takes a stock Apple-Silicon Mac to a working msksd.

The Linux setup tools (``msks-setup-debian-host``, ``-fedora-``)
prepare a host msksd runs on directly; on macOS the daemon runs
inside its own Lima VM, so this tool's phases compose the pieces
the other tools never see:

1. **The host check** — Apple Silicon (the guest images are
   aarch64 and nested virtualization needs the Apple Virtualization
   framework), and a Lima at the template's floor (2.2.1, the
   version the template's own expansions were verified against).
   A Lima the Mac does not carry installs through brew (#699) —
   the stock-Mac step this tool runs itself; a Mac without brew
   names the Homebrew line and stops.
2. **The msksd VM** — created from the template that ships in the
   wheel (``server/assets/msksd-vm.yaml``, #671: vz, nested
   virtualization, the invoking user's home mounted under the
   reserved ``/mnt/msks`` prefix) with the daemon's port forward
   appended — the one piece the template leaves to this tool,
   because the port is a deployment choice. The sizing flags
   (``--cpus``, ``--memory``, ``--disk``, #721) render over the
   template's own three lines at creation and re-apply on every
   rerun: a changed value stops the VM, edits its instance, and
   starts it again — running workspaces stop with the VM, and
   each starts again with ``msks start`` — while equal
   values change nothing, and the disk only grows (Lima resizes
   the data disk at the next start, and cloud-init grows the
   guest's filesystem with it). A run without
   ``--port`` talks through the forward the existing VM already
   carries (#699: the forward is fixed at creation, so the VM's
   own answer is this deployment's port); an explicit ``--port``
   that disagrees with it stays the named recreation refusal.
3. **The in-VM bootstrap** — the daemon's Debian tool set
   (egress plumbing, volume trio, seed tooling) and its kernel
   routing setting, the pinned cloud-hypervisor static release,
   the wheel installed into /opt/msks
   (the PyPI release matching this tool's version, or ``--wheel``
   for a build in hand), ``/etc/msksd.yaml`` written (the daemon
   on the forwarded port, host-share mode ``prefix`` — the
   reserved-prefix geometry the template mounted, the invoking
   user's home the default allowed root), and a systemd unit that
   starts msksd at boot.
4. **The verify pass** — the daemon's TLS listener answers
   through the forward; the CA and the bootstrap token come back
   out, the chain proven against the fetched CA before anything
   is written (#733), and the CA written under
   ``~/.config/msks/``.
5. **The client config and the login agent** — ``msks.yaml`` itself
   (the client's own config tree): a daemon alias pointing a bare
   ``msks`` at the forward (``url``, ``token_file``, ``cafile`` —
   the token written beside the CA), so every CLI invocation
   reaches the daemon with no exports in the shell profile; and a
   launchd agent that runs ``limactl start msksd`` at login —
   kickstarted when installed, so the VM starts through the
   agent immediately too — so the daemon answers after a reboot.
   The printed exports remain
   as a copy-paste fallback, and an existing config file is kept
   exactly as it is, whatever wrote it (#730) — the run names
   the file and prints the alias block to paste or update in
   it, and the config is written only when the file is missing.
6. **The client links (#719, #734)** — ``msks`` and
   ``msks-term-popup`` linked into the brew prefix's bin dir
   (``$(brew --prefix)/bin`` — the Homebrew the tool already
   requires for Lima): user-owned, on PATH for every brew user,
   and the tool never runs sudo. The popup command rides beside
   ``msks`` because an ``open``-opened terminal window resolves
   its command on a fresh login PATH — nothing from the shell
   that spawned it passes through (#734). A foreign file at a
   path refuses by name; a dangling link re-points (stale by
   definition); ``--uninstall`` removes both.

``--check`` runs the verify pass alone — the macOS doctor row
set: Lima, the VM, the nested-virtualization marker, virtiofsd
inside the VM, the listener, the client config, the login agent
— and mutates nothing: a missing Lima refuses with the
run-the-tool remedy, never an install.
``--uninstall`` stops and deletes the VM, its login agent, and
the brew-prefix client links (Lima and the client config stay,
and the summary names
them). Every phase is idempotent: re-run any time; a rerun
preserves the token, the CA, and the daemon's state.
"""

import os as os
import shutil as shutil
import socket as socket
import ssl as ssl
import subprocess as subprocess
import sys as sys
import time as time
from importlib.resources import files as files
from pathlib import Path as Path

from ...client.config import default_config_path as default_config_path
from . import (
    agent,
    base,
    bootstrap,
    clientconfig,
    entry,
    hostcheck,
    links,
    skew,
    verify,
    vm,
)
from .agent import (
    AGENT_LABEL,
    AGENT_LOG,
    AGENT_PLIST,
    MAC_CA,
    MAC_TOKEN,
    MANAGED_ALIAS,
    MANAGED_HEADER,
    agent_loaded,
    agent_plist,
    ensure_agent,
    limactl_path,
    remove_agent,
    unload_agent,
)
from .base import (
    CH_SHA256,
    CH_URL,
    LIMA_FLOOR,
    PORT,
    SHAPE_FLOORS,
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
    fetch_vm_file,
    run,
    say,
    vm_shell,
)
from .bootstrap import (
    bootstrap_script,
    bootstrap_vm,
    refuse_dev_build,
    vm_version,
)
from .clientconfig import (
    active_daemon_entry,
    alias_entry,
    alias_stanza,
    client_config_body,
    client_config_row,
    ensure_client_config,
    entry_points_at,
    existing_config_status,
    identity_examples,
    points_at_this_daemon,
    write_private,
)
from .entry import (
    main,
    parse_args,
    refuse_readonly_shape,
    run_setup,
    shape_from_args,
    uninstall_closing_line,
    validate_shape,
)
from .hostcheck import (
    check_chip_class,
    check_host,
    install_lima,
    lima_after_install,
    lima_version_output,
    lima_version_tuple,
    require_lima,
)
from .links import (
    CLIENT_COMMANDS,
    FALLBACK_BREW_BINS,
    brewed_prefix,
    client_link_answer,
    client_link_path,
    client_link_row,
    client_sibling,
    link_client_mac,
    link_one_client,
    remove_client_link,
    remove_one_client_link,
)
from .skew import (
    check_rows,
    check_skew_remedy,
    client_side_rows,
    daemon_version,
    nested_marker,
    skew_row,
    virtiofsd_state,
)
from .verify import (
    client_preset,
    verify_chain,
    verify_daemon,
)
from .vm import (
    adopted_port,
    apply_shape,
    create_vm,
    delete_vm,
    edit_command,
    ensure_vm,
    forward_host_port,
    instance_shape,
    instance_yaml,
    listing_entries,
    planned_changes,
    refuse_disk_shrink,
    resolved_port,
    say_shape,
    vm_entry,
)

SUBMODULES = (
    base,
    hostcheck,
    vm,
    bootstrap,
    verify,
    clientconfig,
    agent,
    skew,
    links,
    entry,
)

__all__ = [
    "CH_SHA256",
    "CH_URL",
    "LIMA_FLOOR",
    "MacSetupError",
    "PORT",
    "SHAPE_FLOORS",
    "TOOL",
    "UV_PIN",
    "VM_CONF",
    "VM_NAME",
    "VM_PACKAGES",
    "VM_PORT",
    "VM_PREFIX",
    "VM_STATE",
    "VM_UNIT",
    "run",
    "say",
    "vm_shell",
    "check_chip_class",
    "check_host",
    "install_lima",
    "lima_after_install",
    "lima_version_output",
    "lima_version_tuple",
    "require_lima",
    "adopted_port",
    "apply_shape",
    "create_vm",
    "delete_vm",
    "edit_command",
    "ensure_vm",
    "forward_host_port",
    "instance_shape",
    "instance_yaml",
    "listing_entries",
    "planned_changes",
    "refuse_disk_shrink",
    "resolved_port",
    "say_shape",
    "vm_entry",
    "bootstrap_script",
    "bootstrap_vm",
    "refuse_dev_build",
    "vm_version",
    "client_preset",
    "verify_chain",
    "verify_daemon",
    "active_daemon_entry",
    "alias_entry",
    "alias_stanza",
    "client_config_body",
    "client_config_row",
    "ensure_client_config",
    "entry_points_at",
    "existing_config_status",
    "identity_examples",
    "points_at_this_daemon",
    "write_private",
    "AGENT_LABEL",
    "AGENT_LOG",
    "AGENT_PLIST",
    "MAC_CA",
    "MAC_TOKEN",
    "MANAGED_ALIAS",
    "MANAGED_HEADER",
    "agent_loaded",
    "agent_plist",
    "ensure_agent",
    "limactl_path",
    "remove_agent",
    "unload_agent",
    "check_rows",
    "check_skew_remedy",
    "client_side_rows",
    "daemon_version",
    "fetch_vm_file",
    "nested_marker",
    "skew_row",
    "virtiofsd_state",
    "CLIENT_COMMANDS",
    "FALLBACK_BREW_BINS",
    "brewed_prefix",
    "client_link_answer",
    "client_link_path",
    "client_link_row",
    "client_sibling",
    "link_client_mac",
    "link_one_client",
    "remove_client_link",
    "remove_one_client_link",
    "main",
    "parse_args",
    "refuse_readonly_shape",
    "run_setup",
    "shape_from_args",
    "uninstall_closing_line",
    "validate_shape",
]
