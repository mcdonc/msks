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
DOCTOR_HINT = (
    "The VMM exited before the workspace came up. Run `msksd doctor` "
    "on the daemon's host: its /dev/kvm row tells whether the host "
    "can boot workspaces at all."
)

#: The suggestions the shell flow's refusal note leads with
#: (#748, #1139): the console fallback, and the host's doctor
#: after a VMM exit.
CONSOLE_HINT = "try msks console"
DOCTOR_HINT_LINE = "run msksd doctor on the daemon's host"

#: The local driver's words for a VMM exit, the two halves the
#: predicate looks for.
EXITED_WITH = "cloud-hypervisor exited with"
BEFORE_SERVING = "before serving"


def vmm_exit(detail: str) -> bool:
    """Whether a start refusal's detail carries the VMM's exit:
    the daemon words it ``cloud-hypervisor exited with <code>
    before serving <socket>`` (the local driver's message, the
    last log line after it), and the fix is the host's to make."""
    return EXITED_WITH in detail and BEFORE_SERVING in detail


def refusal_detail(detail: str) -> str:
    """The text a refused start's panel shows: the daemon's
    detail verbatim, then the ``msksd doctor`` hint as its own
    paragraph when the detail carries a VMM exit."""
    if vmm_exit(detail):
        return f"{detail}\n\n{DOCTOR_HINT}"
    return detail


def ssh_hint(detail: str) -> str:
    """The suggestion the shell flow's refusal note leads with:
    the console fallback, unless the boot refused on a VMM exit.
    The console boots the workspace too, so after a VMM exit the
    step that goes somewhere is the host's doctor."""
    if vmm_exit(detail):
        return DOCTOR_HINT_LINE
    return CONSOLE_HINT
