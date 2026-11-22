"""Consent-mode switching.

The static-ask path a mode switch takes, with its confirm — the
question and the effective-allows read live in the shared
controller module (:mod:`msks.client.tui.consent`), one text and
one rule behind every client that asks them (#1042's GUI Egress
tab included).
"""

from __future__ import annotations

from ..consent import effective_allows


async def switch_mode_path(mode, rules, confirm, send) -> None:
    """The mode switch's shared path (#280, #344) — one logic for
    the decider app and the workspace page, each host supplying
    its own asker and sender: a cancel decides nothing, ``static``
    with nothing effectively allowed asks first (the host pushes
    its own confirmation), and every other pick goes straight
    through ``send``."""
    if mode is None:
        return
    if mode == "static" and not effective_allows(rules):
        confirm(lambda answer: confirmed_static_switch(answer, send))
        return
    await send(mode)


async def confirmed_static_switch(answer: bool, send) -> None:
    """The empty-static confirmation's answer (#280): a yes sends
    the confirmed static switch through ``send``, a no decides
    nothing."""
    if answer:
        await send("static", confirm_empty=True)
