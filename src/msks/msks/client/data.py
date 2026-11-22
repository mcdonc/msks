"""The client's shared REST surface (#924 phase 2): the door
every screen action of both interactive clients — the TUI and
the GUI — walks through.

The surface owns the failure contract (#904): the CLI's exit shape
becomes :class:`RestFailure`, catchable and carriable inside a
UI that must stand and retry, with ``reach`` naming whether the
daemon answered at all. The surface owns the shared TLS context too
(one build per process, the TOFU warning once), and the two
exchanges both clients make verbatim — the health probe and the
create. The per-screen one-liner methods live in the clients'
own data modules (:mod:`msks.client.tui.data`,
:mod:`msks.client.gui.data`), each the same exchange the
matching CLI subcommand makes.
"""

from .create import create_workspace_core, invoking_user
from .env import env_token, env_url, shared_ssl
from .jsonio import ApiExit
from .rest import api_call, fetch_health


class RestFailure(Exception):
    """One REST call's failure as a UI holds it (#904): the transport
    raises :class:`SystemExit` — the CLI's contract, one readable
    line and then the process ends — and inside a UI that same
    failure must stay catchable: a task that raises
    ``SystemExit`` takes the event loop down whatever the
    awaiting screen catches, and a GUI carries the failure across
    threads instead. The door carries the same message as a
    plain exception: the screens' guards flash it, and the tree
    or window stands to retry on its next tick. ``reach`` names
    the failure's kind: True when the daemon answered nothing at
    all (an unreachable daemon, a timeout), False when it
    answered with a refusal — a listing the daemon cannot serve
    is an answer, not an outage, and the listing's down marker
    (#904) states it only for the outage.
    """

    def __init__(self, message: str, *, reach: bool) -> None:
        super().__init__(message)
        self.reach = reach


