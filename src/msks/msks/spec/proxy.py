"""The bridge relay's wire vocabulary (#1102): the framing and
close codes of the websocket that carries the ssh-agent
protocol between the daemon's bridge and the client that serves
it.

A forward proxy row reaches the guest's own ``127.0.0.1``
through an ssh session the daemon holds — the bridge. The
session authenticates with the operator's key: the client
serves signature requests from its agent over the bridge relay,
a websocket that carries the ssh-agent protocol framed by
connection id, and the private half never leaves the client.

Both sides of the relay speak this vocabulary: the daemon's
relay socket (``msks.bridge``, the route in
``server.api.bridge``) and the client that serves it
(``msks.client.bridge``). It lives in the shared leaf so neither
side owns the other's wire.
"""

import struct

#: The relay's control message kinds (text frames; JSON objects
#: with a ``t`` field). The daemon sends ``open`` and ``close``
#: as agent connections come and go at its relay socket, and
#: ``established`` once the bridge's ssh session has
#: authenticated. The client sends ``close`` when its side of one
#: agent connection ends.
OPEN = "open"
CLOSE = "close"
ESTABLISHED = "established"

#: The relay's close codes (the websocket the client dials): the
#: named refusals beyond the auth and lookup codes the forward
#: websocket already carries (4401, 4404). A bridge establish
#: that failed (ssh's own stderr names why), and a second relay
#: for a workspace whose bridge is already coming up.
ESTABLISH_FAILED = 4502
RELAY_BUSY = 4503

#: One relay binary frame: a four-byte big-endian connection id,
#: then the agent-protocol bytes. The id ties the bytes to one
# agent connection; control frames carry the same id as JSON.
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
