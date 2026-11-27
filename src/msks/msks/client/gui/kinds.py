"""The three kinds a state word wears (#1160): a state that is as
it should be, one in flight or wanting a look, and one that
failed or cannot be placed. Plain words, no Qt in them, so every
Qt-free presenter names its cells' kinds;
:mod:`msks.client.gui.tints` holds the colors and the cell's
painter, and :mod:`msks.client.gui.tintedlabel` the label's.
The word stays in the cell; the color never carries the meaning
alone.
"""

OK = "ok"
WARN = "warn"
ERROR = "error"
