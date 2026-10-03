"""The placeholder-secret commands (#339, #423): ``msks secret
mint``, ``ls``, ``revoke``, ``renew``, ``coverage``, and
``check`` — the group the CLI's typer layer
(:mod:`msks.client.cli`) dispatches into. The client duplicates
the coverage and identifier vocabulary per the CLI-isolation
rule (the daemon's tables stay in its own process).
"""

import asyncio
import json

from .context import call
from .tabular import listing_text


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
    transport=None,
) -> int:
    """``msks secret mint`` (#339, #423): one step; the daemon
    mints the value, and this prints it once beside the sentinel.
    No ``--workspace`` mints the daemon-wide row; a target scopes
    it."""
    targets = mint_targets(workspace_refs)
    body: dict = {
        "name": name,
        "dests": dests,
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
    print(f"value (shown once): {row['value']}")
    print(f"sentinel (shown once): {row['sentinel']}")
    return 0


def secret_cells(row: dict) -> list[str]:
    """One placeholder row's cells: id, coverage/name,
    destinations, expiry."""
    return [
        str(row["id"]),
        f"{coverage_label(row['workspaces'])}/{row['name']}",
        ", ".join(row["dests"]),
        row["expires_at"] or "never",
    ]


def cmd_secret_ls(as_json: bool = False, transport=None) -> int:
    """``msks secret ls``: every placeholder with its coverage, no
    sentinels."""
    rows = asyncio.run(call("GET", "/api/v1/secrets", transport=transport))
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    text = listing_text(
        ["id", "coverage", "destinations", "expires"],
        [secret_cells(row) for row in rows],
    )
    if text:
        print(text)
    return 0


def cmd_secret_revoke(
    workspace_refs: list[str] | None, name: str, transport=None
) -> int:
    """``msks secret revoke`` (#339): effective on the next
    request; the targeting mirrors the mint's."""
    row = asyncio.run(find_placeholder(workspace_refs, name, transport))
    result = asyncio.run(
        call(
            "DELETE",
            f"/api/v1/secrets/{row['id']}",
            transport=transport,
        )
    )
    suffix = (
        ""
        if result.get("store_cleaned", True)
        else (" (store value left behind; msks secret check reports it)")
    )
    print(f"revoked {coverage_label(row['workspaces'])}/{name}{suffix}")
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


def cmd_secret_check(transport=None) -> int:
    """``msks secret check``: the configured store answers writes."""
    result = asyncio.run(
        call("POST", "/api/v1/secrets/check", transport=transport)
    )
    print(f"secret store ({result['provider']}): ok")
    return 0
