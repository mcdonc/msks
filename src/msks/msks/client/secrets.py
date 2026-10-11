"""The placeholder-secret commands (#339, #423): ``msks secret
mint``, ``ls``, ``revoke``, ``renew``, ``coverage``, and
``unlock`` (#515) — the group the CLI's typer layer
(:mod:`msks.client.cli`) dispatches into. The client duplicates
the coverage and identifier vocabulary per the CLI-isolation
rule (the daemon's tables stay in its own process).
"""

import asyncio
import getpass
import json
import sys
from pathlib import Path

from msks.agecrypto import (
    AgeCryptoError,
    key_is_encrypted,
    load_identity,
)

from .context import call
from .ssh import configured_identity_file
from .tabular import listing_text
from .unlock import (
    decrypt_rows,
    locked_rows,
    read_identity_pem,
    unlock_body,
)


def read_secret(path: str) -> str:
    """The mint's payload (#198, #423): a file's contents, or
    stdin for ``-``.

    Whitespace-stripped at both ends — a token file's trailing
    newline (or a password manager's) is not part of the secret —
    and never accepted as a command-line argument, which lands in
    process lists and shell history. A UTF-8 BOM is dropped the
    same way: an export saved with one (a Windows-side password
    manager) would otherwise mint a credential with an invisible
    prefix that no external service accepts.
    """
    try:
        if path == "-":
            if sys.stdin.isatty():
                raise SystemExit(
                    "msks: --secret-file - expects the secret on stdin "
                    "(pipe it in; it is never read interactively)"
                )
            text = sys.stdin.read()
        else:
            text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(
            f"msks: cannot read secret file {path}: {exc}"
        ) from None
    value = text.strip().lstrip("\ufeff").strip()
    if not value:
        raise SystemExit("msks: the secret file is empty")
    return value


def coverage_label(workspaces: list[str]) -> str:
    """The display name of a row's coverage (#339, duplicated from
    the daemon per the CLI-isolation rule): ``*`` is the
    daemon-wide row, a comma-joined list is scoped."""
    return ",".join(sorted(set(workspaces))) if workspaces else "*"


def resolve_coverage(
    workspaces: list[dict], refs: list[str] | None
) -> list[str]:
    """The sorted id list a --workspace target names (#339), or the
    named exit when a ref answers to no workspace. No target is
    the daemon-wide coverage: ``[]``. The refs take the same
    repeatable, comma-splitting, whitespace-stripping form the
    mint's flag does, so a row minted with one command line is
    revoked with the same one."""
    if not refs:
        return []
    expanded = expand_targets(refs)
    if not expanded:
        raise SystemExit(
            "msks: --workspace names no workspace; omit the flag to "
            "target the daemon-wide row"
        )
    return sorted({resolved_workspace_id(workspaces, ref) for ref in expanded})


async def find_placeholder(
    refs: list[str] | None, name: str, transport
) -> dict:
    """The (coverage, name) pair's row, or a named exit (#339).

    ``refs`` carry the same targeting the mint took — absent names
    the daemon-wide row, a list names the row whose coverage is
    exactly that set. Each ref names a workspace by id or name
    (#246); the listing's workspace rows carry both, and the
    placeholder row binds to the immutable ids, so the refs
    resolve against the listing before the pair matches.
    """
    workspaces = await call("GET", "/api/v1/workspaces", transport=transport)
    coverage = resolve_coverage(workspaces, refs)
    rows = await call("GET", "/api/v1/secrets", transport=transport)
    for row in rows:
        if sorted(set(row["workspaces"])) == coverage and row["name"] == name:
            return row
    label = coverage_label(coverage)
    hint = (
        " (no --workspace targets the daemon-wide row; a target "
        "names the row whose coverage is exactly that set)"
    )
    raise SystemExit(f"msks: no placeholder {name} covering {label}{hint}")


