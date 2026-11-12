"""The CLI's machine-readable half (#841): the ``--json`` emit
helper, the invocation's JSON-mode flag, the API-status exit, and
the exit-code table.

Every command that prints takes ``--json``. The flag switches its
confirmation to one JSON document on stdout and its failures to
one error document on stderr, and the table maps a failure to the
code a harness branches on. Human output stays the default and
keeps its shape.
"""

import json
import sys

#: Success: the command's own exchange completed.
EXIT_OK = 0
#: A failure the table names no code for: an unreachable daemon,
#: a timeout, a refused request the daemon answers with a status
#: outside the table.
EXIT_FAILURE = 1
#: A bad invocation (click's usage error).
EXIT_USAGE = 2
#: The daemon answered 404: no such workspace, image, or row.
EXIT_NOT_FOUND = 3
#: The daemon answered the duplicate-name 409 ("workspace
#: exists"): a create or clone with a name another row holds.
EXIT_CONFLICT = 4
#: The workspace's state or placement refuses the action: the
#: 409s that are not the duplicate-name one (a clone source that
#: runs, a placement the daemon does not own), and the start-again
#: refusal (a start against a workspace whose VMM already runs).
EXIT_WRONG_STATE = 5
#: A Ctrl-C.
EXIT_INTERRUPT = 130

#: The start-again refusal's anchor (#825's wording): the one
#: wrong-state answer that arrives as a 503 rather than a 409 — a
#: boot failure is also 503, so the anchor separates them.
START_AGAIN_DETAIL = "already exists; shutdown or cleanup first"

#: The duplicate-name conflict's detail, verbatim: the create and
#: clone races answer this exact string, and it is the one 409
#: that means a NAME conflict rather than a state one.
NAME_CONFLICT_DETAIL = "workspace exists"

_json_mode = False


class ApiExit(SystemExit):
    """The API-status failure: the daemon's status and detail ride
    beside the one-line message, so ``--json`` mode prints the
    structured error and the exit table maps the status. A
    subclass on purpose: every existing ``except SystemExit`` —
    the recovery re-wraps, the probe's silencer — keeps working
    unchanged."""

    def __init__(self, message: str, status: int, detail: str) -> None:
        super().__init__(message)
        self.api_status = status
        self.api_detail = detail


def set_json_mode(on: bool) -> None:
    """Record the invocation's ``--json`` at the command body's
    edge — the parse accepted the flag, so every failure from here
    on is machine-readable too."""
    global _json_mode
    _json_mode = on


def json_mode() -> bool:
    """Whether this invocation asked for machine-readable output."""
    return _json_mode


def outcome(as_json: bool, doc, line) -> None:
    """One command's confirmation: the JSON document, or the human
    line. ``doc`` is the affected row (or reply) the command holds
    beside its human wording; ``line`` is the wording, or a
    callable returning it — the sites whose wording reads fields
    the document itself may not carry pass it lazily, so a JSON
    run never evaluates a human line."""
    if as_json:
        emit(doc)
        return
    print(line() if callable(line) else line)


def emit(doc) -> None:
    """One JSON document, indented for a reader — the shape every
    single-document ``--json`` emit uses, matching the list
    commands the CLI already carried."""
    print(json.dumps(doc, indent=2))


def json_line(doc) -> None:
    """One compact JSON document on its own line: the streaming
    commands (``rm``) confirm each id as it lands, so a partial
    run's survivors stay on the record."""
    print(json.dumps(doc))


def error_document(status: int | None, detail: str) -> None:
    """The structured failure on stderr: the daemon's status
    (None for a failure that never reached one) and its detail."""
    print(
        json.dumps({"error": {"status": status, "detail": detail}}),
        file=sys.stderr,
    )


def conflict_code(detail: str) -> int:
    """Which 409: the duplicate-name detail is the conflict, any
    other 409 names a state or placement refusal."""
    if detail == NAME_CONFLICT_DETAIL:
        return EXIT_CONFLICT
    return EXIT_WRONG_STATE


def exit_code(status: int | None, detail: str) -> int:
    """The table a harness branches on (docs/cli.md carries it):
    404 is not-found, the 409s split by their detail, the
    start-again refusal is wrong state, and everything else is
    the unnamed failure."""
    if status == 404:
        return EXIT_NOT_FOUND
    if status == 409:
        return conflict_code(detail)
    if status == 503 and START_AGAIN_DETAIL in detail:
        return EXIT_WRONG_STATE
    return EXIT_FAILURE


def api_failure(status: int, detail: str) -> int:
    """The JSON-mode exit for an API-status failure: the error
    document on stderr, then the table's code."""
    error_document(status, detail)
    return exit_code(status, detail)


def plain_failure(exc: SystemExit) -> int:
    """The JSON-mode exit for a failure that never reached an API
    status: the error document on stderr, then the interrupt's
    130 or the unnamed failure."""
    error_document(None, plain_detail(exc))
    return plain_exit(exc)


def plain_exit(exc: SystemExit) -> int:
    """A failure that is not an API status: the interrupt keeps
    its 130, everything else is the unnamed failure."""
    if exc.code == EXIT_INTERRUPT:
        return EXIT_INTERRUPT
    return EXIT_FAILURE


def plain_detail(exc: SystemExit) -> str:
    """The non-API failure's detail: the message for a
    string-coded exit, the interrupt named for 130."""
    if exc.code == EXIT_INTERRUPT:
        return "interrupted"
    if isinstance(exc.code, str) and exc.code:
        return exc.code
    return f"exit {exc.code}"
