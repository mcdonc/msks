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
"""

import base64
from pathlib import Path

from msks.agecrypto import (
    AgeCryptoError,
    addressed,
    decrypt_with_identity,
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
    row with several blobs (a daemon-wide row, #339) opens under
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
