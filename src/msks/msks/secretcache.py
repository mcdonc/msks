"""The unlock-time plaintext cache and the mint's blob encryption
(#514, #504).

The daemon never holds a secret's plaintext at rest anymore: a
mint stores only per-recipient ciphertext blobs (the age files of
the crypto leaf, #513), and the plaintext returns to the daemon
solely through an operator's unlock — the client (#515) decrypts
the blobs with the workspace identity's private half and posts
the values back over the token-authenticated API. Until that
happens the daemon cannot swap: the interceptor's resolve step
reads this cache and a cold read is a named refusal, never a
sentinel passing through as a value.

The window is the ``ssh-add -t`` pattern: accepted plaintext
lives for ``MSKSD_SECRET_STORE_UNLOCK_TIMEOUT_S`` seconds past the
unlock that supplied it. The timer resets only on an unlock POST
itself — an explicit operator action carrying plaintext. Websocket
traffic, keepalives, and scheduled tasks never extend the window;
a settings value of zero holds plaintext only while at least one
operator websocket stays connected — the last one's disconnect
drops it, and an unlock over HTTP alone holds nothing past the
reply.

A standalone leaf (rank 0): the interceptor imports the locked
error, the daemon's API and store import the cache and the
refusal vocabulary. Nothing here touches the crypto leaf, the
model, or the settings modules — the live timeout is read off the
app the same way every subsystem reads its settings.
"""

from datetime import UTC, datetime, timedelta


def now_utc() -> datetime:
    """The clock every cache deadline reads (a test seam)."""
    return datetime.now(UTC)


#: The absence marker for served lookups: a served marker can be
#: session-held (a None deadline, alive), so None itself cannot
#: mean "absent" there.
_UNSERVED = object()


class SecretLockedError(Exception):
    """A plaintext read against a cold or expired cache (#514).

    The named refusal behind two surfaces: the interceptor's swap
    path answers the guest with a locked body instead of
    forwarding, and the swap never hangs and never substitutes the
    sentinel for the value.
    """


class NotEncryptableError(Exception):
    """A mint refused because a covering workspace's key cannot
    receive a blob (#514). Carries the workspace id and the
    constraint the key failed, so the refusal names both."""

    def __init__(self, workspace_id: str, reason: str) -> None:
        super().__init__(
            f"workspace {workspace_id} cannot receive a secret blob: {reason}"
        )
        self.workspace_id = workspace_id
        self.reason = reason


