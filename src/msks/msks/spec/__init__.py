"""The daemon's shared leaf vocabulary (#387).

Every module here is stdlib-only and owns a piece of the language
the daemon's layers speak to each other:

- ``vm`` — the backend-neutral workspace spec and status types,
  plus the failure class the driver boundary and the data plane
  raise (#401)
- ``egress`` — the allowlist grammar plus the consent lifecycle
  vocabulary (decisions, durations, the decider-facing row shape)
  and the decided side #401 added: verdict TTL resolution and the
  pin shape consent hands the data plane
- ``images`` — the image-reference grammar (hash shape, version
  ordering) the catalog and the client share (#397)
- ``tokens`` — the bearer-token grammar every minting and parsing
  site validates against
- ``time`` — deadline predicates over naive-UTC stored deadlines
- ``version`` — the daemon's version string, its single home
  since #407 moved it off the package root

Layers above import from here; modules here import nothing from the
daemon. ``src/msks/tests/test_layering.py`` holds the layering to
that rule and the edge whitelist that documents it.
"""
