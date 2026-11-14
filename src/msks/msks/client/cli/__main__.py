"""The package's own entry: ``python -m msks.client.cli``.

The console-script entry point (``msks = msks.client.cli:main``)
resolves to the package's ``main`` re-export; this module serves
the module invocation the same way the single module did (#874
split the CLI into a package).
"""

from .invoke import main

if __name__ == "__main__":
    raise SystemExit(main())
