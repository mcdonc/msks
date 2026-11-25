"""The client's half of the unlock (#515, #504).

The daemon holds each placeholder value as ciphertext blobs, one
per covering workspace identity key; the daemon holds no key that
opens them. This module is the client's flow over that store:
fetch the pending rows, decrypt the blobs whose private half this
machine holds, and hand the plaintext back — possession of the
private key is the authority for a value, proven by the unwrap
itself. A client without the key receives ciphertext and learns
nothing, and the daemon stays cold for those rows.

The identity file is the one the client already reads for ssh
(``identity_file`` / ``MSKSC_IDENTITY_FILE``, #336 —
:func:`msks.client.ssh.configured_identity_file`); an agent-held
key cannot unlock (#510's correction to #504): ssh-agent exposes
signing, and the age ssh-recipient unwrap needs the private
scalar directly, so the unlock reads the file where it lives and
writes no copy anywhere. A passphrase-encrypted file prompts
once per session — the caller owns the prompt seam, and the
loaded identity then decrypts every row.

The flow splits where the prompt stands (#1147). The TUI runs
:func:`unlock_flow` as one coroutine on its loop, its modal
between the halves. The GUI runs the same halves as two flights
over its bridge with the prompt between them on the window's own
thread: :func:`prepare_unlock` fetches the locked rows and the
configured key file, :func:`key_file_state` says whether that
file asks for a passphrase, :func:`open_key_file` loads it under
the answer, and :func:`settle_unlock` decrypts and uploads. The
outcome lines are the module's own, so every surface flashes the
same words.
"""

import base64
from pathlib import Path

from msks.agecrypto import (
    AgeCryptoError,
    addressed,
    decrypt_with_identity,
    key_is_encrypted,
    load_identity,
)

from .ssh import configured_identity_file

#: The two quiet cases' lines (#515), named when an operator asks
#: by hand and silent when the automatic flow meets them: no key
#: file configured, and a daemon whose cache already holds every
#: row's plaintext.
MISSING_IDENTITY_LINE = (
    "the unlock needs the workspace identity's private half — "
    "point identity_file (the client config) or MSKSC_IDENTITY_FILE "
    "at your key file"
)
NOTHING_LOCKED_LINE = (
    "nothing to unlock — the daemon holds every row's plaintext"
)

#: The spent prompt's line (#920), the surface's own by-hand
#: action filled in: the automatic flow of a session whose prompt
#: another surface already asked answers the rows as locked, and
#: the operator's by-hand action asks again.
SPENT_PROMPT_LINE = (
    "secrets stay locked — this session's passphrase prompt was "
    "already asked; {} asks again"
)

#: A pending row as ``GET /api/v1/secrets/pending`` serves it
#: (#514): identity, coverage, one ciphertext blob per recipient,
#: staleness flags, and whether the daemon's cache already holds
#: the plaintext.


def read_identity_pem(path: Path) -> bytes:
    """The named identity file's bytes — a missing or unreadable
    file is the one named refusal (the caller prints it with the
    file's name and the setting that points at it)."""
    try:
        return path.read_bytes()
    except OSError:
        raise SystemExit(
            f"msks: cannot read the identity file {path} — the "
            "unlock reads the private half in place; check the "
            "file and the identity_file setting"
        ) from None


def locked_rows(pending: list[dict]) -> list[dict]:
    """The pending rows the daemon's cache does not hold (#515):
    the rows an unlock attempt is for. A row with no blobs stays
    out — there is no ciphertext to try, and its own unlock (the
    mint that wrote it, or a coverage heal) is the recovery."""
    return [
        row for row in pending if not row.get("unlocked") and row.get("blobs")
    ]


def row_blobs(row: dict) -> list[bytes]:
    """The row's blob payloads as bytes — a blob whose base64
    does not decode is skipped (named at the row level, never a
    crash on one bad blob)."""
    out = []
    for blob in row["blobs"]:
        try:
            out.append(base64.b64decode(blob["ciphertext"]))
        except ValueError:
            continue
    return out


def header_addresses(data: bytes, identity) -> bool:
    """Whether the blob's header carries a stanza addressed to
    this key (#515 review) — a blob that cannot even parse reads
    as corruption (the daemon presented it as ciphertext), not
    as another operator's row."""
    try:
        return addressed(data, identity)
    except AgeCryptoError:
        return True


