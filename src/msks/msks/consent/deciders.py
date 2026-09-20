"""Live decider registry (#69), driven by the events websocket.

A workspace in ``interactive`` mode holds SYNs for a human decision
only while at least one decider watches it — interactivity is
runtime state, not the stored mode (klangk #2308's rule). A decider
is any authenticated events-websocket client that sent an
``egress.decider`` frame naming the workspace; its liveness is the
websocket itself (uvicorn's ping/pong notices a dead peer, the
handler exits, and its teardown deregisters), so there is no
separate ping protocol to keep honest.
"""

import logging

logger = logging.getLogger(__name__)


class DeciderRegistry:
    """``app.state.deciders``: which workspaces have a live decider."""

    def __init__(self) -> None:
        # client id -> the set of workspaces it watches.
        self._watching: dict[int, set[str]] = {}

    def register(self, client_id: int, workspace_id: str) -> None:
        """One events client now decides for ``workspace_id``."""
        self._watching.setdefault(client_id, set()).add(workspace_id)
        logger.info(
            "consent decider registered: ws=%s client=%s",
            workspace_id[:8],
            client_id,
        )

    def deregister(self, client_id: int) -> None:
        """The client's socket closed: it decides for nothing."""
        if self._watching.pop(client_id, None) is not None:
            logger.info("consent decider disconnected: client=%s", client_id)

    def has_decider(self, workspace_id: str) -> bool:
        """True iff at least one live decider watches the workspace."""
        return any(
            workspace_id in watched for watched in self._watching.values()
        )

    def watchers(self, workspace_id: str) -> int:
        """How many live deciders watch the workspace (diagnostics)."""
        return sum(
            1 for watched in self._watching.values() if workspace_id in watched
        )
