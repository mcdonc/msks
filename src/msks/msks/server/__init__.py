"""The msksd API server: FastAPI app, auth, events, TLS, entry point.

Nothing UI-shaped links in here — this package *is* the server side of
the client/server split. A future ``msks.client`` package may not
import anything from ``msks.server`` or ``msks.model`` (enforced by
the import-boundary test).
"""
