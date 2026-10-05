"""The image-reference grammar shared by the store and the client
(#397): hash shape and the bare-name newest ordering, one
definition so a reference the client parses resolves the same way
the daemon's catalog resolves it.
"""


def version_numbers(version: str) -> tuple[int, ...]:
    """The version's leading numbers: ``13.6-beta`` → ``(13, 6)``,
    ``26.05pre-git-54w4wrbv`` → ``(26, 5)``.

    Each segment contributes its leading digit run (``05pre`` →
    ``5``, so a fused prerelease tag keeps its minor), and the
    first purely-alpha segment ends the walk (``git`` above): the
    trailing segments a build stamps beside the numbers — a NixOS
    version's store hash, a Debian build's tag — carry no
    ordering, so they drop here. Two builds of one numeric version
    tie until import recency speaks (#448).
    """
    numbers = []
    for piece in version.replace("-", ".").split("."):
        if piece.isalpha():
            break
        digits = leading_digits(piece)
        if digits:
            numbers.append(int(digits))
    return tuple(numbers)


def leading_digits(piece: str) -> str:
    """A segment's leading digit run: ``6rc1`` → ``6``, ``pre`` →
    empty."""
    end = 0
    while end < len(piece) and piece[end].isdigit():
        end += 1
    return piece[:end]


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
