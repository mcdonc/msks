"""The image-reference grammar shared by the store and the client
(#397): hash shape and numeric version ordering, one definition so
a reference the client parses resolves the same way the daemon's
catalog resolves it.
"""


def version_key(version: str) -> tuple:
    """Numeric version ordering: 13.10 sorts after 13.9."""
    parts = []
    for piece in version.replace("-", ".").split("."):
        # Tagged so segments never compare int-to-str (13.9 vs 13.rc).
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def is_hash_shape(ref: str) -> bool:
    """64 lowercase hex characters."""
    return len(ref) == 64 and all(c in "0123456789abcdef" for c in ref)