def row_outcome(row: dict, identity) -> tuple[str | None, bool]:
    """One row's try (#515): the decrypted value (None when no
    blob opened) and whether a blob addressed to this key broke —
    corruption, not another operator's row."""
    value: str | None = None
    broken = False
    for data in row_blobs(row):
        try:
            plain = decrypt_with_identity(data, identity)
        except AgeCryptoError:
            if header_addresses(data, identity):
                broken = True
            continue
        value = plain.decode()
        break
    return value, broken


def decrypt_rows(rows: list[dict], identity) -> tuple[list, list, list]:
    """One identity over the locked rows (#515): the pairs of row
    and decrypted value the key opened, the rows whose ciphertext
    stayed ciphertext (no stanza addressed to this key — another
    operator's rows, the design's quiet miss), and the rows
    addressed to this key whose file failed to open — corruption
    the operator remedies with a re-mint (#515 review: the two
    reads carry different remedies, so they name differently). A
    row with several blobs (an owner-wide row, #339) opens under
    the first blob this key unwraps."""
    unlocked: list = []
    left: list = []
    corrupt: list = []
    for row in rows:
        value, broken = row_outcome(row, identity)
        if value is not None:
            unlocked.append((row, value))
        elif broken:
            corrupt.append(row)
        else:
            left.append(row)
    return unlocked, left, corrupt


def value_items(unlocked: list) -> list[dict]:
    """The unlock POST's row items (#514, #515): one value per
    unlocked row, keyed by the row's id — the one builder the CLI
    and the TUI both send."""
    return [
        {"placeholder_id": row["id"], "value": value}
        for row, value in unlocked
    ]


def unlock_body(unlocked: list) -> dict:
    """The POST body for ``/api/v1/secrets/unlock`` (#514)."""
    return {"values": value_items(unlocked)}


# -- the key file ----------------------------------------------------------


def read_key_file(path: Path) -> tuple[bytes, str | None]:
    """The key file's bytes, or the named refusal (#515): a file
    that cannot be read is the one line the flow ends on — the
    exit contract ``read_identity_pem`` raises, carried as the
    line a status line shows."""
    try:
        return read_identity_pem(path), None
    except SystemExit as exc:
        return b"", str(exc)


def load_key(key_pem: bytes, answer: str) -> tuple[object | None, str | None]:
    """The identity loaded under the passphrase answer, or the
    crypto's named refusal (#515 review: a file that is no key, a
    wrong passphrase) — the loaded identity, or the refusal line
    beside None."""
    try:
        return load_identity(key_pem, lambda: answer), None
    except AgeCryptoError as exc:
        return None, f"unlock refused: {exc}"


def needs_passphrase(key_pem: bytes) -> tuple[bool, str | None]:
    """Whether the key file's bytes ask for a passphrase (#515,
    #1147): the encrypted flag, or the crypto's named refusal in
    its place — a file that is no key refuses here, at the
    encrypted check, the way it refuses at the load (#515
    review), so the flow ends on one line either way."""
    try:
        return key_is_encrypted(key_pem), None
    except AgeCryptoError as exc:
        return False, f"unlock refused: {exc}"


def key_file_state(path: Path) -> tuple[bool, str | None]:
    """Whether the key file asks for a passphrase (#1147): the
    encrypted flag the GUI's prompt decision reads, and the
    refusal in its place — a file that cannot be read, or one the
    crypto refuses. The fetch precedes the load (#515 review), so
    a warm store never reaches this read."""
    key_pem, refused = read_key_file(path)
    if refused is not None:
        return False, refused
    return needs_passphrase(key_pem)


def open_key_file(path: Path, answer: str) -> tuple[object | None, str | None]:
    """The key file read and loaded under the answer (#1147): the
    GUI's second flight's load — both refusals named, the
    unreadable file's and the crypto's."""
    key_pem, refused = read_key_file(path)
    if refused is not None:
        return None, refused
    return load_key(key_pem, answer)


# -- the flow's lines ------------------------------------------------------


def prompt_refusal(path: Path, spent_line: str | None) -> str:
    """The empty prompt answer's named line (#515, #920): the
    spent line the automatic re-run passes — a prompt another
    surface already asked — else the cancel line that names
    the passphrase the file needs."""
    if spent_line:
        return spent_line
    return (
        "secrets stay locked — the unlock needs the "
        f"key file's passphrase ({path.name})"
    )


def quiet_line(explicit: bool, named: str) -> str | None:
    """The two quiet gates' shared shape (#515): the mount-time
    flow says nothing when there is nothing to do, and an
    explicit ask — the page's own action — names the line the
    operator's question answers."""
    if not explicit:
        return None
    return named


