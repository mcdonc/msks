"""Typed errors for the microvm seam (klangk PodmanError analogue).

``MicrovmError`` lives in :mod:`msks.spec.failures` (moved to its
own spec leaf by #416): the egress data plane raises it too, and
neither surface may import the other — the vocabulary package owns
the class, and this module re-exports it so the driver's own
imports keep one failure module.
"""

from ..spec.failures import MicrovmError as MicrovmError


class MicrovmTimeoutError(MicrovmError):
    """A lifecycle step exceeded its deadline (fail-closed analogue)."""
