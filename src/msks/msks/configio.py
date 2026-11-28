"""Config-file reading and writing, shared by msksd and the
client (#397): one duplicate-key grammar, one exclusive first-run
create, and one in-place key edit over an existing file, so the
daemon's config and the client's config carry the same file
discipline. A leaf — yaml and stdlib only; importing it loads no
daemon composition.
"""

import os
import re
import tempfile
from pathlib import Path

import yaml

#: One top-level mapping key's line (#1224): the key at column 0,
#: the first colon ending it, whitespace or the line's end after
#: the colon. A comment, an indented line, and a document marker
#: do not match; the operator's own spelling stands before the
#: colon, hyphens and all.
KEY_LINE = re.compile(r"^([^\s#][^:]*?):(?:\s|$)")


class UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses duplicate mapping keys and merge keys.

    PyYAML keeps the last of duplicate keys silently; the config file
    fails fast instead — an operator appending a second block to a
    long file gets an error naming the key, not a silent override of
    everything above it. Merge keys (``<<: *anchor``) are refused
    with their own message: the file is flat and every key is spelled
    out, so an anchored base has nothing to merge into — and every
    practical merge carries a mapping-valued anchor carrier, which
    is itself not a config key.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            note_key(self, key_node, seen, deep)
        return super().construct_mapping(node, deep)


def note_key(loader, key_node, seen: set, deep: bool) -> None:
    """Validate one mapping key in place: refuse merge keys and
    complex (non-scalar) keys, and duplicate keys."""
    if key_node.tag == "tag:yaml.org,2002:merge":
        raise ValueError(
            "merge keys (<<) are not supported by the msksd "
            "config file; write each key out"
        )
    key = loader.construct_object(key_node, deep=deep)
    try:
        duplicate = key in seen
    except TypeError:
        raise ValueError(
            "config keys must be scalars, not lists or mappings"
        ) from None
    if duplicate:
        raise ValueError(f"duplicate config key {key!r}")
    seen.add(key)


def write_exclusive(path: str, body: str) -> None:
    """Write *body* to *path* as a new file (both config files'
    first-run writer, #46/#314): the parent directory is created
    0700 when missing, the file itself is written 0600 — the
    templates' examples name credentials, so the file joins the
    house pattern of secret-bearing artifacts readable only by
    its owner — and an existing file is refused (the exclusive
    create), never overwritten."""
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)


