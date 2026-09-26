"""The websocket handshake's authentication (#216).

Every msks websocket carries its bearer token in the handshake's
``Authorization: Bearer`` header — the same scheme the REST surface
uses. A URL query string would land the token in access logs, proxy
logs, browser history, and process listings; an ordinary header
does not, and stock proxies forward it with the upgrade. There is
no echo to verify: the daemon accepts a valid token and closes
4401 for anything else, and each client names that close at its
first receive.

Two middlebox shapes read the same from every client as a token
problem, and are accepted here: a proxy that strips the
``Authorization`` header from the upgrade produces the daemon's
4401 (named "authentication failed"), and a middlebox that answers
the handshake itself leaves a silent connection — the link and
egress watch reconnect after the ping timeout, the console and
forward surfaces end the connection there. Both fail closed; a
browser client that cannot set headers at all takes the
short-lived ticket pattern, not a token in a URL.
"""

import re

#: The close code for a token the daemon does not hold — the shared
#: contract of every msks websocket surface.
CLOSE_AUTH_FAILED = 4401

#: The one-line label for that close — the message every surface's
#: close-code table gives it, owned here so the copies cannot drift.
AUTH_FAILED_MESSAGE = "authentication failed (bad token?)"

#: The HTTP ``token`` grammar (RFC 9110): the charset the
#: ``Authorization`` value's credential half must fit. The daemon
#: mints inside it (#116); a client-held token outside it (a padded
#: seed, a stray space in the environment) cannot ride the header
#: at all.
TCHAR_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")

#: The refusal for a token the handshake cannot carry. The message
#: names the credential's problem without echoing the credential:
#: the websocket library's own validation error embeds the token
#: whole, and a traceback would print it.
UNUSABLE_MESSAGE = (
    "the token cannot ride the websocket handshake (characters "
    "outside the HTTP token grammar) — check MSKSC_TOKEN or the "
    "client token file"
)


class UnusableToken(Exception):
    """A token outside the credential grammar: it cannot ride the
    ``Authorization`` header cleanly, so no retry can help."""


def auth_headers(token: str) -> list[tuple[str, str]]:
    """The handshake headers that carry the bearer token.

    Refused here — message intact, token unechoed — when the token
    cannot fit the credential grammar: a control character makes
    the websocket library reject the connect call with the token
    embedded in its error, and a separator would ride the header
    only to fail as an invalid token at the daemon. The gate names
    the problem before either.
    """
    if not TCHAR_RE.fullmatch(token):
        raise UnusableToken(UNUSABLE_MESSAGE)
    return [("Authorization", f"Bearer {token}")]
