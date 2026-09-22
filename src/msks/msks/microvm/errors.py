"""Typed errors for the microvm seam (klangk PodmanError analogue)."""


class MicrovmError(Exception):
    """A driver-level failure.

    ``status`` carries the transport status when one exists (a CH
    API HTTP status), ``None`` otherwise.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MicrovmTimeoutError(MicrovmError):
    """A lifecycle step exceeded its deadline (fail-closed analogue)."""
