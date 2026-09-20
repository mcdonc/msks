"""HMAC audit tags for consent rows (#69)."""

from msks.model.audit_hmac import (
    canonical_pairs,
    compute_egress_consent_hmac,
    resolve_audit_key,
)
from msks.settings import ServerSettings, Settings


def settings_with(key: str | None) -> Settings:
    return Settings(server=ServerSettings(audit_hmac_key=key))


def test_tagging_is_opt_in() -> None:
    assert resolve_audit_key(settings_with(None)) is None
    assert resolve_audit_key(settings_with("")) is None
    assert resolve_audit_key(settings_with("k1")) == b"k1"


def test_compute_tags_when_a_key_is_set() -> None:
    row = {
        "id": "r1",
        "workspace_id": "ws",
        "dest_host": "example.com",
        "dest_port": 443,
        "decision": "allowed",
        "duration": "forever",
        "requested_at": 1.0,
        "decided_at": 2.0,
        "decided_by": "token",
        "revoked_at": None,
        "revoked_by": None,
    }
    assert compute_egress_consent_hmac(settings_with(None), row) is None
    tag = compute_egress_consent_hmac(settings_with("k1"), row)
    assert tag is not None and len(tag) == 64
    # Deterministic: same row, same tag; a changed field, new tag.
    assert compute_egress_consent_hmac(settings_with("k1"), row) == tag
    row["decision"] = "denied"
    assert compute_egress_consent_hmac(settings_with("k1"), row) != tag


def test_canonical_pairs_is_prefix_free_and_injective() -> None:
    columns = ["a", "b"]
    one = canonical_pairs("t", {"a": "x", "b": None}, columns)
    other = canonical_pairs("t", {"a": "x=1", "b": "n"}, columns)
    assert one == b"t\x00a=1:x\x00b=n"
    assert other == b"t\x00a=3:x=1\x00b=1:n"
    assert one != other  # a literal "n" cannot impersonate a NULL
