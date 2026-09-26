"""The workspace TUI's data (#309): the daemon's REST surface
through the same client conventions ``msksc`` uses — the TUI holds
no direct daemon-state access.

One :class:`TuiData` method per screen action, each the same
exchange the matching CLI subcommand makes. Every call opens its
own client on the shared TLS context (the TOFU warning prints once,
before the first screen draws), and the screens plug fakes here for
the tests.
"""

from pathlib import Path

from ..create import create_workspace_core, invoking_user
from ..rest import api_call, env_token, env_url
from .consent_ui import shared_ssl

#: The create form's identity mode (#309): the per-workspace
#: client mint, taken explicitly — the keypair is minted on this
#: client, the public half travels, the private half is written
#: mode 0600 under the client data root. The CLI's bare-create
#: default (#336) plants the operator's own key instead; the TUI
#: form keeps the per-workspace mint until it grows an operator-key
#: surface of its own.
TUI_KEY_TYPE = "ed25519"


class TuiData:
    """The screens' daemon calls; one instance per app run."""

    def __init__(self, transport=None) -> None:
        self.transport = transport

    async def workspaces(self) -> list[dict]:
        """GET the workspaces listing — the main screen's rows."""
        return await api_call(
            "GET",
            env_url(),
            env_token(),
            "/api/v1/workspaces",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
        )

    async def images(self) -> list[dict]:
        """GET the image catalog — the create form's image select
        (the same listing ``msksc image ls`` prints)."""
        return await api_call(
            "GET",
            env_url(),
            env_token(),
            "/api/v1/images",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
        )

    async def create_defaults(self) -> dict:
        """GET the create defaults — the root/home sizes a create
        lands on when its body leaves them unset (the create
        form's size placeholders hint them)."""
        return await api_call(
            "GET",
            env_url(),
            env_token(),
            "/api/v1/create-defaults",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
        )

    async def create(self, body: dict) -> tuple[dict, Path | None]:
        """POST one workspace (the create form's fields), the client
        mint for its identity — ``(row, private-half path)``."""
        body.setdefault("user", invoking_user())
        return await create_workspace_core(
            env_url(),
            env_token(),
            body,
            self.transport,
            ssl_ctx=shared_ssl(),
            key_type=TUI_KEY_TYPE,
        )

    async def resize(self, workspace_id: str, body: dict) -> dict:
        """POST the resize — the edit dialog's sizes and topology
        (#331), the same exchange ``msks resize`` makes. The daemon
        owns the stopped-workspace rule: a workspace that is not
        stopped answers the named 409, which the page flashes."""
        return await api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/resize",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
            json_body=body,
        )

    async def start(self, workspace_id: str) -> dict:
        """POST the boot — the same call ``msks start`` makes."""
        return await api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/start",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
        )

    async def stop(self, workspace_id: str) -> dict:
        """POST the graceful power-off — ``msks stop``."""
        return await api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/stop",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
        )

    async def remove(self, workspace_id: str) -> dict:
        """DELETE the workspace and its persistent data — ``msks
        rm`` for one row."""
        return await api_call(
            "DELETE",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
        )

    async def set_egress_mode(
        self, workspace_id: str, mode: str, *, confirm_empty: bool = False
    ) -> dict:
        """PUT the egress policy (#280) — the workspace page's
        mode switch (#344), the same exchange ``msks egress mode``
        makes. ``confirm_empty`` rides only when set — the daemon's
        refusal names it. Returns the reply: the fresh rules frame
        with ``applied`` beside it."""
        body: dict = {"mode": mode}
        if confirm_empty:
            body["confirm_empty"] = True
        return await api_call(
            "PUT",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/egress/policy",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
            json_body=body,
        )

    async def decide(
        self,
        workspace_id: str,
        request_id: str,
        decision: str,
        duration: str,
    ) -> dict:
        """POST one verdict on a held request — the consent
        overlay's decide, the same exchange ``msks egress decide``
        makes."""
        return await api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
            json_body={"decision": decision, "duration": duration},
        )

    async def revoke(self, workspace_id: str, request_id: str) -> dict:
        """DELETE one in-effect verdict — the rules screen's
        revoke, the same exchange ``msks egress revoke`` makes."""
        return await api_call(
            "DELETE",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
        )