def write_atomic(path: str, body: str) -> None:
    """Write *body* to *path* over whatever stands there (the
    client's in-place key edit, #1224): the parent directory is
    created 0700 when missing, the text lands in a temporary file
    beside the target — written 0600, the tree's own mode — and is
    renamed over it once complete. A reader never sees a
    half-written file, and a crash mid-write leaves the old file
    whole and the temporary file beside it (``<name>.*.tmp``),
    swept by hand. The target is the path's final resolution: a
    symlink is written *through*, so a file home-manager links
    stays a link and the link's own file changes — and a link
    into the read-only store fails at the temporary file with the
    path in the error, never forking the operator's file."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def set_scalars(text: str, values: dict) -> str:
    """Merge top-level scalar keys into a config file's text, in
    place (#1224): every byte the keys do not own stays as the
    operator wrote it — comments, blank lines, the order of the
    other keys, a ``daemons:`` block.

    Each key in *values* is found by its normalized spelling (a
    hyphen in the file is the underscore in *values*), so an
    operator's ``dictate-model`` line is rewritten and never gets a
    ``dictate_model`` twin. A set value replaces the key's line —
    a trailing comment on that line goes with it — keeping the
    file's spelling, or appends at the end of the file when the key
    is absent; a ``None`` or blank value deletes the key's line. A
    value is a string, rendered by ``yaml.safe_dump`` on one line
    with its non-ASCII kept as written, which quotes what needs
    quoting; anything else is refused. The key's block covers its
    continuation lines — an indented scalar — and stops at the next
    top-level line or at a blank line that precedes one. The text
    is written back with ``\n`` line endings. The caller validates
    the result with its own loader before writing (this leaf reads
    no key vocabulary).
    """
    refuse_non_scalars(values)
    lines = text.split("\n")
    for key, value in values.items():
        lines = set_scalar(lines, key, value)
    return "\n".join(lines)


def refuse_non_scalars(values: dict) -> None:
    """The merge's contract (#1224, #1226): every value a string, a
    whole number, or None, refused by name before any key is edited.
    A number renders bare — a port written as digits a loader reads
    back as an int, where the quoted string form reads as text."""
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(
            value, (str, int, type(None))
        ):
            raise ValueError(
                f"config key {key!r} must be a string, an int, or None "
                f"to merge, got {type(value).__name__}"
            )


def set_scalar(lines: list[str], key: str, value) -> list[str]:
    """One key's edit (#1224): the key's block replaced, deleted,
    or the key appended at the end."""
    index = key_line_index(lines, key)
    if value is None or value == "":
        return without_key(lines, index)
    line = f"{spelling_of(key, lines, index)}: {render_scalar(value)}"
    if index is None:
        return append_block(lines, [line])
    lines[index : block_end(lines, index)] = [line]
    return lines


def without_key(lines: list[str], index: int | None) -> list[str]:
    """The lines with the key's block gone (#1224); the lines as
    they are when the file does not carry the key."""
    if index is not None:
        del lines[index : block_end(lines, index)]
    return lines


def spelling_of(key: str, lines: list[str], index: int | None) -> str:
    """The key's spelling for the new line (#1224): the file's
    own when it carries the key, the given one when it appends."""
    return key if index is None else key_line_key(lines[index])


def render_scalar(value) -> str:
    """One value on one line (#1224): the plain form when the
    dump keeps it on one line, the double-quoted form (its line
    breaks escaped) when the value carries a newline. The value
    is dumped under a fixed key, never at the document root: a
    root plain scalar comes back with a ``...`` marker line."""
    under_key = yaml.safe_dump(
        {"k": value}, default_flow_style=False, width=10**6, allow_unicode=True
    )
    if under_key.count("\n") == 1 and under_key.startswith("k: "):
        return under_key[len("k: ") :].rstrip("\n")
    return yaml.safe_dump(
        value, default_style='"', width=10**6, allow_unicode=True
    ).rstrip("\n")


def key_line_key(line: str) -> str:
    """The key a top-level line spells, as the operator wrote it."""
    return KEY_LINE.match(line).group(1)


def key_line_index(lines: list[str], key: str) -> int | None:
    """The line of the top-level key *key*, either spelling; None
    when the file does not carry it."""
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line)
        if match and match.group(1).replace("-", "_") == key:
            return index
    return None


def block_end(lines: list[str], index: int) -> int:
    """Where the key's block at *index* ends (#1224): the next
    top-level line, or a blank line that precedes one — a blank
    line inside an indented scalar belongs to the block."""
    end = index + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() == "":
            if next_is_indented(lines, end):
                end += 1
                continue
            break
        if line[0] in " \t":
            end += 1
            continue
        break
    return end


def next_is_indented(lines: list[str], index: int) -> bool:
    """Whether the next non-blank line after *index* is indented
    (or there is none, which ends the block as a trailing blank)."""
    for line in lines[index + 1 :]:
        if line.strip() == "":
            continue
        return line[0] in " \t"
    return False


def append_block(lines: list[str], block: list[str]) -> list[str]:
    """The block at the end of the file (#1224), after a trailing
    newline when the last line lacks one, the file's own trailing
    newline kept."""
    if lines and lines[-1] != "":
        lines.append("")
    at = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
    lines[at:at] = block
    return lines


#: The ``daemons:`` block's child indent (#1226) when the file
#: carries the key with no entries under it yet: two spaces, the
#: templates' and the dumps' own.
DAEMON_INDENT = "  "


def set_daemon(text: str, alias: str, entry: dict | None) -> str:
    """Merge one ``daemons:`` alias entry into a config file's text,
    in place (#1226): the companion of :func:`set_scalars` for the
    one structural key. Every byte outside the alias's own block
    stays as the operator wrote it — the other aliases, their
    comments, the keys above and below, the spellings.

    A dict *entry* replaces the alias's block (the ``alias:`` line
    through the line before the next alias, comments at the alias's
    own indent included in the block they follow) or joins the
    ``daemons:`` block at its end, each key on its own line in the
    entry vocabulary's order, rendered by :func:`render_scalar`; the
    block itself is appended at the end of the file when the file
    carries none, and a ``daemons:`` line with an empty inline value
    (``{}``, ``~``) is opened up for the entry. A ``daemons:`` line
    carrying its entries inline (``daemons: {lab: {url: ...}}``) is
    refused: the edit cannot keep what it does not spell out.
    ``None`` deletes the alias's block, and with the last entry the
    ``daemons:`` line goes too; it is a no-op when neither the alias
    nor the block stands. The caller validates the result with its
    own loader before writing.
    """
    lines = text.split("\n")
    start = key_line_index(lines, "daemons")
    if start is None:
        return open_block(lines, alias, entry)
    open_line(lines, start)
    end = block_end(lines, start)
    indent = child_indent(lines, start + 1, end)
    at = alias_line(lines, alias, start + 1, end, indent)
    if entry is None:
        if at is not None:
            del lines[at : alias_block_end(lines, at, end, indent)]
            drop_emptied(lines, start)  # the block shrank: its end moved
    elif at is None:
        lines[end:end] = render_entry(alias, entry, indent)
    else:
        lines[at : alias_block_end(lines, at, end, indent)] = render_entry(
            alias, entry, indent
        )
    return "\n".join(lines)


def open_line(lines: list[str], start: int) -> None:
    """The ``daemons:`` line opened up for block entries (#1226): a
    bare line stays as written, comment and all; one with an empty
    inline value loses the value (and its comment with it, the way
    :func:`set_scalar` treats an edited line); one carrying entries
    inline is refused."""
    line = lines[start]
    match = KEY_LINE.match(line)
    inline = line[match.end() :].split("#", 1)[0].strip() if match else ""
    if inline in ("", "{}", "~", "null"):
        if inline:
            lines[start] = f"{key_line_key(line)}:"
        return
    raise ValueError(
        "the daemons: block carries its entries inline "
        f"({line.strip()}); write each alias out under daemons: to "
        "edit them here"
    )


def drop_emptied(lines: list[str], start: int) -> None:
    """The ``daemons:`` line gone when its last entry went (#1226):
    a bare key with nothing under it reads as null, which the loader
    refuses. Comments under it stay where they are."""
    end = block_end(lines, start)
    if any(
        entry.strip() and not entry.lstrip().startswith("#")
        for entry in lines[start + 1 : end]
    ):
        return
    del lines[start]


def open_block(lines: list[str], alias: str, entry: dict | None) -> str:
    """The file without a ``daemons:`` block (#1226): the text as it
    stands for a removal, the block opened at the file's end with
    the entry under it for a set."""
    if entry is None:
        return "\n".join(lines)
    block = ["daemons:", *render_entry(alias, entry, DAEMON_INDENT)]
    return "\n".join(append_block(lines, block))


def render_entry(alias: str, entry: dict, indent: str) -> list[str]:
    """One alias's entry as block lines (#1226): the ``alias:`` line
    at the block's child indent, each set key under it in the
    entry vocabulary's order, one line each, blanks left out. The
    alias and the values are spelled by ``yaml.safe_dump``, so a
    name or a value that needs quoting gets it. An entry's values
    are strings (the loader refuses a number for an address); an
    empty entry renders as the empty mapping."""
    refuse_non_strings(alias, entry)
    spelled = yaml.safe_dump(
        {alias: 0}, default_flow_style=False, allow_unicode=True
    ).rstrip("\n")
    spelled = spelled[: -len(": 0")]
    lines = [indent + spelled + (":" if any(entry.values()) else ": {}")]
    for key in DAEMON_ENTRY_ORDER:
        if entry.get(key):
            value = render_scalar(entry[key])
            lines.append(f"{indent}{DAEMON_INDENT}{key}: {value}")
    return lines


def refuse_non_strings(alias: str, entry: dict) -> None:
    """An entry's contract (#1226): every value a string, refused by
    name before the entry is rendered — an address, a path, or an
    image reference is text in the file, never a number."""
    for key, value in entry.items():
        if not isinstance(value, str):
            raise ValueError(
                f"daemon {alias!r} key {key!r} must be a string, got "
                f"{type(value).__name__}"
            )


def child_indent(lines: list[str], start: int, end: int) -> str:
    """The indent of the ``daemons:`` block's first entry (#1226):
    the leading whitespace of its first non-blank line that is not a
    comment, the default when the block is empty."""
    for line in lines[start:end]:
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            return line[: len(line) - len(stripped)]
    return DAEMON_INDENT


def alias_line(
    lines: list[str], alias: str, start: int, end: int, indent: str
) -> int | None:
    """The line of the alias's entry (#1226): a non-blank, non-comment
    line at the block's child indent whose key, quotes stripped,
    spells the alias; None when the block carries none."""
    for index in range(start, end):
        if is_alias_line(lines[index], indent, alias):
            return index
    return None


def is_alias_line(line: str, indent: str, alias: str) -> bool:
    """Whether one line is *alias*'s own entry line (#1226): a
    non-blank, non-comment line at the block's child indent whose
    key, quotes stripped, spells the alias."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return False
    if line[: len(line) - len(stripped)] != indent:
        return False
    match = KEY_LINE.match(stripped)
    return bool(match) and unquote(match.group(1).strip()) == alias


def alias_block_end(lines: list[str], at: int, end: int, indent: str) -> int:
    """Where the alias's block ends (#1226): the next non-blank line
    at the alias's indent or shallower, inside the ``daemons:``
    block — a comment at the alias's indent before the next alias
    ends the block, so the comment stays with the file."""
    stop = at + 1
    while stop < end:
        line = lines[stop]
        if line.strip() and len(line) - len(line.lstrip()) <= len(indent):
            break
        stop += 1
    return stop


def unquote(spelled: str) -> str:
    """A YAML key's name (#1226): the quoted forms stripped, the
    plain form as written."""
    if len(spelled) >= 2 and spelled[0] == spelled[-1] and spelled[0] in "'\"":
        return spelled[1:-1]
    return spelled


#: The entry keys' order in a rendered entry (#1226): the address
#: first, the credentials, the image last — the template's own
#: order, and :data:`msks.client.config_keys.DAEMON_ENTRY_KEYS`'
#: (the vocabulary lives in the client; this leaf spells its own
#: copy so the client's config module keeps importing the leaf,
#: never the reverse).
DAEMON_ENTRY_ORDER = (
    "url",
    "socket",
    "token_file",
    "cafile",
    "expected_image",
)
