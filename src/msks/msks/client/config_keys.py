"""The client config's env-var vocabulary.

The global keys the file validates and the two environment
variables that override them — one leaf both the config module
and its validator read.
"""

from __future__ import annotations

#: The dictation keys (#1214): the GUI's dictate pill's settings —
#: the model (a ggml name the cache fetches, or a path to a
#: ``.bin``), the capture device (a ``pw-record`` target: a node
#: name or serial), the gate's three numbers, and the vocabulary
#: prompt whisper-cli biases its spelling with. Global-only, the
#: rule ``identity_file`` carries: the microphone and the models
#: belong to the operator's desktop, not to a daemon connection.
#: The GUI's own preferences file (``gui.yaml``) sits between the
#: environment and the operator's config for these keys — the
#: settings page writes that file and never this one — so the
#: environment still pins a key above both. Each key's variable
#: is the rule's own, ``MSKSC_`` plus the key uppercased.
DICTATE_KEYS = (
    "dictate_model",
    "dictate_device",
    "dictate_threshold",
    "dictate_margin",
    "dictate_silence",
    "dictate_prompt",
)

#: The file's scalar global keys, each with the ``MSKSC_*``
#: variable that overrides it. ``token_file``'s variable carries
#: the token itself, not a path — the file form points at a file so
#: the config tree stays free of inline credentials (#314).
#: ``identity_file`` names the operator's own private key file
#: (#336) and is global-only (no per-alias form): an identity
#: belongs to the operator, not to a daemon connection.
#: ``pubkey_file`` names the operator's key's public half (#495)
#: and carries the same global-only rule for the same reason.
#: ``screenshot_command`` and ``screenshot_port`` configure the
#: client-held screenshot pipe (#1194) and carry the same rule:
#: the screen the pipe captures belongs to the operator's
#: desktop, not to a daemon connection. The dictation keys
#: (#1214) join with the same global-only rule.
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
    "screenshot_command": "MSKSC_SCREENSHOT_COMMAND",
    "screenshot_port": "MSKSC_SCREENSHOT_PORT",
}
GLOBAL_ENV_VARS.update({key: "MSKSC_" + key.upper() for key in DICTATE_KEYS})

#: The terminal-launch setting's variable (#314): the string form
#: of the file value, overriding it for one shell.
TERMINAL_ENV_VAR = "MSKSC_TERMINAL_OPEN_CMD"

#: The remembered-passthrough setting's variable (#385): the string
#: form of the file value (shell-split), overriding it for one
#: shell — the same shape :data:`TERMINAL_ENV_VAR` carries.
SSH_OPTIONS_ENV = "MSKSC_SSH_OPTIONS"

#: The screenshot pipe's two settings (#1194): the capture
#: command whose presence enables the pipe, and the guest's
#: loopback port its sshd binds. The variables are the one-shell
#: forms of the file's global-only keys.
SCREENSHOT_COMMAND_ENV = "MSKSC_SCREENSHOT_COMMAND"
SCREENSHOT_PORT_ENV = "MSKSC_SCREENSHOT_PORT"

#: The keys one ``daemons:`` entry may carry. Exactly one of
#: ``url`` and ``socket`` is required — a remote daemon's address
#: or a local listener's socket path (#555); the rest override
#: their global forms for that alias. A ``socket`` entry needs no
#: ``token_file``: the local listener authenticates by peer
#: credential.
DAEMON_ENTRY_KEYS = (
    "url",
    "socket",
    "token_file",
    "cafile",
    "expected_image",
)
