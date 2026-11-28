"""The settings page's vocabulary (#1214, #1226): every client
``msks.yaml`` key's label and long description, the subtabs'
labels, and the resolution a field shows — plain data over the
client's own config keys, no Qt in it, so the panels and the
tests read one definition. A field's tooltip is the setting's
description, the same words the docs chapter uses; a key the
environment pins appends its variable's notice to both the field
and its label.

The dictation keys keep their own record
(:mod:`msks.client.gui.dictatekeys`); the other keys resolve the
way every client key does — the variable, then the merged
``msks.yaml`` document (the user's file over the system's, #1224),
then the default the readers hold. A field shows the effective
value, and a key no layer carries shows blank: the blank is the
file's own unset form, and the default applies.
"""

from __future__ import annotations

import shlex

from ...config_keys import (
    GLOBAL_ENV_VARS,
    SSH_OPTIONS_ENV,
    TERMINAL_ENV_VAR,
)

#: The subtabs' labels (#1226), in the strip's order.
CONNECTION_LABEL = "Connection"
FILES_LABEL = "Files"
SHELL_LABEL = "Terminal and ssh"
SCREENSHOT_LABEL = "Screenshot"
DICTATION_LABEL = "Dictation"

#: The subtab labels in strip order (#1226).
SUBTAB_LABELS = (
    CONNECTION_LABEL,
    FILES_LABEL,
    SHELL_LABEL,
    SCREENSHOT_LABEL,
    DICTATION_LABEL,
)

#: The marker a subtab with unsaved edits wears before its label
#: (#1226): the editor's own "modified" convention.
MODIFIED = "* "

#: The pin notice's shape (#1214): the variable, then the rule.
PIN_NOTICE = "%s pins this setting; the page cannot change it."
PIN_LINE = "%s pins this setting"

#: The device row's Refresh button (#1214).
REFRESH_TIP = (
    "Read the desktop's source list again: a microphone plugged in or "
    "a source renamed since the page opened joins the picker."
)

#: Each setting's label on the form (#1226): the name the operator
#: reads beside the field.
LABELS = {
    "url": "URL",
    "socket": "Socket",
    "token_file": "Token file",
    "cafile": "CA file",
    "expected_image": "Expected image",
    "active_daemon": "Active alias",
    "cache_dir": "Cache directory",
    "state_dir": "State directory",
    "identity_file": "Identity file",
    "pubkey_file": "Public key file",
    "image_manifest_url": "Image manifest URL",
    "terminal_open_cmd": "Terminal launcher",
    "ssh_options": "SSH options",
    "screenshot_command": "Capture command",
    "screenshot_port": "Port",
    "dictate_model": "Model",
    "dictate_device": "Device",
    "dictate_prompt": "Vocabulary prompt",
}

