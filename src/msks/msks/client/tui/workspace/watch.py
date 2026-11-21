"""The workspace page's watches over its decider link.

The per-tick eyes of :mod:`msks.client.tui.workspace.screen`:
everything the page learns from the link's buffers and from the
host's own state — the holds the frames landed, the off-allowlist
sightings, the proxy and row transitions that poke a re-read
(#809, #1019), the shell count off the session records (#858),
and the reconnect poke that serves the frames a dropped
connection missed. A mixin (:class:`PageWatches`) so the screen
keeps one class at runtime — the same shape
:mod:`msks.client.tui.help`'s ``HelpKeys`` gives every screen.
"""

from ...sshsessions import count_sessions
from ..consent_ui import ConsentPage, hold_flash, sighting_flash


class PageWatches:
    """The link-facing half of the workspace page — the methods
    its tick drains and its link's hooks call. Lives on
    :class:`msks.client.tui.workspace.screen.WorkspaceScreen`'s
    bases; every ``self`` below is that screen."""

    def link_reconnected(self) -> None:
        """The decider connection serves again (#1019): the row
        and the compact proxies line may have moved while the
        link was down — lifecycle frames and proxy transitions
        alike never landed — so both re-read now, beside the
        replay the registration lands."""
        if not self.is_mounted:
            return
        self.refresh_row()
        self.refresh_proxies()

    # -- the holds watch (#454) ----------------------------------------

    def watch_holds(self) -> None:
        """The per-tick watch over the link: drain the sighting
        buffer (#201 over #358), then name each first-seen hold on
        the consent line (#454) — the passive attention a hold
        gets, the header's count beside it."""
        if self.link is None:
            return
        self.drain_sightings()
        self.flash_new_holds()

    def watch_proxies(self) -> None:
        """The per-tick watch over the link's proxy transitions
        (#809): each one refetches the compact line's rows — the
        same immediacy the events socket gives the proxies page,
        on the connection the page already holds."""
        if self.link is None:
            return
        if self.link.take_proxy_events():
            self.refresh_proxies()

    def watch_row_events(self) -> None:
        """The per-tick watch over the link's row-affecting
        transitions (#1019): each one refetches the row — the
        page's dimming and header follow a workspace another
        surface (the CLI, another operator) moved while the page
        stood, without the page re-reading the listing on every
        tick."""
        if self.link is None:
            return
        if self.link.take_row_events():
            self.refresh_row()

    def sync_shells(self) -> None:
        """Land the host's open-shell count on the row (#858 over
        #1019): the count is client-side, per-host state the
        daemon never holds, so the local tick repaints the header
        when it moves — the row-affecting refetch the old cadence
        carried is gone."""
        fresh = count_sessions().get(self.row["id"], 0)
        if self.row.get("shells") == fresh:
            return
        self.row["shells"] = fresh
        self.paint_header()

    def flash_new_holds(self) -> None:
        """Flash each hold the queue has not named yet (#454): the
        page pushes nothing on a hold's arrival, so the consent
        line names the destination and the key in. The flash waits
        while another screen owns the terminal (the consent page,
        the edit form, a picker): the ids stay unseen and the
        first tick after the stack unwinds names what arrived —
        the overlay-era burst guard's rule, carried to the flash
        (a flash painted on a hidden line is consumed unseen).
        A burst takes one flash naming its count (a FlashLine
        owns the line for its TTL — one destination a tick would
        overwrite the rest)."""
        if self.app.screen is not self:
            return
        fresh = self.take_unseen_holds()
        if len(fresh) == 1:
            self.flash(hold_flash(fresh[0]))
        elif fresh:
            self.flash(f"{len(fresh)} egress holds to decide — press e")

    def take_unseen_holds(self) -> list:
        """The queue's holds the consent line has not named yet,
        taken: their ids join the seen set here and never leave it
        — a reconnect's replay re-lands the same holds without
        re-flashing them, and the holds that arrived while the
        link was down name themselves once, on the tick after the
        replay lands them."""
        pending = self.link.controller.ordered()
        fresh = [
            request
            for request in pending
            if request.id not in self.seen_hold_ids
        ]
        self.seen_hold_ids.update(request.id for request in pending)
        return fresh

    def drain_sightings(self) -> None:
        """Flash the off-allowlist sightings the frames landed since
        the last drain, each on the surface that owns the terminal
        (#358 carries #201's rule — the exfil signal interrupts
        wherever the operator is)."""
        for event in self.link.take_sightings():
            self.flash_visible(sighting_flash(event))

    def flash_visible(self, message: str) -> None:
        """Give one message to the surface that owns the terminal: the
        consent page's status line while it is up, this page's
        consent line when it is not."""
        page = self.consent_page()
        if page is not None:
            page.flash(message)
        else:
            self.flash(message)

    def consent_page(self) -> ConsentPage | None:
        """The open consent page wherever it sits in the stack (a
        picker may stand above it)."""
        for screen in self.app.screen_stack:
            if isinstance(screen, ConsentPage):
                return screen
        return None
