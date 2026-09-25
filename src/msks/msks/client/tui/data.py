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
from .consent_app import shared_ssl

#: The create form's identity mode (#309): the client mint, the
#: same default ``msks create`` ships (#121) — the keypair is minted
#: on this client, the public half travels, the private half is
#: written mode 0600 under the client data root.
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

    async def remint_llm_token(self, workspace_id: str) -> str:
        """POST the token remint (#259) — the workspace page's
        remint action; returns the fresh token."""
        reply = await api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/llm-token",
            transport=self.transport,
            ssl_ctx=shared_ssl(),
        )
        return reply["token"]
