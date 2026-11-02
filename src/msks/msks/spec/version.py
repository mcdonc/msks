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


def ordering(word: str) -> tuple:
    """The numeric parts of a version word for before/after
    comparisons (#715): ``0.6.0.dev0`` compares as ``(0, 6, 0)``
    — a dev marker names an in-progress release, and the pair
    orders by the release it is becoming; non-numeric fragments
    drop, and an empty word compares below every release (the
    same rule the Lima floor check applies to its words)."""
    base = word.lstrip("v").split(".dev")[0]
    return tuple(int(part) for part in base.split(".") if part.isdigit())
