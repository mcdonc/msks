"""The client config's env-var vocabulary.

The global keys the file validates and the two environment
variables that override them — one leaf both the config module
and its validator read.
"""

from __future__ import annotations

#: The file's scalar global keys, each with the ``MSKSC_*``
#: variable that overrides it. ``token_file``'s variable carries
#: the token itself, not a path — the file form points at a file so
#: the config tree stays free of inline credentials (#314).
#: ``identity_file`` names the operator's own private key file
#: (#336) and is global-only (no per-alias form): an identity
#: belongs to the operator, not to a daemon connection.
#: ``pubkey_file`` names the operator's key's public half (#495)
#: and carries the same global-only rule for the same reason.
GLOBAL_ENV_VARS: dict[str, str] = {
    "url": "MSKSC_URL",
    "socket": "MSKSC_SOCKET",
    "token_file": "MSKSC_TOKEN",
    "cafile": "MSKSC_CAFILE",
    "expected_image": "MSKSC_EXPECTED_IMAGE",
    "cache_dir": "MSKSC_CACHE_DIR",
    "state_dir": "MSKSC_STATE_DIR",
    "identity_file": "MSKSC_IDENTITY_FILE",
    "pubkey_file": "MSKSC_PUBKEY_FILE",
    "image_manifest_url": "MSKSC_IMAGE_MANIFEST_URL",
}

#: The terminal-launch setting's variable (#314): the string form
#: of the file value, overriding it for one shell.
TERMINAL_ENV_VAR = "MSKSC_TERMINAL_OPEN_CMD"

#: The remembered-passthrough setting's variable (#385): the string
#: form of the file value (shell-split), overriding it for one
#: shell — the same shape :data:`TERMINAL_ENV_VAR` carries.
SSH_OPTIONS_ENV = "MSKSC_SSH_OPTIONS"

#: The keys one ``daemons:`` entry may carry. Exactly one of
#: ``url`` and ``socket`` is required — a remote daemon's address
#: or a local listener's socket path (#555); the rest override
#: their global forms for that alias. A ``socket`` entry needs no
#: ``token_file``: the local listener authenticates by peer
#: credential.
DAEMON_ENTRY_KEYS = ("url", "socket", "token_file", "cafile", "expected_image")