class PlaintextCache:
    """Ref-keyed plaintext with a deadline (#514): the store's
    cache, timed. Caches only ``self.app`` (the ownership rule) —
    the timeout is read live so a SIGHUP settings swap applies to
    the next store without a restart."""

    def __init__(self, app) -> None:
        self.app = app
        # ref -> (value, deadline); a None deadline is a
        # session-held entry (timeout zero): it lives only while
        # at least one operator websocket stands (the count below).
        self._values: dict[str, tuple[str, datetime | None]] = {}
        # placeholder id -> served deadline: which rows' ciphertext
        # the pending list served, so an unlock can require it.
        self._served: dict[int, datetime | None] = {}
        # How many operator websockets stand connected: a
        # session-held entry is live exactly while this is
        # positive, so an HTTP-only unlock holds nothing past its
        # reply and the LAST socket's disconnect (not the first)
        # drops the session (#514 review).
        self._operators = 0
        self._clock = now_utc

    def timeout_s(self) -> float:
        """The live unlock window, in seconds (zero: drop on the
        operator's disconnect)."""
        return self.app.state.settings.secret_store.unlock_timeout_s

    def deadline(self) -> datetime | None:
        """The deadline a fresh entry takes: the clock plus the
        window, or None when the window is zero (session-held)."""
        timeout = self.timeout_s()
        return self._clock() + timedelta(seconds=timeout) if timeout else None

    def store(self, values: dict[str, str]) -> None:
        """Accept plaintext for refs (#514): each entry takes a
        fresh deadline — this is the unlock, the one event that
        resets the timer."""
        stamp = self.deadline()
        for ref, value in values.items():
            self._values[ref] = (value, stamp)

    def served(self, placeholder_ids: list[int]) -> None:
        """Record which rows the pending list served, and when
        their window ends: an unlock answers only rows whose
        ciphertext went out under a live window (#508)."""
        stamp = self.deadline()
        for placeholder_id in placeholder_ids:
            self._served[placeholder_id] = stamp

    def live(self, deadline: datetime | None, now: datetime) -> bool:
        """Whether a deadline still holds: session-held entries
        live exactly while an operator socket stands, dated ones
        until the clock passes them."""
        if deadline is None:
            return self._operators > 0
        return deadline > now

    def operator_connected(self) -> None:
        """An operator websocket was accepted: the session (a
        zero window's whole lifetime) stands while one remains."""
        self._operators += 1

    def operator_disconnected(self) -> None:
        """An operator websocket closed (#514): the session-held
        entries go when the LAST one closes — one operator's
        disconnect never drops another's live session. Dated
        entries keep their deadline: their window is the clock's,
        not the socket's."""
        if self._operators:
            self._operators -= 1
        if self._operators == 0:
            self.drop_session_entries()

    def read(self, ref: str) -> str:
        """One ref's plaintext — the swap path's read. A cold or
        expired ref raises the named locked error (and the expired
        entry dies here, so the memory does not outlive the
        window)."""
        entry = self._values.get(ref)
        if entry is None:
            raise SecretLockedError(ref)
        value, deadline = entry
        if not self.live(deadline, self._clock()):
            del self._values[ref]
            raise SecretLockedError(ref)
        return value

    def knows(self, ref: str) -> bool:
        """Whether a ref's plaintext stands ready — the pending
        list's and the row fetch's lock indicator. Expired entries
        read cold (and drop, the read's own rule)."""
        try:
            self.read(ref)
        except SecretLockedError:
            return False
        return True

    def served_alive(self, placeholder_id: int) -> bool:
        """Whether a row's ciphertext was served under the live
        window — the unlock's precondition."""
        deadline = self._served.get(placeholder_id, _UNSERVED)
        if deadline is _UNSERVED:
            return False
        if self.live(deadline, self._clock()):
            return True
        del self._served[placeholder_id]
        return False

    def dead_keys(self, deadlines: dict, now: datetime) -> list:
        """The keys whose deadline passed — one rule for the
        values and the served markers alike."""
        return [
            key
            for key, deadline in deadlines.items()
            if not self.live(deadline, now)
        ]

    def value_deadlines(self) -> dict:
        """The values' deadlines alone, keyed by ref."""
        return {ref: deadline for ref, (_, deadline) in self._values.items()}

    def expired_keys(self, now: datetime) -> list:
        """Every key past its deadline, values and served markers
        together (a str ref and an int placeholder id never
        collide)."""
        return [
            *self.dead_keys(self.value_deadlines(), now),
            *self.dead_keys(self._served, now),
        ]

    def flush_expired(self) -> int:
        """Drop every dated entry whose deadline passed; returns
        how many went. The watcher calls it every loop so the
        memory flushes on time even with no swap reading it."""
        dead = self.expired_keys(self._clock())
        for key in dead:
            self._values.pop(key, None)
            self._served.pop(key, None)
        return len(dead)

    def drop_session_entries(self) -> None:
        """Keep only the entries that carry a deadline: the
        session-held ones (a None deadline) leave now."""
        self._values = {
            ref: entry
            for ref, entry in self._values.items()
            if entry[1] is not None
        }
        self._served = {
            placeholder_id: deadline
            for placeholder_id, deadline in self._served.items()
            if deadline is not None
        }

    def clear(self) -> None:
        """Drop everything (a SIGHUP settings swap; the next unlock
        re-populates under the new settings)."""
        self._values.clear()
        self._served.clear()

    def forget(self, refs: list[str], placeholder_ids: list[int]) -> None:
        """Drop named refs' plaintext and rows' served markers (a
        mint that rolled back its row, a revoke, an expiry —
        #514): the swap path would refuse the row anyway, and a
        marker left behind would admit an unlock for a rowid a
        later re-mint can inherit."""
        for ref in refs:
            self._values.pop(ref, None)
        for placeholder_id in placeholder_ids:
            self._served.pop(placeholder_id, None)
