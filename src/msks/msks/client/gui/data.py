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
one refresh stays one flight. The screens plug fakes here for
the tests, and the bridge (:mod:`msks.client.gui.bridge`)
runs these coroutines off the GUI thread.
"""

from ..data import RestData
from ..images import fetch_manifest
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
