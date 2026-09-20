"""The application container and its state objects (#1).

``build_app`` constructs the app once; every subsystem is created as
``app.state.X = X(app)`` and caches **only** ``self.app`` — the
klangk ownership rule. Settings are read live at call time
(``self.app.state.settings``), so replacing ``app.state.settings``
propagates without per-subsystem reconfiguration.
"""

from .consent.coordinator import ConsentEngine
from .consent.deciders import DeciderRegistry
from .microvm import Microvm
from .model import Model
from .net import NetManager
from .secretstore import SecretStore
from .server.events import EventHub
from .settings import Settings


class AppState:
    """Owned subsystems, all taking ``app`` and caching only ``app``."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.microvm: Microvm | None = None
        self.model: Model | None = None
        self.net: NetManager | None = None
        # The consent engine + decider registry (#69): built with the
        # app so every subsystem (net, api, watcher) can reach them;
        # the hub is shared with the api's events websocket.
        self.consent: ConsentEngine | None = None
        self.deciders: DeciderRegistry | None = None
        self.hub: EventHub | None = None
        self.secrets: SecretStore | None = None


class App:
    """The msksd application: a state holder swappable at runtime."""

    def __init__(self, settings: Settings) -> None:
        self.state = AppState(settings)
        self.state.microvm = Microvm(self)
        self.state.model = Model(self)
        self.state.net = NetManager(self)
        self.state.consent = ConsentEngine(self)
        self.state.deciders = DeciderRegistry()
        self.state.hub = EventHub()
        self.state.secrets = SecretStore(self)


def build_app(settings: Settings | None = None) -> App:
    """Construct the app from explicit settings or the environment."""
    return App(settings if settings is not None else Settings.from_env())
