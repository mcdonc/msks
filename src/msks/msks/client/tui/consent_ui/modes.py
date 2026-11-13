"""Consent-mode switching.

The static-ask path a mode switch takes, with its confirm and
the effective-allows read the switch answers on.
"""

from __future__ import annotations

from ..consent import EgressRules


def effective_allows(rules: EgressRules | None) -> bool:
    """Whether anything effectively allows egress under the
    snapshot (#280): a non-empty allowlist or an in-effect allowed
    verdict — the condition the static switch's confirmation
    gates on."""
    if rules is None:
        return False
    return bool(rules.allow_list) or bool(rules.allowed)


#: The empty-static confirmation's question (#280) — one text for
#: every surface that asks it: the decider app's picker, and the
#: workspace page's (#344). It names the posture (every name
#: answers NXDOMAIN — an offline workspace) and the escape
#: (switching anyway).
EMPTY_STATIC_QUESTION = (
    "static with nothing allowed answers every name NXDOMAIN "
    "— an offline workspace. Switch anyway?"
)


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