def resolved_workspace_id(workspaces: list[dict], ref: str) -> str:
    """The immutable id the ref names (#246), or the named exit
    when no workspace answers to it."""
    for row in workspaces:
        if ref in (row["id"], row.get("name")):
            return row["id"]
    raise SystemExit(f"msks: no such workspace: {ref}")


def mint_targets(workspace_refs: list[str] | None) -> list[str] | None:
    """The mint's coverage refs (#339): None (no --workspace) is
    the daemon-wide mint; a flag whose every segment is empty
    names no workspace — and an empty target list is the daemon-
    wide mint, the broadest row there is, so the mismatch is
    refused here, not minted."""
    if workspace_refs is None:
        return None
    targets = expand_targets(workspace_refs)
    if not targets:
        raise SystemExit(
            "msks: --workspace names no workspace; omit the flag to "
            "mint the daemon-wide placeholder"
        )
    return targets


def expand_targets(refs: list[str]) -> list[str]:
    """The flat ref list a repeatable, comma-splitting flag carries
    (#339): ``--workspace a,b --workspace c`` becomes
    ``[a, b, c]``, each ref whitespace-stripped, empty segments
    dropped."""
    return [ref.strip() for ref in ",".join(refs).split(",") if ref.strip()]


def cmd_secret_mint(
    workspace_refs: list[str] | None,
    name: str,
    dests: list[str],
    ttl: int | None,
    secret_file: str,
    transport=None,
) -> int:
    """``msks secret mint`` (#339, #423): one step; the operator's
    value rides the request and is never echoed — the sentinel
    prints once. No ``--workspace`` mints the daemon-wide row; a
    target scopes it."""
    value = read_secret(secret_file)
    targets = mint_targets(workspace_refs)
    body: dict = {
        "name": name,
        "dests": dests,
        "value": value,
    }
    if targets:
        body["workspaces"] = targets
    if ttl is not None:
        body["ttl_s"] = ttl
    row = asyncio.run(
        call("POST", "/api/v1/secrets", json_body=body, transport=transport)
    )
    print(
        f"minted {coverage_label(row['workspaces'])}/{name} "
        f"for {', '.join(row['dests'])}"
    )
    print(f"sentinel: {row['sentinel']}")
    return 0


def secret_cells(row: dict) -> list[str]:
    """One placeholder row's cells: id, coverage/name,
    destinations, expiry, lock state (#515)."""
    return [
        str(row["id"]),
        f"{coverage_label(row['workspaces'])}/{row['name']}",
        ", ".join(row["dests"]),
        row["expires_at"] or "never",
        "locked" if row.get("locked") else "open",
    ]


def cmd_secret_ls(as_json: bool = False, transport=None) -> int:
    """``msks secret ls``: every placeholder with its coverage, no
    sentinels."""
    rows = asyncio.run(call("GET", "/api/v1/secrets", transport=transport))
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    text = listing_text(
        ["id", "coverage", "destinations", "expires", "state"],
        [secret_cells(row) for row in rows],
    )
    if text:
        print(text)
    return 0


def unlock_identity(path):
    """The named key file loaded for one command run (#515): a
    passphrase-encrypted file prompts once — getpass on the tty —
    and the loaded identity decrypts every row."""
    key_pem = read_identity_pem(path)
    if key_is_encrypted(key_pem):
        answer = getpass.getpass(f"passphrase for {path}: ")
        return load_identity(key_pem, lambda: answer)
    return load_identity(key_pem, lambda: "")


def row_names(rows: list) -> str:
    """The report's name list: coverage/name per row, joined."""
    return ", ".join(
        f"{coverage_label(row['workspaces'])}/{row['name']}" for row in rows
    )


def report_unlock(unlocked: list, left: list, corrupt: list) -> None:
    """The command's report (#515): one line per unlocked row,
    then the rows that failed to open (a re-mint remedies them)
    and the rows that stayed ciphertext — another operator's —
    each named when any stand."""
    for row, _value in unlocked:
        print(f"unlocked {coverage_label(row['workspaces'])}/{row['name']}")
    if corrupt:
        print(f"ciphertext failed to open (re-mint): {row_names(corrupt)}")
    if left:
        print(
            "stayed ciphertext (no stanza matches this key): "
            f"{row_names(left)}"
        )


