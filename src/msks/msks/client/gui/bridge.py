"""The GUI's asyncio worker thread (phase 2 of #924).

One event loop on a worker thread, started with the window and
stopped on close: the screens' REST calls run on it, and results
return to the GUI thread over Qt signals. Phase 1 ships the
module shell only — nothing runs here until phase 2 fills it in.
"""
