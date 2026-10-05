"""The image-reference grammar shared by the store and the client
(#397): hash shape and the bare-name newest ordering, one
definition so a reference the client parses resolves the same way
the daemon's catalog resolves it.
"""


def version_numbers(version: str) -> tuple[int, ...]:
    """The version's numeric pieces: ``13.6-beta`` → ``(13, 6)``.

    The non-numeric segments a build stamps beside the numbers — a
    NixOS version's trailing store hash, a prerelease tag — carry
    no ordering, so they drop here: two builds of one numeric
    version tie until import recency speaks (#448).
    """
    return tuple(
        int(piece)
        for piece in version.replace("-", ".").split(".")
        if piece.isdigit()
    )


def newest_rank(version: str, imported: str | None, digest: str) -> tuple:
    """The bare-name newest ordering: the version's numeric pieces
    first, then import recency, then the digest for determinism.

    Recency, never the version's non-numeric suffixes, decides
    between two builds of one numeric version (#448): the suffixes
    carry no ordering, and the row the catalog just took in is the
    one a bare-name create must boot.
    """
    return (version_numbers(version), imported or "", digest)


def is_hash_shape(ref: str) -> bool:
    """64 lowercase hex characters."""
    return len(ref) == 64 and all(c in "0123456789abcdef" for c in ref)
