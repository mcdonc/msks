"""The workspace-addressing vocabulary two owners make (#1084).

Per-owner names (#1082) let one bare name name two workspaces. The
surfaces that refuse to pick answer with the vocabulary here: the
REST resolve answers a 409 whose detail is the structured payload
carrying the kind word, the console and forward dials close with
the code, and the owners render through the one phrase — the
daemon writes both ends and the client reads them, so the words
live in the leaf they share.
"""

import pwd

#: The REST ambiguity error's kind (#1084): the ``error`` word in
#: the 409's structured detail — a name more than one owner holds
#: answers its own error shape, not the not-found path, and the
#: client branches on the word rather than matching message text.
AMBIGUOUS_WORKSPACE = "ambiguous_workspace"

#: The websocket close code for the same refusal (#1084): the
#: console and forward dials close with it when the handshake's
#: ref is a name two or more owners hold, the reason naming the
#: owners and the by-id remedy. Its own code, apart from 4400
#: (a bad port or parameters), so the clients label the ambiguity
#: without reading the reason's text.
AMBIGUOUS_CLOSE = 4405


def dead_owner_cause(uid: int, getpwuid=pwd.getpwuid) -> str | None:
    """The named skip cause a uid no account holds (#1088): the
    mounts derivation and the spawn seam share the spelling, so
    the row's standing marker and a start's refusal read the
    same word. None when the account exists — no cause, no
    skip."""
    try:
        getpwuid(uid)
    except KeyError:
        return f"owner uid {uid} no longer exists"
    return None


def owners_word(frames: list) -> str:
    """The owner frames as one phrase: the account name with its
    uid beside it — ``alice (4242)`` — and the bare uid for a dead
    owner. The daemon's close reason and the client's error line
    share the spelling, so an operator reads the same owners
    whichever surface named them."""
    words = []
    for frame in frames:
        if frame.get("name"):
            words.append(f"{frame['name']} ({frame['uid']})")
        else:
            words.append(str(frame.get("uid")))
    return ", ".join(words)
