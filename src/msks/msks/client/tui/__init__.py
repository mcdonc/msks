"""Client-side TUI screens (textual; the daemon never imports these)."""

import os

# (#437) The tree leaves the terminal's own shortcuts to it.
# Textual enables the kitty keyboard protocol by default, which
# asks the terminal to deliver every key — Ctrl+Shift+C included
# — to the app instead of letting the terminal's own copy
# shortcut answer the gesture. This package is the client's first
# textual-importing module, so the flag lands before
# textual.constants reads it; setdefault keeps an operator's
# explicit choice (a 0 opts the protocol back in) authoritative.
os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")
