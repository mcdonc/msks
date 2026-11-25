"""The GUI's screen actions over the REST transport (phase 2 of
#924): the daemon's surface through the shared data layer
(:mod:`msks.client.data`) — the GUI holds no direct
daemon-state access.

Every screen action is a :class:`~msks.client.data.RestData`
method — the same exchange the matching CLI subcommand makes,
through the one door, its failures carried as
:class:`~msks.client.data.RestFailure` (#904). The TUI's
data (:mod:`msks.client.tui.data`) builds on the same one —
the two clients share the door, and the per-client surface
grows here and there as their screens diverge: the images
tab's gathered read (#964) composes the shared door's own
calls with the client-side manifest fetch here, so the tab's
one refresh stays one flight. The unlock (#1147) runs as two
flights over the same door: the fetch with the key file's state,
then the load, decrypt, and upload under the passphrase the
window asked for — the client's shared flow
(:mod:`msks.client.unlock`) between them. The screens plug fakes
here for the tests, and the bridge
(:mod:`msks.client.gui.bridge`) runs these coroutines off the GUI
thread.
"""

from ..data import RestData
from ..images import fetch_manifest
from ..unlock import (
    key_file_state,
    open_key_file,
    prepare_unlock,
    settle_unlock,
)
from .imagerows import daemon_machine, served_rows


class GuiData(RestData):
    """The screens' daemon calls; one instance per window, shared
    with the bridge."""

    async def images_page(self, url: str) -> dict:
        """The images tab's one gathered read (#964): the
        daemon's catalog and the published channel's manifest,
        the manifest already narrowed to the machine the
        daemon's /health names — the arch guard the presenter
        owns, run on the worker so the tab's answer carries
        whole rows. The manifest's fetch is a plain public GET
        (#565), the daemon uninvolved until a pick imports one
        of its rows; the manifest's fetch refusals arrive carried
        like every other call's, and a broken channel's row
        refuses on the worker — the bridge carries that refusal
        to the tab the same visible way."""

        async def build() -> dict:
            catalog = await self.images()
            machine = daemon_machine(await self.health())
            published = served_rows(
                await fetch_manifest(url, self.transport), machine
            )
            return {"catalog": catalog, "published": published}

        return await self.carried(build)

    async def secrets_page(self) -> dict:
        """The secrets tab's one gathered read (#966): the
        placeholder listing with its lock states beside the
        recorded audit rows — the two halves the tab's one
        refresh fills, one flight, both daemon reads."""

        async def build() -> dict:
            return {
                "secrets": await self.secrets(),
                "audit": await self.secret_audit(),
            }

        return await self.carried(build)

    async def unlock_prepare(self, explicit: bool) -> dict:
        """The unlock's first flight (#1147): the daemon's locked
        rows and the configured key file's state for the window's
        prompt decision — the rows, the file's path, and whether
        it asks for a passphrase. A flow that ends here answers
        the rows as None: under ``line`` the words that end it
        (the by-hand ask's name for a warm store or a missing
        identity, an unreadable file's refusal), or None for the
        automatic flow's silence. The same fetch the TUI's flow
        opens with: the fetch precedes the load (#515 review), so
        a warm store reads no key file at all."""

        async def build() -> dict:
            rows, path, line = await prepare_unlock(self, explicit)
            encrypted = False
            if rows is not None:
                encrypted, refused = key_file_state(path)
                if refused is not None:
                    rows, line = None, refused
            return {
                "line": line,
                "rows": rows,
                "path": path,
                "encrypted": encrypted,
            }

        return await self.carried(build)

    async def unlock_finish(self, rows: list[dict], path, answer: str) -> str:
        """The unlock's second flight (#1147): the key file loaded
        under the passphrase answer, the rows the key opens
        uploaded, and the outcome line — the refusal when the
        load refuses (a wrong passphrase, a file that is no key),
        the rows-left-locked line when the key opens nothing, the
        outline otherwise."""

        async def build() -> str:
            identity, refused = open_key_file(path, answer)
            if refused is not None:
                return refused
            return await settle_unlock(self, rows, identity)

        return await self.carried(build)
