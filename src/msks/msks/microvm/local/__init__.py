"""Local backend: one cloud-hypervisor process per workspace VM (#1).

Per-workspace layout under ``<state_dir>/vms/<workspace_id>/``:

- ``api.sock``  — the CH REST socket this process serves
- ``ch.pid``    — the CH process id (restart-surviving kill path)
- ``ch.log``    — the VMM's own stderr
- ``console.sock``— the serial device's unix socket (the guest's
                 autologin root getty on the arch's console
                 device, ttyS0 on x86_64 and ttyAMA0 on aarch64
                 (#833), speaks through it, #481)

Shutdown model, matching how the VMM really behaves: a bare
``cloud-hypervisor --api-socket`` is a daemon that keeps running after
the guest powers off (``vm.info`` returns to a non-running state), so
graceful shutdown is PUT /vm.power-button (the guest's handler runs
the clean poweroff) -> poll the guest down -> SIGTERM the VMM -> wait
for process exit, all under one deadline.
"""

import os as os  # noqa: F401 — suite seam
import sys  # noqa: F401 — suite seam
from pathlib import Path  # noqa: F401 — suite seam

from ..chapi import CloudHypervisorApi  # noqa: F401 — suite seam
from ..fsdaemons import (  # noqa: F401 — suite seam
    fs_pidfile_path,
    fs_socket_path,
)
from . import config, console, driver, fsproc, procs
from .config import (
    CH_STATE_TO_STATUS,
    GUEST_DOWN_STATES,
    POLL_INTERVAL_S,
    POWER_REPRESS_S,
    check_id,
    console_retry_error,
    disk_entries,
    map_ch_state,
    vm_config,
    vm_memory,
    vm_net,
)
from .console import (
    _ConsoleRetry,
    close_console_stream,
    console_attempt,
    half_close_raw_socket,
    probe_refusal_is_stale,
    socket_stale,
)
from .driver import (
    LocalCloudHypervisor,
)
from .fsproc import (
    FS_TERM_GRACE_S,
    VERSION_PROBE_TIMEOUT_S,
    fs_pid_serves,
    host_mount_facts,
    read_fs_pid,
    stop_fs_pid,
    sweep_fs_pidfile,
)
from .procs import (
    cmdline_is_vmm,
    has_socket_arg,
    kill_proc,
    kill_procs,
    pid_dead_by,
    pid_gone,
    read_cmdline,
)

SUBMODULES = (
    console,
    procs,
    config,
    fsproc,
    driver,
)

__all__ = [
    "_ConsoleRetry",
    "close_console_stream",
    "console_attempt",
    "half_close_raw_socket",
    "probe_refusal_is_stale",
    "socket_stale",
    "cmdline_is_vmm",
    "has_socket_arg",
    "kill_proc",
    "kill_procs",
    "pid_dead_by",
    "pid_gone",
    "read_cmdline",
    "CH_STATE_TO_STATUS",
    "GUEST_DOWN_STATES",
    "POLL_INTERVAL_S",
    "POWER_REPRESS_S",
    "check_id",
    "console_retry_error",
    "disk_entries",
    "map_ch_state",
    "vm_config",
    "vm_memory",
    "vm_net",
    "FS_TERM_GRACE_S",
    "VERSION_PROBE_TIMEOUT_S",
    "fs_pid_serves",
    "host_mount_facts",
    "read_fs_pid",
    "stop_fs_pid",
    "sweep_fs_pidfile",
    "LocalCloudHypervisor",
]
