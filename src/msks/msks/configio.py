"""Config-file reading and first-run writing, shared by msksd and
the client (#397): one duplicate-key grammar and one exclusive
first-run create, so the daemon's config and the client's config
carry the same file discipline. A leaf — yaml and stdlib only;
importing it loads no daemon composition.
"""

import os
from pathlib import Path

import yaml


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
