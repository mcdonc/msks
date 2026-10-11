"""The secret store's vocabulary and the unlock-time cache owner
(#198, #423, #514).

A placeholder's real value lives as per-recipient ciphertext blobs
in the daemon's database (#513, #514): one age file per covering
workspace identity key, wrapped to that workspace's ssh public
half. The daemon holds no key that opens them — the client (#515)
decrypts with the workspace identity's private half and posts the
plaintext back through the unlock exchange, and until that
happens the swap path refuses with the named locked error.

This module owns the row-level vocabulary every surface shares —
the sentinel formats (#198, #339), the backend-ref grammar that
names a row's cache key, the blob-mint helpers over the age
crypto leaf — and the ``SecretStore`` object itself, which the
application's state holds as the owner of the unlock-time
plaintext cache (:mod:`msks.secretcache`).

The pre-#514 SecretSpec agefile store is retired with the blob
move: values stopped landing in the agefile when mints went to
blobs, and the agefile, its identity, the generated manifest, and
the ``secretspec`` CLI setting went with it — the settings a
stale config still names are refused at load with the removal
named.
"""

import re
import secrets as pysecrets

from .agecrypto import (
    age_capable,
    encrypt,
    secret_key_hash,
)
from .secretcache import NotEncryptableError, PlaintextCache

#: The scoped sentinel format: versioned prefix + 32 random bytes
#: base64url — uniform fixed length, so the wire matcher can
#: recognize it without parsing (#198).
SENTINEL_PREFIX = "mskssec1_"

#: The daemon-wide sentinel format (#339): the same body under a
#: distinct prefix, so an operator who finds a sentinel in a log
#: or a guest can tell its reach from the string alone —
#: ``mskssec1_`` swaps on its row's workspaces, ``mskssec2_`` on
#: every accepting workspace's tap.
DAEMON_SENTINEL_PREFIX = "mskssec2_"

#: The prefix every scoped backend ref carries. The ref is the
#: row's identity in the plaintext cache and the blob rows' join
#: name — a stable, identifier-safe spelling of the row's
#: coverage and label.
REF_PREFIX = "MSKSWS_"

#: The prefix a daemon-wide row's backend ref carries (#339): a
#: ref family of its own, so the store's namespace names the
#: row's reach the same way the sentinel prefix does.
DAEMON_REF_PREFIX = "MSKSDAEMON_"

#: The audit/ref identifier rule the mint enforces on names:
#: letters, numbers, and underscores, no leading digit.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def new_sentinel(daemon_wide: bool = False) -> str:
    """A fresh placeholder sentinel (#198, #339): the
    daemon-wide row mints under ``mskssec2_``, a scoped row under
    ``mskssec1_``."""
    prefix = DAEMON_SENTINEL_PREFIX if daemon_wide else SENTINEL_PREFIX
    return prefix + pysecrets.token_urlsafe(32)


def backend_ref(workspaces: list[str], name: str) -> str:
    """The backend ref for one placeholder (#339: *workspaces* is
    the row's coverage set).

    Scoped: ``MSKSWS_<WS...>_<NAME>`` — identifier-safe (dashes
    and dots become underscores), uppercase, the coverage's sorted
    ids in order. Daemon-wide (an empty coverage): a ref of its
    own family, ``MSKSDAEMON_<NAME>``. Two coverage/name pairs
    that sanitize identically collide on the unique index — the
    mint answers 409 rather than silently sharing a ref.
    """
    parts = [
        re.sub(r"[^A-Za-z0-9_]", "_", part).upper()
        for part in (
            [*sorted(set(workspaces)), name] if workspaces else [name]
        )
    ]
    prefix = REF_PREFIX if workspaces else DAEMON_REF_PREFIX
    return prefix + "_".join(parts)


def valid_name(name: str) -> bool:
    """Whether *name* is a mintable placeholder label."""
    return bool(_IDENTIFIER.match(name))


def blob_recipients(value: bytes, recipients: dict[str, str]):
    """Encrypt *value* to every recipient, keyed by workspace id
    (#514): one age file per key line, plus the line's staleness
    hash. Raises :class:`NotEncryptableError` naming the first
    recipient whose key is not age-capable — the mint's named
    refusal."""
    blobs: dict[str, tuple[str, bytes]] = {}
    for workspace_id, line in sorted(recipients.items()):
        if not age_capable(line):
            kind = line.split(None, 1)[0] if line.split() else "(empty)"
            raise NotEncryptableError(
                workspace_id,
                f"its key type {kind} is not one the secret store can "
                "encrypt to (ssh-ed25519 or ssh-rsa required)",
            )
        blobs[workspace_id] = (
            secret_key_hash(line),
            encrypt(value, line),
        )
    return blobs


def eligible_recipients(recipients: dict[str, str]) -> dict[str, str]:
    """The subset of *recipients* whose keys can hold a blob
    (#514): the unlock re-encrypts to every recipient it can and
    leaves the rest to staleness — one workspace's rotated-in
    incapable key never blocks the row's others from healing."""
    return {
        workspace_id: line
        for workspace_id, line in recipients.items()
        if age_capable(line)
    }


def recipient_hashes(recipients: dict[str, str]) -> dict[str, str]:
    """Each recipient's staleness hash (#513): the token a blob
    row records at mint, so a caller can tell a standing blob set
    from a stale one without encrypting anything."""
    return {
        workspace_id: secret_key_hash(line)
        for workspace_id, line in recipients.items()
    }


class SecretStore:
    """The unlock-time plaintext cache's owner; caches only
    ``self.app`` (the klangk ownership rule). Every surface
    reaches the cache as ``app.state.secrets.plain``."""

    def __init__(self, app) -> None:
        self.app = app
        self.plain = PlaintextCache(app)

    def cache_clear(self) -> None:
        """Drop every cached plaintext (a SIGHUP settings swap; the
        next unlock re-populates under the new settings' window)."""
        self.plain.clear()
