"""The daemon's version string (#407).

The entry point's ``--version`` and the API's root endpoint read
it from the vocabulary leaf, so the HTTP surface learns its
version without importing the package root; every layer may
import the leaf.
"""

__version__ = "0.1.0"
