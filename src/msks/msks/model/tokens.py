"""Token ORM: hashed bearer credentials with revocation (#8)."""

import re
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

#: The HTTP ``token`` grammar (RFC 9110): the charset a bearer
#: plaintext must fit. Bearer tokens ride the websocket handshake's
#: Sec-WebSocket-Protocol value (#116), whose grammar rejects spaces
#: and separators — and ``secrets.token_urlsafe`` output already
#: fits it, so this is a guard against a future minting scheme that
#: emits something else.
TCHAR_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")


def validate_token_plaintext(token: str) -> str:
    """The mint-time charset guard: pass a plaintext through, or
    refuse it (#116).

    A token outside the HTTP ``token`` grammar would authenticate
    over REST and fail every websocket handshake — the client
    library rejects the malformed Sec-WebSocket-Protocol offer
    before it reaches the daemon. Minting and seeding refuse such
    a plaintext outright, so every token the daemon holds works on
    every surface.
    """
    if not TCHAR_RE.fullmatch(token):
        raise ValueError(
            "bearer tokens must fit the HTTP token grammar "
            "(letters, digits, and !#$%&'*+-.^_`|~); "
            f"got {token!r}"
        )
    return token


class Token(Base):
    """One API bearer token. The plaintext exists only at creation."""

    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
