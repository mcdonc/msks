"""The GUI's screen actions over the REST transport (phase 2 of #924).

One method per screen action, mirroring the TUI's data seam
(:mod:`msks.client.tui.data`): the same ``RestFailure`` contract
and the same calls through :mod:`msks.client.rest`. Phase 1
ships the module shell only — the bodies arrive with the
screens (phases 3 and 4 of the plan).
"""
