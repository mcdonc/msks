"""The ssh agent-forwarding argument surgery.

Reading what a command line asks for (plain flags, bundles,
``-o`` options), rewriting it to forward or not forward the
agent, and the spans each edit cuts — the machinery
``msks ssh`` and ``rsync`` share.
"""

from __future__ import annotations

import os

from . import agent


def operator_agent_socket(command: str = "msks ssh") -> str:
    """The operator's agent socket, for forwarding into the guest —
    the one the operator's environment names
    (:func:`msks.client.agent.environment_agent`).

    Forwarding was asked for — on the command line or through the
    config's remembered options — so an environment that names no
    live agent is an error here: ssh would otherwise forward the
    session's transient agent, and the operator's keys — the whole
    point of ``-A`` — would quietly not be there.
    """
    socket = agent.environment_agent()
    if socket is None:
        named = os.environ.get("SSH_AUTH_SOCK", "") or "unset"
        raise SystemExit(
            f"{command}: -A (or ForwardAgent=yes, or the config's "
            "ssh_options) forwards the operator's agent, and "
            f"SSH_AUTH_SOCK ({named}) names no agent socket — start "
            "one (ssh-agent, or the desktop agent) or drop the "
            "forwarding option"
        )
    return socket


def forward_agent_value(value: str) -> str | None:
    """The forwarding target an ssh ``-o`` value names, when it is a
    ForwardAgent setting (any whitespace separates keyword and
    value, as ssh's option parser accepts). The target is returned
    as written — normalization is :func:`normalized_target`'s
    job, so a passthrough token survives verbatim."""
    for sep in ("=", " ", "\t"):
        if value.lower().startswith("forwardagent" + sep):
            return value[len("forwardagent") + 1 :]
    return None


def normalized_target(target: str) -> str:
    """A ForwardAgent target as ssh reads it: outer whitespace
    trimmed, surrounding double quotes stripped (ssh's parser drops
    them), a leading ``=`` from the spaced ``key = value`` spelling
    dropped, lowercase — so a comparison here matches ssh's own
    keyword-value parsing."""
    return target.strip().strip('"').lstrip("=").strip().lower()


#: ssh short options that carry a value attached or beside them —
#: scanning a bundled token stops at the first of these (the rest
#: is that option's value, not more flags). The union across
#: supported ssh generations, from ``ssh -h``: B b c D E e F I i
#: J L l m O o p Q R S W w.
VALUE_TAKING_SHORTS = "BDEFIJLOPQRSWbceilmopw"


def flags_until_value(arg: str) -> str:
    """The leading short flags of a bundled token, up to the first
    option that carries its value attached — the rest of the token
    is that value, not more flags (``-JAdmin@h`` names a jump
    host, not a bundle)."""
    for pos, ch in enumerate(arg):
        if ch in VALUE_TAKING_SHORTS:
            return arg[:pos]
    return arg


def is_flag_bundle(arg: str) -> bool:
    """Whether the token is a bundled short-flag cluster this pass
    decomposes: not an ssh long option, not a value-attached option
    form (``-o``/``-l`` own everything after themselves), and at
    least one flag before any attached value."""
    if len(arg) < 3 or not arg.startswith("-"):
        return False
    if arg.startswith(("--", "-o", "-l")):
        return False
    return flags_until_value(arg[1:]) != ""


def bundle_flags(arg: str) -> str:
    """The flag characters of a bundled token (empty when the token
    is no bundle)."""
    return flags_until_value(arg[1:]) if is_flag_bundle(arg) else ""


def bundled_option_value(
    arg: str, index: int, options: list[str]
) -> str | None:
    """The ``-o`` value a bundled token carries, when its flag
    portion ends in ``o``: the attached remainder
    (``-voForwardAgent=yes``) or the next argv token
    (``-vo ForwardAgent=yes``) — ssh accepts both, and the value
    counts as a ForwardAgent setting the same as a plain ``-o``'s
    would. The flag portion is non-empty by precondition (the
    caller checks :func:`is_flag_bundle`)."""
    flags = flags_until_value(arg[1:])
    rest = arg[1 + len(flags) :]
    if not rest.startswith("o"):
        return None
    attached = rest[1:]
    if attached:
        return attached
    if index + 1 < len(options):
        return options[index + 1]
    return None


def option_values(options: list[str]):
    """Every ssh ``-o`` value on the line, in argv order — from
    plain ``-o`` tokens (inline or beside their value) and the
    ``-o`` a flag bundle carries."""
    for index, arg in enumerate(options):
        value = option_value(index, arg, options)
        if value is None and is_flag_bundle(arg):
            value = bundled_option_value(arg, index, options)
        if value is not None:
            yield value


