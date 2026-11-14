"""The CLI's vocabulary: the choice enums, the passthrough
split, the interrupt wrapper, and the typer applications.

One module the command groups decorate onto — the root app and
its five sub-apps (egress, image, proxy, home, secret), with no
commands of its own.
"""

import enum
import functools
import sys
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from ..config import ClientConfig

from ..jsonio import json_mode
from ..jsonio import set_json_mode as set_json_mode
from ..proxies import app as proxy_app


class PostureChoice(enum.StrEnum):
    """The egress consent postures (#69)."""

    allow = "allow"
    static = "static"
    interactive = "interactive"


class CoverageChoice(enum.StrEnum):
    """The daemon-wide placeholder postures (#339)."""

    all = "all"
    scoped = "scoped"


class DecisionFilter(enum.StrEnum):
    """The consent row lifecycle states."""

    pending = "pending"
    allowed = "allowed"
    denied = "denied"
    expired = "expired"
    revoked = "revoked"


class VerdictChoice(enum.StrEnum):
    """The decider's two verdicts."""

    allow = "allow"
    deny = "deny"


class DurationChoice(enum.StrEnum):
    """How long enforcement honors a verdict."""

    once = "once"
    five_m = "5m"
    fifteen_m = "15m"
    tilrestart = "tilrestart"
    forever = "forever"


def passthrough_args(argv: list[str]) -> list[str]:
    """The ssh/rsync variadic's verbatim value: click hands the
    ``--`` separator through inside the list where argparse
    swallowed it, so one leading separator drops here — everything
    else, options included, reaches ssh/rsync exactly as typed."""
    return argv[1:] if argv[:1] == ["--"] else argv


#: Whether the current parse reached a command body: the help
# screens and the usage errors never do, and main()'s help gate
# reads the difference (a token spelled like a help flag that a
# command consumed as its value must not masquerade as one).
body_reached = False

#: The current invocation's tokens, recorded by run_parsed for the
#: root callback's help-screen check (see set_invocation_tokens).
invocation_tokens: list[str] = []

#: The invocation's resolved client config (#314), set by the root
#: callback right after the bootstrap and read by the TUI entry
#: points: the tree's new-terminal shell action (#341) spawns its
#: console child with the launcher the resolution carries. None
#: when no bootstrap ran (a help screen — and then no TUI starts
#: either).
invoked_conf: ClientConfig | None = None


def one_line_interrupts(fn):
    """A Ctrl-C during a long boot is one line, not a traceback (a
    raw-mode session never gets here — Ctrl-C reaches the guest):
    caught at the command body's edge, where typer's own
    conversion (a bare exit 130) cannot swallow the line. Marks
    the body-reached flag on the way in."""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        global body_reached
        body_reached = True
        try:
            return fn(*args, **kwargs)
        except KeyboardInterrupt:
            if not json_mode():
                print("msks: interrupted", file=sys.stderr)
            raise SystemExit(130) from None

    return wrapped


app = typer.Typer(
    name="msks",
    add_completion=False,
    help="msks client: workspace microvms over the daemon API",
    context_settings={"help_option_names": ["-h", "--help"]},
)

egress_app = typer.Typer(
    help="egress consent: decide, watch, and inspect (#69)"
)
image_app = typer.Typer(help="manage the daemon's image catalog (#65)")
home_app = typer.Typer(
    help="move a workspace's /home volume through the daemon (#80)"
)
secret_app = typer.Typer(
    help="placeholder secrets: mint, list, revoke, renew, unlock"
)
app.add_typer(egress_app, name="egress")
app.add_typer(image_app, name="image")
app.add_typer(proxy_app, name="proxy")
app.add_typer(home_app, name="home")
app.add_typer(secret_app, name="secret")
