"""The daemon's shared leaf vocabulary (#387).

Every module here is stdlib-only and owns a piece of the language
the daemon's layers speak to each other:

- ``vm`` — the backend-neutral workspace spec and status types
- ``egress`` — the allowlist grammar plus the consent lifecycle
  vocabulary (decisions, durations, the decider-facing row shape)
- ``tokens`` — the bearer-token grammar every minting and parsing
  site validates against
- ``time`` — deadline predicates over naive-UTC stored deadlines

Layers above import from here; modules here import nothing from the
daemon. ``src/msks/tests/test_layering.py`` holds the layering to
that rule and the edge whitelist that documents it.
"""
