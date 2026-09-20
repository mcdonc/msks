"""HMAC integrity tags for consent audit rows (#69), ported from
klangk's ``model/audit_hmac.py``.

Each ``egress_consent`` row carries an HMAC-SHA256 tag computed
over a canonical serialization of its data columns at write time.
An external checker holding the same key can re-compute the tag and
detect a row modified after it was written; msksd itself only
writes tags, never verifies them.

The key is ``MSKSD_AUDIT_HMAC_KEY``. Tagging is opt-in: with the
key unset, no tag is computed or stored, and rows written while
tagging is disabled carry a NULL tag. The key is read live off the
settings, so a SIGHUP reload turns tagging on for later rows
without touching earlier ones.

FIPS posture: the digest goes through :mod:`hmac`/``hashlib` — the
process's own OpenSSL — pinning nothing in this module.
"""

import hashlib
import hmac

# The columns the tag covers, in canonical order. ``hmac`` itself
# is excluded (it is the tag).
EC_HMAC_COLUMNS = [
    "id",
    "workspace_id",
    "dest_host",
    "dest_port",
    "decision",
    "duration",
    "requested_at",
    "decided_at",
    "decided_by",
    "revoked_at",
    "revoked_by",
]


def resolve_audit_key(settings) -> bytes | None:
    """The configured HMAC key bytes, or None when tagging is off."""
    explicit = getattr(settings.server, "audit_hmac_key", None)
    return explicit.encode() if explicit else None


def canonical_pairs(table: str, row: dict, columns: list[str]) -> bytes:
    """Deterministic serialization: ``table\\0col=len:value\\0…``.

    ``None`` encodes as the bare marker ``n``; every other value as
    ``<len>:<str>`` — length-prefixed, so the encoding is
    prefix-free and injective for any column content, including the
    attacker-influenced ``dest_host`` that comes from inside an
    untrusted workspace: no value can splice fields or impersonate
    another column's NULL."""
    parts = [table]
    for col in columns:
        val = row.get(col)
        if val is None:
            parts.append(f"{col}=n")
        else:
            text = str(val)
            parts.append(f"{col}={len(text)}:{text}")
    return "\0".join(parts).encode()


def compute_egress_consent_hmac(settings, row: dict) -> str | None:
    """The HMAC tag for one consent row dict, or None when tagging
    is disabled (no key configured)."""
    key = resolve_audit_key(settings)
    if key is None:
        return None
    payload = canonical_pairs("egress_consent", row, EC_HMAC_COLUMNS)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()
