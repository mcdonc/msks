"""The application container and its state objects (#1).

``build_app`` constructs the app once; every subsystem is created as
``app.state.X = X(app)`` and caches **only** ``self.app`` — the
klangk ownership rule. Settings are read live at call time
(``self.app.state.settings``), so replacing ``app.state.settings``
propagates without per-subsystem reconfiguration.
"""

from .microvm import Microvm
from .model import Model
from .net import NetManager
from .settings import Settings


class AppState:
    """Owned subsystems, all taking ``app`` and caching only ``app``."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.microvm: Microvm | None = None
        self.model: Model | None = None
        self.net: NetManager | None = None


class App:
    """The msksd application: a state holder swappable at runtime."""

    def __init__(self, settings: Settings) -> None:
        self.state = AppState(settings)
        self.state.microvm = Microvm(self)
        self.state.model = Model(self)
        self.state.net = NetManager(self)


def build_app(settings: Settings | None = None) -> App:
    """Construct the app from explicit settings or the environment."""
    return App(settings if settings is not None else Settings.from_env())