def forward_agent_settings(options: list[str]):
    """Every ForwardAgent value on the line, in argv order, in
    every spelling ssh accepts."""
    for value in option_values(options):
        target = forward_agent_value(value)
        if target is not None:
            yield target


def named_forward_agent_target(options: list[str]) -> str | None:
    """The explicit socket some ForwardAgent setting on the line
    names, when one does: any target that is not yes/no/
    SSH_AUTH_SOCK — a path (absolute or relative), or a value that
    fails at dial time, which is stock's own answer (an empty value
    is stock's usage error). A stated socket is sticky in stock ssh:
    it wins over every flag and value in any order, so its presence
    means the operator already chose the socket and the rewrite
    stands down entirely."""
    for target in forward_agent_settings(options):
        if normalized_target(target) not in ("yes", "no", "ssh_auth_sock"):
            return target
    return None


def assigns_forwarding(flag_chars: str, result: bool | None) -> bool | None:
    """Fold each bundled flag character into the running
    assignment — ``A`` asks, ``a`` disables, later characters win
    as later flags do."""
    for ch in flag_chars:
        if ch == "A":
            result = True
        elif ch == "a":
            result = False
    return result


def last_flag_requests(options: list[str]) -> bool | None:
    """The last ``-A``/``-a`` on the line, bundles included — True
    when the last one asks for forwarding, False when it disables,
    None when no flag appears. ssh's flags assign unconditionally
    in argv order, over every plain value (``-o no -A`` and
    ``-A -o no`` both forward)."""
    result: bool | None = None
    for arg in options:
        if arg == "-A":
            result = True
        elif arg == "-a":
            result = False
        else:
            result = assigns_forwarding(bundle_flags(arg), result)
    return result


def effective_forwarding(options: list[str]) -> bool | None:
    """Whether the line, read as stock ssh reads it, asks for agent
    forwarding: the last ``-A``/``-a`` flag decides over every
    plain value; with no flag, the first ForwardAgent value does
    (plain values are first-obtained — ``-o no -o yes`` stays
    off)."""
    flag = last_flag_requests(options)
    if flag is not None:
        return flag
    for target in forward_agent_settings(options):
        return normalized_target(target) == "yes"
    return None


def forward_agent_args(
    options: list[str], command: str = "msks ssh"
) -> list[str]:
    """The passthrough's ssh options with command-line agent
    forwarding pointed at the operator's agent socket, mirroring
    stock ssh's own precedence exactly:

    - a ForwardAgent value that names a socket (anything but
      yes/no/SSH_AUTH_SOCK) is sticky — stock ssh resolves it over
      every flag and value in any order — so the rewrite stands
      down entirely and the operator's own spelling rides
      untouched;
    - otherwise the last ``-A``/``-a`` decides (flags assign
      unconditionally in order), and with no flag the first
      ForwardAgent value does (first-obtained) — a line stock
      resolves to off, or that names no forwarding at all, passes
      through untouched;
    - a line that resolves to forwarding gets the operator's
      socket stated at the FRONT of the options: a stated path wins
      over every flag and value behind it, so the user's own
      spellings (a trailing ``-a``, a ``no``, a bundled ``-vA``)
      ride along inert and stock resolves the operator's agent.

    This session authenticates through the transient workspace agent
    (``IdentityAgent``), and ssh forwards *that* socket when asked
    to forward at all — which is why a resolved request is answered
    with the operator's socket explicitly: the workspace identity
    stays an authentication credential; the guest receives the
    operator's real keys and nothing else. #123's own-key sessions
    authenticate through the operator's agent or an identity file;
    the rewrite then states the same socket ssh would forward
    anyway, and this pass reads as a no-op.

    ``command`` names the calling command for the missing-agent
    refusal (``msks rsync`` asks through here too, #385).
    """
    if named_forward_agent_target(options) is not None:
        return list(options)
    if effective_forwarding(options) is not True:
        return list(options)
    return [
        "-o",
        "ForwardAgent=" + config_quote(operator_agent_socket(command)),
        *options,
    ]


