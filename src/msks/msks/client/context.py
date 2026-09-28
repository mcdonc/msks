"""The command groups' connection context (#418): the configured
daemon behind one seam.

The CLI's command bodies live in topical groups
(:mod:`msks.client.workspaces`, :mod:`msks.client.images`,
:mod:`msks.client.secrets`, :mod:`msks.client.volumes`); this
module is the surface they share — the ``MSKSC_*`` readers
(:mod:`msks.client.env`) beside the one-shot call shape over
:mod:`msks.client.rest` (one exchange on a fresh client; the
shared-client exchanges — resolve-a-reference-then-mutate, the
volume streams — keep their explicit url/token seams on
:mod:`msks.client.rest`), bound together so a group imports one
module instead of pointing its own import at every shared leaf.
The fan-in ceiling is the hard edge: four groups importing
:mod:`msks.client.env` directly would push it past the recorded
ceiling, while the one edge through here keeps it where it
stands. The module is declared a facade in ``import-graph.toml``
for the same reason — the env names it binds are the groups' to
import through it.
"""

from .env import (
    env_token,
    env_url,
    ssl_context,  # noqa: F401 — the groups' TLS seam
)
from .rest import api_call


def call(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    transport=None,
    ssl_ctx=None,
):
    """One exchange against the configured daemon, on a fresh
    client (the shared-client exchanges keep their explicit
    url/token seams on :mod:`msks.client.rest`)."""
    return api_call(
        method,
        env_url(),
        env_token(),
        path,
        json_body=json_body,
        transport=transport,
        ssl_ctx=ssl_ctx,
    )
