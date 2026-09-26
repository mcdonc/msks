"""The resize command's domain (#184, #277): the outcome line
the CLI's print and the workspace TUI's edit dialog (#331) both
speak, so one resize reads the same on both surfaces."""

#: The changes whose effect waits for the next boot, by the word
#: the daemon's ``changes`` list starts each entry with: home
#: bytes move at once on the host, while the root's guest-side
#: fill and the new topology are facts the next boot reads.
BOOT_WAITING = (
    (("root",), "the guest fills the larger root"),
    (("cpus", "mem"), "the new topology applies"),
)


def resize_boot_note(changes: list[str]) -> str:
    """The parenthesized note for what waits for the next boot:
    empty when nothing does. The daemon's ``changes`` list decides,
    not the request's flags — home bytes moved at once on the host;
    the root's guest-side fill and the new topology apply at the
    next boot."""
    waits = [
        note
        for prefixes, note in BOOT_WAITING
        if any(change.startswith(prefixes) for change in changes)
    ]
    if not waits:
        return ""
    return f" ({' and '.join(waits)} on its next boot)"


def resize_message(row: dict, body: dict) -> str:
    """The result line: the new sizes, plus the topology when the
    request moved it, with the boot note naming what waits for the
    next boot."""
    line = (
        f"resized {display_name(row)}: root {row['root_mib']} MiB, "
        f"home {row['home_mib']} MiB"
    )
    if body.get("cpus") is not None or body.get("mem_mib") is not None:
        line += f", cpus {row['cpus']}, mem {row['mem_mib']} MiB"
    return line + resize_boot_note(row.get("changes", []))


def display_name(row: dict) -> str:
    """The human-facing label (#246): the workspace's name, else
    its immutable id (a nameless workspace is addressed by id)."""
    return row.get("name") or row["id"]
