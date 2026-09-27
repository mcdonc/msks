"""Deadline predicates over the naive-UTC deadlines the store
keeps (#387).

Stdlib-only leaf: the watcher (placeholder retirement) and the
interceptor (verdict expiry) consult the same rule, which lived in
the HTTP layer before the relocation and dragged the interceptor
up with it.
"""

from datetime import UTC, datetime


def deadline_passed(expires_at: str | None, now: datetime) -> bool:
    """Whether a placeholder's deadline is past. The stored deadline
    is naive UTC on the sqlite round-trip (the dialect strips
    tzinfo at bind); replace() unconditionally normalizes."""
    if expires_at is None:
        return False
    deadline = datetime.fromisoformat(expires_at).replace(tzinfo=UTC)
    return deadline <= now
