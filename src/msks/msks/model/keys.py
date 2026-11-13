"""The model's token and coverage vocabulary.

The bearer-token entropy, hashing, and minting, beside the
coverage set's canonical stored form and the blob classifier
the placeholder swap walks.
"""

from __future__ import annotations

import hashlib
import json
import secrets

from ..agecrypto import secret_key_hash
from ..spec.tokens import validate_token_plaintext


def coverage_key(workspaces: list[str]) -> str:
    """The canonical stored form of a coverage set: sorted,
    de-duplicated ids as JSON (#339). An empty list is the
    daemon-wide row and stays ``[]``."""
    return json.dumps(sorted(set(workspaces)))


TOKEN_ENTROPY_BYTES = 32


def classify_blobs(expected: dict[str, str], blobs: dict[str, str]) -> dict:
    """The staleness rule over two maps (#513): a recipient whose
    workspace has no blob row is missing, one whose blob names
    another key hash is stale (the key rotated), and a matching
    hash is fresh. Pure — the rows alone decide."""
    missing = []
    stale = []
    for workspace_id, line in expected.items():
        hash_seen = blobs.get(workspace_id)
        if hash_seen is None:
            missing.append(workspace_id)
        elif hash_seen != secret_key_hash(line):
            stale.append(workspace_id)
    return {"missing": sorted(missing), "stale": sorted(stale)}


# Inside the package, so the wheel ships it: a pip-installed msksd
# can run its migrations (the package build inherits this).


def hash_token(plaintext: str) -> str:
    """The stored form of a bearer token (sha256 hex)."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def new_token() -> str:
    """A fresh bearer token plaintext (shown once, stored hashed).

    The plaintext passes the grammar guard: tokens ride the
    ``Authorization`` header on the REST surface and the websocket
    handshake (#216), where a stray separator would fail the
    header-form guard before any dial.
    """
    return validate_token_plaintext(secrets.token_urlsafe(TOKEN_ENTROPY_BYTES))
