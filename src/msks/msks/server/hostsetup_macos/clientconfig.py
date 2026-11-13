"""Phase 5: the client config.

The msks client config that points at the VM's daemon, the ssh
alias stanza, and the rows a re-run reports about an existing
config's allegiance.
"""

from __future__ import annotations

import os
from pathlib import Path

from ...client.config import default_config_path, parse_config_doc
from .agent import MAC_CA, MAC_TOKEN, MANAGED_ALIAS, MANAGED_HEADER
from .base import TOOL, MacSetupError

# --- phase 5: the client config and the login agent -------------------


def write_private(path: Path, body: str) -> None:
    """Write a private file the house way: the parent 0700 when
    created, the file 0600 — the token is a credential, and the
    CA and the config join it for uniformity. A step the
    filesystem refuses (the mkdir, the open, the write) is a
    named refusal, never a raw traceback."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        raise MacSetupError(
            f"could not write {path} ({exc}); fix the path or its "
            "permissions, then re-run"
        ) from None


def client_config_body(port: int) -> str:
    """The managed client config: the daemon alias a bare ``msks``
    addresses — the forwarded port, the token file, the CA — with
    ``active_daemon`` naming it, so every CLI invocation reaches
    the daemon with no exports in the shell profile. The identity
    settings ride as commented examples (#730), the client's own
    first-run template's shape: the file a fresh install lands
    with names the two keys a ``msks create`` reads, one uncomment
    away."""
    return (
        f"{MANAGED_HEADER} (#689); this tool writes the file only\n"
        "# when it is missing (#730) — edit it freely; a rerun\n"
        "# keeps an existing file exactly as it is.\n"
        + alias_stanza(port)
        + identity_examples()
    )


def identity_examples() -> str:
    """The commented identity pair under the alias stanza: the
    ssh identity a ``msks create`` plants into every workspace,
    and its public half for a key the operator keeps encrypted.
    Commented, so the file sets nothing until the operator
    uncomments a line — the client reads both keys as global-
    only settings, exactly where they sit here."""
    return (
        "\n"
        "# identity_file: ~/.ssh/id_ed25519  # your own private "
        "key — the ssh\n"
        "#                                 # identity msks create "
        "plants into\n"
        "#                                 # every workspace\n"
        "# pubkey_file: ~/.ssh/id_ed25519.pub  # your key's public "
        "half, for a\n"
        "#                                 # key you keep encrypted "
        "(your agent\n"
        "#                                 # holds it; msks reads no "
        "private half)\n"
    )


def alias_stanza(port: int) -> str:
    """The daemon alias block alone: the managed config's body —
    for the file this tool writes."""
    return (
        "daemons:\n" + alias_entry(port) + f"active_daemon: {MANAGED_ALIAS}\n"
    )


def alias_entry(port: int) -> str:
    """The alias's own entry block, indented to sit under a
    ``daemons:`` mapping — the paste-able piece for an
    operator's own ``msks.yaml``. It carries no top-level key
    (the client's loader refuses duplicates, so a verbatim paste
    can never corrupt the file) and no managed header (the entry
    names no author; the operator's file stays theirs)."""
    return (
        f"  {MANAGED_ALIAS}:\n"
        f"    url: https://127.0.0.1:{port}\n"
        f"    token_file: {MAC_TOKEN}\n"
        f"    cafile: {MAC_CA}\n"
    )


def ensure_client_config(port: int) -> str:
    """Write or report the client config, idempotently.

    A missing file is written; a file the read cannot reach is a
    named refusal (never the missing arm's overwrite); an
    existing file stays exactly as it is (#730), and the status
    :func:`existing_config_status` builds names that choice for
    the caller to say."""
    path = Path(default_config_path())
    body = client_config_body(port)
    try:
        text = path.read_text()
    except FileNotFoundError:
        write_private(path, body)
        return f"wrote the client config ({path})"
    except OSError as exc:
        raise MacSetupError(
            f"the client config ({path}) cannot be read ({exc}); fix "
            "the file or remove it, then re-run"
        ) from None
    return existing_config_status(text, body, port, path)


def existing_config_status(text: str, body: str, port: int, path: Path) -> str:
    """The report over an existing config file's text: the
    current body is a no-op; a file whose active alias already
    dials this daemon is named as such — it needs no hand edit,
    whatever header prose a past release wrote into it; anything
    else — the client's own first-run template, a file this tool
    wrote on an earlier run, an operator's own file — stays
    byte-for-byte (#730), with the hand path named in the status
    and the alias block to paste or update riding the preset.
    A file that will not load takes the hand path too."""
    if text == body:
        return f"the client config is current ({path})"
    if points_at_this_daemon(text, port, path):
        return (
            f"the client config already points a bare msks at this "
            f"daemon ({path}) — kept as written"
        )
    return (
        f"kept the config at {path} — a rerun writes it only when "
        "it is missing; edit the file by hand or remove it and "
        "re-run"
    )


def points_at_this_daemon(text: str, port: int, path: Path) -> bool:
    """Whether the file's active alias already dials this forward
    with the VM's token and CA — the machinery the check row
    uses, reused for the kept report. A file that will not load
    answers False: the kept arm's hand path is its remedy too."""
    try:
        entry = active_daemon_entry(text, path)
    except MacSetupError:
        return False
    return entry_points_at(entry, port)


def client_config_row(port: int) -> str:
    """The check row: a bare ``msks`` reaches THIS daemon — the
    active alias names this forward with the VM's token and CA.
    Anything else (no file, a file that will not parse, an active
    daemon pointing elsewhere) is a named refusal with the
    hand-edit remedy."""
    path = Path(default_config_path())
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise MacSetupError(
            f"the client config ({path}) does not exist; re-run {TOOL} "
            "or write the daemon alias into it by hand"
        ) from None
    except OSError as exc:
        raise MacSetupError(
            f"the client config ({path}) cannot be read ({exc}); fix "
            "the file or remove it, then re-run"
        ) from None
    entry = active_daemon_entry(text, path)
    if not entry_points_at(entry, port):
        raise MacSetupError(
            f"the client config's active daemon does not point a bare "
            f"msks at this daemon (https://127.0.0.1:{port} with the "
            f"VM's token and CA); edit {path} by hand or remove it "
            f"and re-run {TOOL}"
        )
    return f"a bare msks reaches the daemon ({path})"


def active_daemon_entry(text: str, path: Path) -> dict:
    """The active alias's settings from the config file's text,
    parsed by the client's own loader — a file that will not load
    is a named refusal, and an active name no alias defines
    answers an empty entry (the pointing check refuses it)."""
    try:
        doc = parse_config_doc(text, str(path))
    except ValueError as exc:
        raise MacSetupError(
            f"the client config does not load ({exc})"
        ) from None
    return doc.get("daemons", {}).get(doc.get("active_daemon"), {})


def entry_points_at(entry: dict, port: int) -> bool:
    """Whether an alias's settings name this forward with the VM's
    token and CA — the three facts a bare ``msks`` dials."""
    return (
        entry.get("url") == f"https://127.0.0.1:{port}"
        and entry.get("token_file") == str(MAC_TOKEN)
        and entry.get("cafile") == str(MAC_CA)
    )