def locked_refusal(rows: list, corrupt: list) -> str:
    """The nothing-opened outcome (#515): no stanza matched the
    key — another operator's rows — with the rows that failed to
    open named for the re-mint they need."""
    plural = "" if len(rows) == 1 else "s"
    line = (
        f"{len(rows)} placeholder row{plural} stay locked — the "
        "key opens none of them"
    )
    if corrupt:
        line += f" ({len(corrupt)} failed to open; re-mint them)"
    return line


def unlock_outline(unlocked: list, left: list, corrupt: list) -> str:
    """The flow's outcome line: the rows the upload unlocked,
    the ones whose ciphertext failed to open (a re-mint), and
    the ones that stayed ciphertext — another operator's."""
    outline = f"unlocked {len(unlocked)} placeholder row"
    outline += "s" if len(unlocked) != 1 else ""
    if corrupt:
        outline += f"; {len(corrupt)} failed to open (re-mint)"
    if left:
        outline += (
            f"; {len(left)} stay ciphertext (no stanza matches this key)"
        )
    return outline


# -- the flow --------------------------------------------------------------


async def prepare_unlock(
    data, explicit: bool
) -> tuple[list[dict] | None, Path | None, str | None]:
    """The flow's fetch half (#515, #1147): the locked rows the
    daemon reports and the configured key file's path, or the
    flow's early end — the rows None, the path None beside a
    missing identity, and the line naming the quiet case only
    when *explicit* asks (no identity configured, nothing
    locked; None otherwise, nothing to say)."""
    path = configured_identity_file()
    if path is None:
        return None, None, quiet_line(explicit, MISSING_IDENTITY_LINE)
    rows = locked_rows(await data.pending_secrets())
    if not rows:
        return None, path, quiet_line(explicit, NOTHING_LOCKED_LINE)
    return rows, path, None


async def upload_unlocked(data, unlocked: list) -> None:
    """The flow's upload half (#515): the decrypted values back
    over the authenticated API — the one item builder the CLI
    shares."""
    await data.unlock_secrets(value_items(unlocked))


async def settle_unlock(data, rows: list, identity) -> str:
    """The flow's second half under a loaded identity (#515):
    the decrypt, the upload of what opened, and the outcome line
    — the refusal when the key opened nothing, the outline
    otherwise."""
    unlocked, left, corrupt = decrypt_rows(rows, identity)
    if not unlocked:
        return locked_refusal(rows, corrupt)
    await upload_unlocked(data, unlocked)
    return unlock_outline(unlocked, left, corrupt)


async def session_identity(path, ask_passphrase, spent_line=None):
    """The configured key loaded for this session (#515): the
    pair of the unwrap-ready identity and the outcome line that
    ends the flow early — the loaded identity, or the named
    refusal (a cancelled prompt, an unreadable file, a key the
    crypto refuses). The TUI's flow reads the file once: the
    prompt decision and the load share the bytes (the GUI's two
    flights read it once each, :func:`key_file_state` and
    :func:`open_key_file`). *ask_passphrase* owns the encrypted-
    key prompt: the modal in the tree, a constant in tests.
    *spent_line* re-answers an empty answer the caller knows
    was a spent prompt rather than a cancel (#920): the
    automatic re-run passes it; the cancel line stays the
    default."""
    key_pem, refused = read_key_file(path)
    if refused is not None:
        return None, refused
    encrypted, refused = needs_passphrase(key_pem)
    if refused is not None:
        return None, refused
    answer = ""
    if encrypted:
        answer = await ask_passphrase(path)
        if not answer:
            return None, prompt_refusal(path, spent_line)
    return load_key(key_pem, answer)


async def unlock_flow(
    data, ask_passphrase, explicit: bool = False, spent_line=None
) -> str | None:
    """The shared session unlock (#515): the configured identity
    against the daemon's locked rows. Returns the outcome line to
    flash, or None when there is nothing to say (no identity
    configured, or the daemon already holds every row's
    plaintext) — *explicit* turns both quiet cases into named
    lines, the page's ``u`` action answering an operator who
    asked. *spent_line* rides to the prompt's refusal
    (:func:`session_identity`)."""
    rows, path, line = await prepare_unlock(data, explicit)
    if rows is None:
        return line
    identity, refused = await session_identity(
        path, ask_passphrase, spent_line
    )
    if refused is not None:
        return refused
    return await settle_unlock(data, rows, identity)
