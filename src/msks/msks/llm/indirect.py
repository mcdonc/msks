"""Secret indirection for provider credentials.

``file:`` and ``cmd:`` prefixes resolve at configure time so provider
keys stay out of the config file and the process environment. A
resolution failure names the setting, never the value.
"""

from __future__ import annotations

import subprocess


def resolve_indirection(value: str, name: str) -> str:
    """Resolve ``file:``/``cmd:`` prefixes on one secret-bearing value.

    ``file:`` reads the remainder as a path; ``cmd:`` runs it through
    the shell; anything else returns as-is. A resolution failure is a
    named error (the *name* of the value — an entry or the default
    key — never the value itself), so a broken reference fails the
    configure step loudly instead of sending an empty key upstream.
    """
    if value.startswith("file:"):
        try:
            return read_secret_file(value[len("file:") :])
        except OSError as exc:
            raise ValueError(
                f"{name}: cannot read {value[:5]} reference ({exc.strerror})"
            ) from None
    if value.startswith("cmd:"):
        return resolve_cmd_ref(value[len("cmd:") :], name)
    return value


def read_secret_file(path: str) -> str:
    """The file's contents, stripped; the path stays out of errors
    (it may name a secret)."""
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def resolve_cmd_ref(command: str, name: str) -> str:
    """The command's stripped stdout; a nonzero exit is a named
    error carrying the command's stderr head, never the value."""
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise ValueError(f"{name}: cmd: reference exited {proc.returncode}")
    return proc.stdout.strip()
