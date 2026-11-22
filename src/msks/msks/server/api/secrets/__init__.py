"""The placeholder-secret surface (#198, #339, #305, #1083),
split by responsibility (the api/secrets lines-gate split):

- :mod:`.routes` — the REST routes and their closures.
- :mod:`.blobs` — the per-recipient ciphertext half (#513, #514):
  the mint's blob write and the unlock's validate-cache-re-encrypt
  sequence.
- :mod:`.probe` — the seeded probe placeholder (#424).
- :mod:`.views` — the API-facing row shapes and the owner mask
  (#1083).

The package root re-exports the two names the api composer and
the boot lifespan reach it by: ``router`` and
``seed_probe_placeholder``.
"""

from .probe import seed_probe_placeholder
from .routes import router

__all__ = ["router", "seed_probe_placeholder"]
