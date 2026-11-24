"""The command groups' connection context (#418): the configured
daemon behind one seam.

The CLI's command bodies live in topical groups
(:mod:`msks.client.workspaces`, :mod:`msks.client.images`,
:mod:`msks.client.secrets`, :mod:`msks.client.volumes`); this
module is the surface they share — the ``MSKSC_*`` readers
(:mod:`msks.client.env`) beside the one-shot call shapes over
:mod:`msks.client.rest` (one exchange on a fresh client; the
shared-client exchanges — resolve-a-reference-then-mutate, the
volume streams — keep their explicit url/token seams on
:mod:`msks.client.rest`), bound together so a group imports one
module instead of pointing its own import at every shared leaf.
The fan-in ceiling is the hard edge: the groups importing
:mod:`msks.client.env` or :mod:`msks.client.rest` directly would
push each past the recorded ceiling, while the one edge through
here keeps them where they stand — the workspace module's #1084
split into a package took the same seam for its two halves. The
module is declared a facade in ``import-graph.toml`` for the same
reason — the env and rest names it binds are the groups' to
import through it.
"""

from .env import (
    env_token,
    env_url,
    is_unix,  # noqa: F401 — the groups' transport-shape read
    shared_ssl,  # noqa: F401 — the clients' TLS context
    ssl_context,  # noqa: F401 — the groups' TLS builder
)
from .rest import (  # noqa: F401 — the workspace verbs' reads
    api_call,
    fetch_health,  # noqa: F401 — the skew probe's read
    fetch_ssh_key,
    scope_params,
)


def call(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    transport=None,
    ssl_ctx=None,
    params: dict | None = None,
):
    """One exchange against the configured daemon, on a fresh
    client (the shared-client exchanges keep their explicit
    url/token seams on :mod:`msks.client.rest`). *params* is the
    query string's pairs — the owner scope a root call carries
    (#1084)."""
    return api_call(
        method,
        env_url(),
        env_token(),
        path,
        json_body=json_body,
        transport=transport,
        ssl_ctx=ssl_ctx,
        params=params,
    )
