"""The workspace failure class the driver boundary and the
egress data plane raise (#401, split to its own leaf by #416).

Stdlib-only leaf: both surfaces fail a workspace boot with one
operator-shaped error, so the class lives below both and neither
imports the other. The VM vocabulary types stay in
:mod:`msks.spec.vm` — a module that raises this error needs
nothing from them, which is what the split fixes.
"""


class MicrovmError(Exception):
    """A workspace-surface failure (klangk PodmanError analogue).

    Raised by the driver boundary (a CH API transport failure, a
    lifecycle refusal) and by the egress data plane (a tap, chain,
    queue, or service that could not arm or operate). ``status``
    carries the transport status when one exists (a CH API HTTP
    status), ``None`` otherwise.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
