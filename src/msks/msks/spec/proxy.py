"""The bridge wire vocabulary (#1102): the framing and control
kinds of the websocket that carries a forward proxy's connections
from the daemon to the client that holds the bridge.

A forward proxy row reaches the guest's own ``127.0.0.1`` through
the bridge: the client holding the workspace identity serves the
row's connections, carrying each one over an ssh ``-W`` channel —
the same transport ``msks ssh`` rides. The daemon relays the
bytes: every accepted connection on the row's port becomes one
connection id on the holder's websocket, control frames carrying
the comings and goings, binary frames carrying the bytes tagged
with the id.

Both sides speak this vocabulary: the daemon's bridge hub
(``msks.bridge``, the route in ``server.api.bridge``) and the
client that holds it (``msks.client.bridge``). It lives in the
shared leaf so neither side owns the other's wire.
"""

import struct

#: The relay's control message kinds (text frames; JSON objects
#: with a ``t`` field). The daemon sends ``open`` when a
#: connection arrives on a row's port (carrying the row's guest
#: port) and ``close`` when that connection ends; the client sends
#: ``close`` for the same from its side.
OPEN = "open"
CLOSE = "close"

#: One binary frame: a four-byte big-endian connection id, then
#: the connection's bytes. The id ties the bytes to one relayed
#: connection; control frames carry the same id as JSON.
CID_BYTES = 4


def pack_frame(cid: int, payload: bytes) -> bytes:
    """One binary relay frame: the connection id, then its bytes."""
    return struct.pack(">I", cid) + payload


def unpack_frame(data: bytes) -> tuple[int, bytes]:
    """(connection id, payload) of one binary relay frame."""
    if len(data) < CID_BYTES:
        raise ValueError("relay frame shorter than its id")
    (cid,) = struct.unpack_from(">I", data)
    return cid, data[CID_BYTES:]
