"""The daemon-wide record of refused tap-listener binds (#533).

The per-tap service listeners — the interceptor, the LLM proxy,
the probe endpoint — bind best-effort at attach: a refused bind
(another service holding the port, an address the host cannot
bind) logs one line and the workspace boots without that surface,
and until its next start nothing else names the absence. This
registry is that name: the net manager records each refused bind
as it catches it, the interceptor records its own bring-up
refusals, and the API's workspace rows carry the record as a field
the client surfaces read — ``msks ls``'s notice, the TUI page's
line, ``msks doctor``'s check.

The record is boot-scoped daemon memory: a successful rebind or
the workspace's detach clears it, and nothing persists it past
the daemon's lifetime — the database row stays a workspace's
permanent facts only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ListenerFault:
    """One refused bind: the service, the port it meant to hold,
    and the error the bind raised."""

    service: str
    port: int
    error: str


class ListenerFaults:
    """Each workspace's refused binds, keyed by service name."""

    def __init__(self) -> None:
        self._faults: dict[str, dict[str, ListenerFault]] = {}

    def record(
        self, workspace_id: str, service: str, port: int, error: str
    ) -> None:
        """Record (or replace) one service's refusal for a
        workspace — the latest attempt's error is the one an
        operator reading the record can act on."""
        self._faults.setdefault(workspace_id, {})[service] = ListenerFault(
            service, port, error
        )

    def clear(self, workspace_id: str, service: str) -> None:
        """Drop one service's fault: the listener bound, or its
        absence became intentional."""
        services = self._faults.get(workspace_id)
        if services is None:
            return
        services.pop(service, None)
        if not services:
            del self._faults[workspace_id]

    def forget(self, workspace_id: str) -> None:
        """Drop a workspace's whole record — detach: the endpoints
        are down because the workspace is, not because a bind was
        refused."""
        self._faults.pop(workspace_id, None)

    def payload(self, workspace_id: str) -> dict[str, dict[str, int | str]]:
        """The API row's field: each service's port and bind error,
        the services ordered by name for a stable display."""
        return {
            service: {"port": fault.port, "error": fault.error}
            for service, fault in sorted(
                self._faults.get(workspace_id, {}).items()
            )
        }
