"""The first-run config template.

The commented msks.yaml a first run writes — one template text,
the write path prints and ``msks config edit`` seeds from.
"""

from __future__ import annotations


def render_template() -> str:
    """The generated ``msks.yaml`` body: a commented near-empty file.

    The template's purpose is discoverability — this is where the
    client's config lives — plus a commented example of every key.
    The settings themselves come from the built-in defaults and the
    environment until the operator edits the file.
    """
    return """\
# msks client configuration — generated on first run.
#
# msks looked here because it was started without a --config
# argument and found no file at
# ${{MSKSC_CONFIG_DIR:-$XDG_CONFIG_HOME/msks}}/msks.yaml.
#
# This file is the durable home for the client's settings. Every
# scalar key here also exists as an MSKSC_* environment variable,
# spelled the same with the prefix stripped and lowercased
# (MSKSC_URL -> url, MSKSC_EXPECTED_IMAGE -> expected_image).
# Hyphens and underscores spell the same key (terminal-open-cmd
# and terminal_open_cmd are one key; a file that carries both
# spellings is refused). Precedence, highest first:
#   --daemon flag > MSKSC_* environment > this file's daemons
#   entries > this file's global keys > built-in defaults
#
# A key set to nothing (key: with no value, or "") is the unset
# form: the environment (or the default) applies.
#
# The full reference — every key, alias, and the precedence rule
# with examples — is docs/cli.md in the msks repository:
# https://github.com/mcdonc/msks
#
# --- Example (every line commented; nothing is set) ---
#
# --- Global keys ---
# url: https://127.0.0.1:8660    # the daemon's base URL
# token_file: ~/.config/msks/dev.token  # a file holding one daemon
#                                # token (the config tree stays free
#                                # of inline credentials); the
#                                # MSKSC_TOKEN variable still carries
#                                # an inline token
# cafile: ~/.config/msks/msks-ca.pem  # the CA that verifies the
#                                # daemon's certificate
# expected_image: msks/debian13:1.0  # the image msks ls compares
#                                # against the daemon's /health (#160)
# cache_dir: ~/.cache/msks      # per-workspace host-key caches (#251)
# state_dir: ~/.local/state/msks  # the per-user state root: the
#                                # session records every interactive
#                                # msks ssh heartbeat there (#858),
#                                # and the TUI counts them per
#                                # workspace as open shells
# identity_file: ~/.ssh/id_ed25519  # your own private key — the ssh
#                                # identity a bare msks create plants
#                                # into every workspace (#336); msks
#                                # reads it in place and copies it
#                                # nowhere. Global-only: a per-alias
#                                # identity_file is refused
# pubkey_file: ~/.ssh/id_ed25519.pub  # your key's public half —
#                                # what a bare create plants instead
#                                # of reading identity_file (#495):
#                                # the form for a key you keep
#                                # encrypted (your agent holds it;
#                                # msks never reads the private half).
#                                # --pubkey overrides it for one
#                                # create. Global-only, like
#                                # identity_file
# terminal_open_cmd: konsole -e # the launcher the workspace page's
#                                # shell action opens shells through
#                                # in new windows (#314, #341, #562);
#                                # string form is shell-split, a list
#                                # form carries its words as written;
#                                # unset (the default) -> the shell
#                                # runs in the TUI's own terminal
# image_manifest_url: https://example.com/images.json
#                                # where the TUI's Import image action
#                                # reads the published channel's manifest
#                                # (#565); the default is this
#                                # repository's latest release
# ssh_options:                  # ssh options remembered for every ssh
#                                # session msks runs — msks ssh, the
#                                # new-terminal shell, msks rsync's
#                                # transport (#385). The tokens are
#                                # the ones you would type after --
#                                # on the command line, and the
#                                # command line still overrides them;
#                                # MSKSC_SSH_OPTIONS carries the string
#                                # form for one shell
# ssh_options: -A -o ServerAliveInterval=30   # string (shell-split)
# ssh_options:                  # list form (tokens stay whole)
#   - -A
#   - -o
#   - ServerAliveInterval=30
#
# --- Named daemon aliases ---
# One entry per daemon you talk to; exactly one of url and socket
# is required (#555) — a remote daemon's address, or a local
# listener's socket path (peer-credential auth: no token_file,
# no cafile). The rest override their global keys for that alias.
# daemons:
#   dev:
#     socket: /run/user/1000/msks/msksd.sock   # the local listener this
#                                    # host's daemon serves
#   lab:
#     url: https://hv-1.lab.example.com:8660
#     token_file: ~/.config/msks/lab.token
#     cafile: ~/.config/msks/lab-ca.pem
#     expected_image: msks/debian13:1.1
#
# active_daemon: lab           # the alias a bare msks addresses;
#                              # unset, a bare msks looks for the
#                              # default local socket first (#555)
"""