def missing_identity_line() -> str:
    """The no-identity refusal's one line: the setting that
    points at the key, and the variable that presets it."""
    return (
        "msks: the unlock needs the workspace identity's private "
        "half — point identity_file (the client config) or "
        "MSKSC_IDENTITY_FILE at your key file"
    )


def cmd_secret_unlock(transport=None) -> int:
    """``msks secret unlock`` (#515): the client's half of the
    store — fetch the pending ciphertexts, decrypt the blobs the
    configured identity file opens, upload the plaintext. The
    rows the key cannot open stay ciphertext on the daemon and
    are named here, not retried: possession of the private key is
    the authority for a value, and a client without it learns
    nothing."""
    path = configured_identity_file()
    if path is None:
        print(missing_identity_line(), file=sys.stderr)
        return 1
    rows = asyncio.run(
        call("GET", "/api/v1/secrets/pending", transport=transport)
    )
    rows = locked_rows(rows)
    if not rows:
        print("every placeholder row is unlocked")
        return 0
    return upload_unlock(path, rows, transport)


def upload_unlock(path, rows, transport) -> int:
    """The load-decrypt-upload half (#515): the key loads only
    now — a store with nothing locked never prompted for its
    passphrase (#515 review). The report names every outcome,
    and the exit code names completeness — a run whose rows
    broke ends incomplete (the re-mint is the operator's next
    act), while rows that are simply another operator's stayed
    ciphertext by design and that run completes clean."""
    try:
        identity = unlock_identity(path)
    except AgeCryptoError as exc:
        print(
            f"msks: the identity file {path} refused: {exc}",
            file=sys.stderr,
        )
        return 1
    unlocked, left, corrupt = decrypt_rows(rows, identity)
    if not unlocked:
        report_unlock(unlocked, left, corrupt)
        return 1 if corrupt else 0
    asyncio.run(
        call(
            "POST",
            "/api/v1/secrets/unlock",
            json_body=unlock_body(unlocked),
            transport=transport,
        )
    )
    report_unlock(unlocked, left, corrupt)
    return 0


def cmd_secret_revoke(
    workspace_refs: list[str] | None, name: str, transport=None
) -> int:
    """``msks secret revoke`` (#339): effective on the next
    request; the targeting mirrors the mint's."""
    row = asyncio.run(find_placeholder(workspace_refs, name, transport))
    asyncio.run(
        call(
            "DELETE",
            f"/api/v1/secrets/{row['id']}",
            transport=transport,
        )
    )
    print(f"revoked {coverage_label(row['workspaces'])}/{name}")
    return 0


def cmd_secret_renew(
    workspace_refs: list[str] | None,
    name: str,
    ttl: int,
    transport=None,
) -> int:
    """``msks secret renew`` (#339): extends in place, sentinel
    unchanged; the targeting mirrors the mint's."""
    row = asyncio.run(find_placeholder(workspace_refs, name, transport))
    updated = asyncio.run(
        call(
            "POST",
            f"/api/v1/secrets/{row['id']}/renew",
            json_body={"ttl_s": ttl},
            transport=transport,
        )
    )
    print(
        f"renewed {coverage_label(row['workspaces'])}/{name}; "
        f"expires {updated['expires_at']}"
    )
    return 0


def cmd_secret_coverage(
    workspace_id: str, coverage: str, transport=None
) -> int:
    """``msks secret coverage`` (#339): flip a workspace's
    daemon-wide placeholder posture — ``all`` takes daemon-wide
    coverage, ``scoped`` exempts the workspace from it."""
    result = asyncio.run(
        call(
            "PUT",
            f"/api/v1/workspaces/{workspace_id}/secret-coverage",
            json_body={"secret_coverage": coverage},
            transport=transport,
        )
    )
    print(
        f"workspace {workspace_id} secret coverage: "
        f"{result['secret_coverage']}"
    )
    return 0
