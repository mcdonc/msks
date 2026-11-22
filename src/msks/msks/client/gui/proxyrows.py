"""The workspace proxies tab's presenter (phase 5.8 of #924,
#988): the plain arithmetic over the proxy rows — the direction
letter, the spec in the CLI's own grammar, the status word with
its cause, the parse that turns the form's fields into a create
body through the CLI's own spec parsers
(:mod:`msks.client.proxies`), and the seed the edit form reads.
Plain data, no Qt in it, mirroring ``tui/proxies.py``'s own pure
helpers; the parity with the TUI's spec grammar holds by test,
the listing presenter's own rule.
"""

from ..proxies import parse_local_spec, parse_remote_spec

#: The spec grammar's unnamed hosts (#809, mirrored from the
#: TUI's own constants — the parity test holds the pair): a
#: forward's default bind and a remote's default target, the
#: loopback the daemon binds when the row names none.
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_TARGET_HOST = "127.0.0.1"

#: The direction letters ssh gives the two kinds (#809): the
#: form's choices and the row cells' own vocabulary.
FORWARD_CHOICE = "-L"
REMOTE_CHOICE = "-R"

#: The direction select's choices, forward first — the add
#: path's own default.
DIRECTION_CHOICES = (FORWARD_CHOICE, REMOTE_CHOICE)

#: The letters' directions, the parse's own map.
LETTER_DIRECTIONS = {FORWARD_CHOICE: "forward", REMOTE_CHOICE: "remote"}


def bracketed_host(host: str) -> str:
    """One host in the spec grammar's own spelling: an IPv6
    literal arrives bracketed (the parser's own rule — the
    brackets close the colons the spec's own separator would
    otherwise eat), every other spelling stands as it reads."""
    return f"[{host}]" if ":" in host else host


def spec_text(row: dict) -> str:
    """One row's spec in the CLI's own grammar (#988, the TUI's
    own text): the exact words ``msks proxy add`` takes — a
    forward's bind appears only when the row names one, its host
    field the workspace's two spellings, an IPv6 literal
    bracketed so the text parses back."""
    if row["direction"] == "remote":
        target = bracketed_host(row.get("target_host") or DEFAULT_TARGET_HOST)
        return f"-R {row['listen_port']}:{target}:{row['target_port']}"
    listen = row.get("listen_host") or DEFAULT_LISTEN_HOST
    bind = (
        "" if listen == DEFAULT_LISTEN_HOST else f"{bracketed_host(listen)}:"
    )
    return f"-L {bind}{row['listen_port']}:localhost:{row['target_port']}"


def direction_letter(row: dict) -> str:
    """The row's direction as ssh's letter (#988): ``-L`` for a
    forward, ``-R`` for a remote."""
    return (
        REMOTE_CHOICE if row.get("direction") == "remote" else FORWARD_CHOICE
    )


def direction_spec(row: dict) -> str:
    """The spec cell's text (#988): the grammar minus its leading
    letter — the letter sits in its own column, and the seed the
    edit form reads is exactly this text."""
    return spec_text(row).split(" ", 1)[1]


def status_text(row: dict) -> str:
    """The row's status cell (#988, the TUI's own words): the
    word, its cause riding along when one stands — a listening
    row the guest cannot reach says so."""
    status = row.get("status") or "stopped"
    if row.get("reason"):
        return f"{status} ({row['reason']})"
    return status


def parsed_body(letter: str, raw: str) -> tuple[dict | None, str]:
    """The form's fields as a create body (#988): the CLI's own
    parser for the picked direction, its refusal in the
    command's words — the daemon never sees a half-parsed body.
    A local refusal answers ``(None, note)``; a whole body
    ``((body), "")`` — the label lands on it when the form
    carries one."""
    if not raw:
        return None, "the spec is the form's one required field"
    parser = (
        parse_local_spec
        if LETTER_DIRECTIONS.get(letter, "forward") == "forward"
        else parse_remote_spec
    )
    try:
        return parser(raw), ""
    except SystemExit as exc:
        return None, str(exc).replace("msks: ", "refused: ")


def form_seeds(row: dict | None) -> dict:
    """The form's seeded values (#988): the edited row's letter,
    its spec text, and its label — the add path's blanks with
    the forward letter standing."""
    if row is None:
        return {"letter": FORWARD_CHOICE, "spec": "", "label": ""}
    return {
        "letter": direction_letter(row),
        "spec": direction_spec(row),
        "label": str(row.get("label") or ""),
    }


def proxy_transition(frame: dict) -> bool:
    """Whether one pushed frame is a proxy transition (#988) —
    the events the proxy subsystem publishes: ``proxy.created``,
    ``proxy.updated``, ``proxy.listening``, ``proxy.failed``,
    ``proxy.closed``, ``proxy.removed``."""
    return str(frame.get("event") or "").startswith("proxy.")
