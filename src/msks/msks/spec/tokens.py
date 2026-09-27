"""The bearer-token grammar (#116) — the one charset every token
plaintext must fit, validated at every minting and parsing site.

Stdlib-only leaf (#387): settings validates at parse time, the
model validates at mint time, and both import the same guard
instead of model and settings importing each other.
"""

import re

#: The HTTP ``token`` grammar (RFC 9110): the charset a bearer
#: plaintext must fit. Bearer tokens ride the ``Authorization``
#: header on the REST surface and the websocket handshake alike
#: (#216), whose value grammar rejects spaces and separators — and
#: ``secrets.token_urlsafe`` output already fits it, so this is a
#: guard against a future minting scheme that emits something
#: else.
TCHAR_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")


def validate_token_plaintext(token: str) -> str:
    """The mint-time charset guard: pass a plaintext through, or
    refuse it (#116).

    A token outside the HTTP ``token`` grammar could not ride the
    websocket handshake's ``Authorization`` header — the client
    library rejects the malformed header value before it reaches
    the daemon. Minting and seeding refuse such a plaintext
    outright, so every token the daemon holds works on every
    surface.
    """
    if not TCHAR_RE.fullmatch(token):
        raise ValueError(
            "bearer tokens must fit the HTTP token grammar "
            "(letters, digits, and !#$%&'*+-.^_`|~); "
            f"got {token!r}"
        )
    return token