def without_forwarding(options: list[str]) -> list[str]:
    """The config's options with every ForwardAgent setting
    dropped — plain ``-A``/``-a`` tokens, ``ForwardAgent`` ``-o``
    pairs in both spellings, the ``-o`` a flag bundle carries with
    its value, and the ``A``/``a`` characters of remaining bundles'
    flag clusters.

    :func:`with_config_options` calls this when the command line
    names a forwarding setting of its own: stock ssh resolves a
    stated socket over every later flag and lets a later ``-A``
    reassign a plain ``-a``, so a remembered ``-A`` riding behind
    the operator's own ``-a`` would win by stock's own rules — the
    one precedence the "command line overrides the config" rule
    cannot get by ordering alone, so the config's forwarding is
    lifted out instead. A bundle whose flag cluster holds nothing
    but ``A``/``a`` (``-aA``) drops whole; one that keeps other
    flags (``-vA`` → ``-v``) keeps them.
    """
    out: list[str] = []
    index = 0
    while index < len(options):
        kept, step = forwarding_step(index, options[index], options)
        out += kept
        index += step
    return out


def forwarding_step(
    index: int, arg: str, options: list[str]
) -> tuple[list[str], int]:
    """One argument's (surviving tokens, argv advance): a
    ForwardAgent setting's span drops with its value consumed, a
    forwarding flag drops alone, a bundle keeps its other flags,
    and everything else rides untouched."""
    span = forwarding_span(index, arg, options)
    if span is not None:
        return [], span
    return forwarding_kept(arg), 1


def forwarding_kept(arg: str) -> list[str]:
    """One non-setting argument's surviving form."""
    if arg in ("-A", "-a"):
        return []
    if is_flag_bundle(arg):
        stripped = strip_bundle_a(arg)
        return [stripped] if stripped is not None else []
    return [arg]


def forwarding_span(index: int, arg: str, options: list[str]) -> int | None:
    """The argv span to drop for a ForwardAgent setting at *index*:
    the ``-o`` — plain or bundle-carried — with its value, two
    tokens when the value sits in the next argv slot, one when it
    is attached — or None when the argument names no ForwardAgent
    setting. A dropped ``-o`` takes its value with it: the value
    left behind would land in the remote command's place."""
    value = option_value(index, arg, options)
    if value is not None:
        return plain_forwarding_span(arg, value)
    if is_flag_bundle(arg):
        return bundle_forwarding_span(index, arg, options)
    return None


def plain_forwarding_span(arg: str, value: str) -> int | None:
    """The span of a plain or inline ``-o`` carrying a ForwardAgent
    setting — its value sits beside a plain ``-o`` or rides
    attached (``-oForwardAgent=...``)."""
    if forward_agent_value(value) is None:
        return None
    return 2 if arg == "-o" else 1


def bundle_forwarding_span(
    index: int, arg: str, options: list[str]
) -> int | None:
    """The span of a flag bundle whose flag cluster ends in ``o``
    (``-vo ForwardAgent=yes``): the value rides attached or in the
    next argv slot."""
    flags = flags_until_value(arg[1:])
    rest = arg[1 + len(flags) :]
    if not rest.startswith("o"):
        return None
    if rest[1:]:
        return setting_span(forward_agent_value(rest[1:]), 1)
    return beside_forwarding_span(index, options)


def beside_forwarding_span(index: int, options: list[str]) -> int | None:
    """The span when the setting's value sits in the next argv
    slot — a dangling end of argv names no setting to drop."""
    if index + 1 >= len(options):
        return None
    return setting_span(forward_agent_value(options[index + 1]), 2)


def setting_span(target: str | None, span: int) -> int | None:
    """*span* when the value names a ForwardAgent setting, None
    when it names none."""
    return span if target is not None else None


def strip_bundle_a(arg: str) -> str | None:
    """A flag bundle with its ``A``/``a`` characters gone — None
    when nothing of the token survives (the cluster held nothing
    but forwarding flags). Only the cluster's flag characters are
    touched: an attached value (``-vJA`` — ``A`` belongs to
    ``J``'s value) keeps every character it had."""
    flags = flags_until_value(arg[1:])
    rest = arg[1 + len(flags) :]
    kept = flags.replace("A", "").replace("a", "")
    if not kept:
        return None
    return "-" + kept + rest


def config_quote(value: str) -> str:
    """Double-quote a value for an ``-o`` option when it carries
    whitespace: double quotes are honored everywhere ssh parses
    option values (the parser strips them itself, and the shell
    strips the remainder), keeping the value whole."""
    return f'"{value}"' if any(c.isspace() for c in value) else value


def option_value(index: int, arg: str, args: list[str]) -> str | None:
    """The value of the ``-o`` at ``index`` — inline (``-oUser=x``)
    or the next argument — or None when ``arg`` names no option."""
    if arg.startswith("-o") and arg != "-o":
        return arg[2:]
    if arg == "-o" and index + 1 < len(args):
        return args[index + 1]
    return None