class RestData:
    """The screens' daemon door: every screen action of the TUI
    (:class:`~msks.client.tui.data.TuiData`) and the GUI
    (:class:`~msks.client.gui.data.GuiData`) runs through
    :meth:`rest`, its failure carried. One instance per app run;
    the screens plug fakes here for the tests."""

    def __init__(self, transport=None) -> None:
        self.transport = transport

    async def carried(self, build):
        """Await one REST call, carrying its exit-contract failure
        as :class:`RestFailure` (#904) — the message unchanged, so
        a screen's flash reads the same line the CLI prints.
        ``build`` makes the call (its argument evaluation
        included) inside the guard, so every failure the
        invocation's own environment can raise arrives carried
        too."""
        try:
            return await build()
        except ApiExit as exc:
            raise RestFailure(str(exc), reach=False) from exc
        except SystemExit as exc:
            raise RestFailure(str(exc), reach=True) from exc

    async def rest(
        self, method: str, path: str, body: dict | None = None
    ) -> dict:
        """One REST exchange through the invocation's own URL,
        token, and shared TLS context — the same call the matching
        CLI subcommand makes, its failure carried (#904)."""
        return await self.carried(
            lambda: api_call(
                method,
                env_url(),
                env_token(),
                path,
                json_body=body,
                transport=self.transport,
                ssl_ctx=shared_ssl(),
            )
        )

    async def health(self) -> dict | None:
        """GET /health best-effort (#717): the daemon host's
        machine name keys the published list's arch filter. An
        answer that will not come — an unreachable daemon, one
        predating the field — is None, and the screen shows every
        row rather than refusing on its filter's behalf
        (:func:`fetch_health` owns the every-failure-is-None
        contract)."""
        return await fetch_health(
            env_url(), env_token(), self.transport, shared_ssl()
        )

    async def create(self, body: dict) -> dict:
        """POST one workspace (the create form's fields) with the
        operator's configured key for its identity (#486) — the
        new row. The environment reads (the default user, the
        identity key) sit inside the carried call, so their
        refusals arrive as RestFailure like every other
        failure."""

        async def build() -> dict:
            body.setdefault("user", invoking_user())
            return await create_workspace_core(
                env_url(),
                env_token(),
                body,
                self.transport,
                ssl_ctx=shared_ssl(),
            )

        return await self.carried(build)

    async def workspaces(self) -> list[dict]:
        """GET the workspaces listing — the main screen's rows."""
        return await self.rest("GET", "/api/v1/workspaces")

    async def storage(self) -> dict:
        """GET the capacity report (#849) — the list's detail
        strip: the per-workspace root-plus-home cost ``msks
        storage`` reports, fetched on the listing's refresh
        cadence and cached between refreshes."""
        return await self.rest("GET", "/api/v1/storage")

    async def images(self) -> list[dict]:
        """GET the image catalog — the create form's image select
        (the same listing ``msksc image ls`` prints)."""
        return await self.rest("GET", "/api/v1/images")

    async def create_defaults(self) -> dict:
        """GET the create defaults — the root/home sizes a create
        lands on when the body leaves them unset (the create
        form's size placeholders hint them)."""
        return await self.rest("GET", "/api/v1/create-defaults")

    async def remove_image(self, digest: str) -> dict:
        """DELETE one image by hash — the image screen's remove
        action (#571), the same exchange the CLI's ``image rm``
        makes."""
        return await self.rest("DELETE", f"/api/v1/images/{digest}")

    async def import_image(self, source: str) -> dict:
        """POST one image import — the import action (#565): the
        source is a published channel URL; the daemon downloads,
        verifies, and floor-checks the archive."""
        return await self.rest("POST", "/api/v1/images", {"source": source})

    async def set_default_image(self, ref: str) -> dict:
        """POST the default designation — the image screen's
        default action, the same exchange the CLI's ``image
        default`` makes: the ref is the row's full hash, and the
        daemon's pointer file is the whole designation."""
        return await self.rest("POST", "/api/v1/images/default", {"ref": ref})

    async def clone(self, workspace_id: str, name: str) -> dict:
        """POST the clone (#773) — the detail screen's clone
        action. The daemon owns the stopped-source rule: a
        workspace that is not stopped answers the named 409,
        which the screen shows."""
        return await self.rest(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/clone",
            {"name": name},
        )

    async def resize(self, workspace_id: str, body: dict) -> dict:
        """POST the resize — the edit dialog's sizes and topology
        (#331), the same exchange ``msks resize`` makes. The
        daemon owns the stopped-workspace rule: a workspace that
        is not stopped answers the named 409, which the dialog
        shows."""
        return await self.rest(
            "POST", f"/api/v1/workspaces/{workspace_id}/resize", body
        )

    async def set_mounts(self, workspace_id: str, mounts: list) -> dict:
        """POST the mounts list whole (#620, part #624) — the same
        exchange ``msks mount`` makes. The daemon validates
        against its gates and answers with the refreshed row,
        whose derived state the editor paints."""
        return await self.rest(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/mounts",
            {"mounts": mounts},
        )

    async def start(self, workspace_id: str) -> dict:
        """POST the boot — the same call ``msks start`` makes."""
        return await self.rest(
            "POST", f"/api/v1/workspaces/{workspace_id}/start"
        )

    async def stop(self, workspace_id: str) -> dict:
        """POST the graceful power-off — ``msks stop``."""
        return await self.rest(
            "POST", f"/api/v1/workspaces/{workspace_id}/stop"
        )

    async def remove(self, workspace_id: str) -> dict:
        """DELETE the workspace and its persistent data — ``msks
        rm`` for one row."""
        return await self.rest("DELETE", f"/api/v1/workspaces/{workspace_id}")

    async def egress_rules(self, workspace_id: str) -> dict:
        """GET the egress rules view — the GUI workspace page's
        Egress tab (#1042): the mode, the static allowlist, and
        the in-effect verdicts, the reply the daemon's
        ``GET …/egress`` route serves."""
        return await self.rest(
            "GET", f"/api/v1/workspaces/{workspace_id}/egress"
        )

    async def set_egress_mode(
        self,
        workspace_id: str,
        mode: str,
        confirm_empty: bool = False,
        allow_list: list[str] | None = None,
    ) -> dict:
        """PUT the egress policy (#280) — the mode switch the
        detail screen's pick (#344), the GUI Egress tab's
        switcher (#1042), and the tab's allowlist editor (#1050)
        drive, the same exchange ``msks egress mode`` makes.
        ``confirm_empty`` is sent only when set — the daemon's
        refusal names it — and stays positional: the GUI
        bridge's one verb passes arguments by position (#1042).
        An ``allow_list`` replaces the row's static allowlist
        with the full new list (the editor's add and remove
        both); None keeps the list the row carries. Returns the
        reply: the fresh rules frame with ``applied`` beside
        it."""
        body: dict = {"mode": mode}
        if confirm_empty:
            # The wire key's spelling is pinned where the wire
            # itself is tested: the data-layer tests assert this
            # body, and the route's refusal — whose detail names
            # the flag — is copied verbatim into the Egress tab's
            # tests (#1042).
            body["confirm_empty"] = True
        if allow_list is not None:
            # The full new list, whole: an omitted allow_list
            # keeps the row's, and the editor's tests pin this
            # body's list the same way (#1050).
            body["allow_list"] = allow_list
        return await self.rest(
            "PUT",
            f"/api/v1/workspaces/{workspace_id}/egress/policy",
            body,
        )

    async def decide(
        self,
        workspace_id: str,
        request_id: str,
        decision: str,
        duration: str,
    ) -> dict:
        """POST one verdict on a held request — the egress consent
        screen's decide, the same exchange ``msks egress decide``
        makes."""
        return await self.rest(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
            {"decision": decision, "duration": duration},
        )

    async def revoke(self, workspace_id: str, request_id: str) -> dict:
        """DELETE one in-effect verdict — the egress consent
        screen's revoke, the same exchange ``msks egress revoke``
        makes."""
        return await self.rest(
            "DELETE",
            f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        )

    async def secrets(self) -> list[dict]:
        """GET the placeholder rows (#390) — the secrets screen's
        rows, the same listing ``msks secret ls`` prints (no
        sentinels)."""
        return await self.rest("GET", "/api/v1/secrets")

    async def secret(self, placeholder_id: int) -> dict:
        """GET one placeholder row with its sentinel (#440) — the
        row's open fetch, the same exchange ``GET
        /api/v1/secrets/<id>`` serves: the listing omits
        sentinels, this reply carries the row's."""
        return await self.rest("GET", f"/api/v1/secrets/{placeholder_id}")

    async def revoke_secret(self, placeholder_id: int) -> dict:
        """DELETE one placeholder row (#390) — the secrets
        screen's revoke, the same exchange ``msks secret revoke``
        makes: the row retires everywhere at once."""
        return await self.rest("DELETE", f"/api/v1/secrets/{placeholder_id}")

    async def renew_secret(self, placeholder_id: int, ttl_s: int) -> dict:
        """POST one placeholder's renewal (#390) — the secrets
        screen's renew, the same exchange ``msks secret renew``
        makes: the lifetime extends in place, the sentinel and
        the row's identity stay as they are."""
        return await self.rest(
            "POST",
            f"/api/v1/secrets/{placeholder_id}/renew",
            {"ttl_s": ttl_s},
        )

    async def mint_secret(self, body: dict) -> dict:
        """POST one placeholder mint (#393) — the secrets screen's
        form, the same exchange ``msks secret mint`` makes: the
        reply carries the row with its sentinel."""
        return await self.rest("POST", "/api/v1/secrets", body)

    async def pending_secrets(self) -> list[dict]:
        """GET the pending ciphertext rows (#515) — the unlock
        flow's fetch, the same exchange ``GET
        /api/v1/secrets/pending`` serves: the rows' blobs and
        lock state, with the session window the unlock POST
        requires opened by the serve."""
        return await self.rest("GET", "/api/v1/secrets/pending")

    async def unlock_secrets(self, values: list[dict]) -> dict:
        """POST the decrypted values back (#515) — the unlock
        flow's upload, the same exchange ``POST
        /api/v1/secrets/unlock`` takes: the values enter the
        daemon's timeout-scoped cache and each row re-encrypts
        fresh blobs to its current recipients."""
        return await self.rest(
            "POST", "/api/v1/secrets/unlock", {"values": values}
        )

    async def secret_audit(self) -> list[dict]:
        """GET the recorded audit rows (#390) — the secrets
        screen's audit view replays them newest first, the same
        listing ``/api/v1/secrets/audit`` serves."""
        return await self.rest("GET", "/api/v1/secrets/audit")

    async def workspace_proxies(self, workspace_id: str) -> list[dict]:
        """GET one workspace's proxy rows with live status (#809)
        — the detail screen's scoped listing, the same exchange
        ``GET /api/v1/workspaces/<id>/proxies`` serves."""
        return await self.rest(
            "GET", f"/api/v1/workspaces/{workspace_id}/proxies"
        )

    async def create_proxy(self, workspace_id: str, body: dict) -> dict:
        """POST one proxy row (#809) — the proxies screen's add
        form, the same exchange ``msks proxy add`` makes: the
        answer carries the row with its live status."""
        return await self.rest(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/proxies",
            body,
        )

    async def update_proxy(
        self, workspace_id: str, proxy_id: int, body: dict
    ) -> dict:
        """PATCH one proxy row (#890) — the proxies screen's edit
        form: the row rewritten whole, the answer carries the row
        with its live status."""
        return await self.rest(
            "PATCH",
            f"/api/v1/workspaces/{workspace_id}/proxies/{proxy_id}",
            body,
        )

    async def remove_proxy(self, workspace_id: str, proxy_id: int) -> dict:
        """DELETE one proxy row (#809) — the proxies screen's
        remove, the same exchange ``msks proxy rm`` makes: the
        listener closes at once."""
        return await self.rest(
            "DELETE",
            f"/api/v1/workspaces/{workspace_id}/proxies/{proxy_id}",
        )
