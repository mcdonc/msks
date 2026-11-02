"""The daemon's version string (#407).

The entry point's ``--version`` and the API's root endpoint read
it from the vocabulary leaf, so the HTTP surface learns its
version without importing the package root; every layer may
import the leaf.

A ``.devN`` suffix marks the tree as between releases (#704):
the number before it is the NEXT release's, and the macOS setup
tool's PyPI install path refuses on the marker — a dev build has
no release to install, and falling back to the same-numbered
released wheel silently installs a daemon that predates the
config the tool writes.
"""

__version__ = "0.6.0.dev0"