#: Each setting's long description (#1214, #1226): the field's
#: tooltip, the same words for the page and for the docs chapter.
TIPS = {
    "url": (
        "The daemon's base URL, such as https://127.0.0.1:8660. A bare "
        "msks addresses this daemon when no alias is active and the "
        "environment names none. Leave it blank and msks looks for the "
        "default local socket first."
    ),
    "socket": (
        "The local listener's socket path, such as "
        "/run/user/1000/msks/msksd.sock. The listener authenticates by "
        "peer credential, so a socket connection needs no token file "
        "and no CA file. Leave it blank and msks looks for the default "
        "socket."
    ),
    "token_file": (
        "A file holding one daemon token, such as "
        "~/.config/msks/dev.token. The config tree stays free of "
        "inline credentials, and the token keeps the permissions of "
        "its file. MSKSC_TOKEN carries an inline token for one shell "
        "instead."
    ),
    "cafile": (
        "The CA certificate that verifies the daemon's TLS "
        "certificate, such as ~/.config/msks/msks-ca.pem. The daemon "
        "writes its CA into its state dir on first serve."
    ),
    "expected_image": (
        "The image msks ls compares against the daemon's /health "
        "report, such as msks/debian13:1.1. A daemon running another "
        "image is reported, so a stale daemon never goes unnoticed. "
        "Leave it blank for no comparison."
    ),
    "active_daemon": (
        "The alias a bare msks addresses. The --daemon flag picks one "
        "for a single invocation, and an MSKSC_URL or MSKSC_SOCKET "
        "export still wins over this pick in a shell that sets one."
    ),
    "daemons": (
        "One entry per daemon you talk to. Each entry names its daemon "
        "one way, a url or a socket, and its token_file, cafile, and "
        "expected_image override the global keys for that alias. "
        "msks --daemon <alias> addresses one for a single invocation."
    ),
    "cache_dir": (
        "The cache root, such as ~/.cache/msks. The per-workspace "
        "host-key caches and the speech models live under it."
    ),
    "state_dir": (
        "The per-user state root, such as ~/.local/state/msks. The "
        "session records of every interactive msks ssh live there, "
        "and the TUI counts them per workspace as open shells."
    ),
    "identity_file": (
        "Your own private key file, such as ~/.ssh/id_ed25519. A bare "
        "msks create plants this key into every workspace. msks reads "
        "it in place and copies it nowhere. The key is global-only: a "
        "per-alias identity_file is refused."
    ),
    "pubkey_file": (
        "Your key's public half, such as ~/.ssh/id_ed25519.pub. A bare "
        "create plants this key instead of reading identity_file, the "
        "form for a key you keep encrypted. ssh sessions then sign "
        "through your agent. --pubkey overrides it for one create. "
        "Global-only, like identity_file."
    ),
    "image_manifest_url": (
        "Where the Import image action reads the published channel's "
        "manifest. The default is this repository's latest release."
    ),
    "terminal_open_cmd": (
        "The launcher that opens a workspace shell in a new terminal "
        "window, such as konsole -e or xterm -e. The string is "
        "shell-split, and msks appends its own ssh invocation after "
        "it. Leave it blank and the shell runs in the TUI's own "
        "terminal."
    ),
    "ssh_options": (
        "SSH options remembered for every ssh session msks runs: msks "
        "ssh, the workspace page's shell action, and msks rsync. Type "
        "them as you would after -- on the command line, such as -A "
        "-o ServerAliveInterval=30. A -- in the value is refused, and "
        "the command line's own options win for one invocation."
    ),
    "screenshot_command": (
        "The capture command a workspace's screenshot pipe runs on "
        "this desktop, such as import -window root. Its stdout is the "
        "image the guest receives, and the command's presence alone "
        "enables the pipe. The requested monitor arrives as the "
        "MSKS_SCREENSHOT_MONITOR variable, never in the argv."
    ),
    "screenshot_port": (
        "The loopback port the guest's sshd binds for the screenshot "
        "pipe, 1-65535. The default is 8662."
    ),
    "dictate_model": (
        "The whisper.cpp speech model. Pick a known name and Download "
        "fetches it into ~/.cache/msks/whisper/; a larger "
        "model transcribes more accurately and loads more slowly at "
        "each clip (about a second with small, two to three with "
        "large-v3-turbo). A path to a .bin file uses that file instead "
        "and downloads nothing: a NixOS host pins a model as a "
        "derivation and types its store path here."
    ),
    "dictate_device": (
        "The microphone the recording captures from: a PipeWire "
        "source. The picker lists the sources the desktop serves "
        "(Refresh reads the list again); the default input follows "
        "the desktop's own choice. A node name or serial typed free "
        "selects one the list does not carry."
    ),
    "dictate_prompt": (
        "The vocabulary whisper-cli biases its spelling toward: the "
        "names it would otherwise misspell, comma-separated. Leave "
        "it empty for plain dictation."
    ),
}

#: Every scalar key's variable (#1226): the ``MSKSC_*`` globals plus
#: the two string-form keys whose variables the vocabulary keeps
#: apart. A non-blank variable pins its key.
VARIABLES = {
    **GLOBAL_ENV_VARS,
    "terminal_open_cmd": TERMINAL_ENV_VAR,
    "ssh_options": SSH_OPTIONS_ENV,
}


def pin_notice(key: str) -> str:
    """The notice a pinned key's field carries (#1214, #1226)."""
    return PIN_NOTICE % VARIABLES[key]


def pinned_keys(environ) -> frozenset:
    """The keys a non-blank variable pins (#1226): blank is the unset
    form for every reader, so a blank export pins nothing."""
    return frozenset(
        key for key, var in VARIABLES.items() if environ.get(var, "").strip()
    )


def display_value(value) -> str:
    """A document value as a field shows it (#1226): a list-form
    command shell-joined into the string form (the same round trip
    the resolution gives the environment), a port as its digits,
    None as blank, a string as written."""
    if value is None:
        return ""
    if isinstance(value, list):
        return shlex.join(str(word) for word in value)
    return str(value).strip()


def effective(key: str, environ, doc: dict) -> tuple[str, bool]:
    """One key's value for its field (#1226): ``(value, pinned)``, the
    variable's non-blank text first, then the merged document's value
    in its display form. A key with no variable (``active_daemon``)
    never pins. The dictation keys resolve through their own record
    and never come here."""
    var = VARIABLES.get(key)
    env = environ.get(var, "") if var else ""
    if env.strip():
        return env.strip(), True
    return display_value((doc or {}).get(key)), False
