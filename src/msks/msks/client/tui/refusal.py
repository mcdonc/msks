"""A refused start's text (#1139).

The daemon refuses a boot with ``503`` and puts the VMM's last
log line at the end of the detail. A status line ellipsizes at
its end, so a five-second flash dropped the cause first and the
row read ``created`` again with no trace of the failure. The
surfaces that show a refused start — the failure panel the list
and the workspace page open, and the shell flow's one-row note —
read the text here: the daemon's detail verbatim, the
``msksd doctor`` hint after it when the detail carries a VMM
exit, and the suggestion the shell flow leads with. Text alone,
no screen loaded: the shell flow imports it beside its
plain-terminal machinery.
"""

from __future__ import annotations

#: The paragraph a VMM exit adds after the daemon's detail in the
#: failure panel.
DOCTOR_PARAGRAPH = (
    "The VMM exited before the workspace came up. Run `msksd doctor` "
    "on the daemon's host: its /dev/kvm row tells whether the host "
    "can boot workspaces at all."
)

#: The suggestions the shell flow's refusal note leads with
#: (#748, #1139): the console fallback, and the host's doctor
#: after a VMM exit.
CONSOLE_HINT = "try msks console"
DOCTOR_HINT = "run msksd doctor on the daemon's host"

#: The first words of the local driver's message for a VMM that died
#: at spawn (:func:`msks.microvm.local.driver.LocalCloudHypervisor._wait_ready`
#: composes ``cloud-hypervisor exited with <code> before serving
#: <socket>``). The client cannot import the daemon's package, so
#: the words are copied here; the driver's suite builds its pin on
#: this constant (``test_launch_binary_exits_early_maps_to_error``),
#: so a reword on the daemon's side fails there until this copy
#: follows.
VMM_EXITED = "cloud-hypervisor exited with"


def vmm_exit(detail: str) -> bool:
    """Whether a start refusal's detail carries the VMM's exit:
    the daemon's ``cloud-hypervisor exited with <code> before
    serving <socket>`` opens with :data:`VMM_EXITED`, the last log
    line follows it, and the fix is the host's to make."""
    return VMM_EXITED in detail


def refusal_detail(detail: str) -> str:
    """The text a refused start's panel shows: the daemon's
    detail verbatim, then the ``msksd doctor`` paragraph as its
    own when the detail carries a VMM exit."""
    if vmm_exit(detail):
        return f"{detail}\n\n{DOCTOR_PARAGRAPH}"
    return detail


def ssh_hint(detail: str) -> str:
    """The suggestion the shell flow's refusal note leads with:
    the console fallback, unless the boot refused on a VMM exit.
    The console boots the workspace too, so after a VMM exit the
    step that goes somewhere is the host's doctor."""
    if vmm_exit(detail):
        return DOCTOR_HINT
    return CONSOLE_HINT
