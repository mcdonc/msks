"""The agent reference card (#842): one text, three surfaces.

The card constant is the whole chapter (``docs/agents.md`` carries
it byte for byte) and the ``msks agent-card`` command prints it.
These tests hold all three together: the docs chapter equals the
constant, the command prints it, and every ``msks`` invocation the
card names parses against the real CLI — a card that names a flag
the parser refuses, or stops naming one it takes, fails here.
"""

import itertools
import re
import shlex
from pathlib import Path

import click
import pytest
import typer
from msks.client import agentcard, cli
from test_client_cli import client_env

DOCS_CARD = Path(__file__).parents[3] / "docs" / "agents.md"


class CardBodyReached(Exception):
    """The stubbed body of a card command: parse reached it."""


def card_invocations() -> list[list[str]]:
    """Every ``msks`` line in the card's fenced blocks, tokens
    split. A comment cuts at the token that starts it (a ``#`` in
    the middle of a token is content, not a comment); a
    backslash-continued line is refused here rather than silently
    mis-split."""
    invocations: list[list[str]] = []
    in_fence = False
    for line in agentcard.AGENT_CARD_MD.splitlines():
        assert not line.rstrip().endswith("\\"), (
            "the card grew a continued line; teach the extractor"
        )
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and line.lstrip().startswith("msks "):
            tokens = shlex.split(line)
            kept = list(
                itertools.takewhile(lambda t: not t.startswith("#"), tokens)
            )
            invocations.append(kept)
    return invocations


def test_the_card_names_commands_that_all_parse() -> None:
    """The card's every invocation parses against the CLI: the
    bodies are stubbed (no dial, no bootstrap, no skew probe — the
    root callback is dropped with them), so a command body reached
    means the parse itself succeeded and every flag and choice was
    accepted."""
    command = typer.main.get_command(cli.app)
    command.callback = None

    def reached(*args, **kwargs) -> None:
        raise CardBodyReached

    def stub_leaves(group: click.Group) -> None:
        for leaf in group.commands.values():
            if isinstance(leaf, click.Group):
                stub_leaves(leaf)
            else:
                leaf.callback = reached

    stub_leaves(command)
    assert card_invocations(), "the card names no commands at all"
    for tokens in card_invocations():
        # The card's lines carry the leading ``msks``; click's main
        # takes the argv without it (run_parsed passes sys.argv[1:]).
        try:
            command.main(
                tokens[1:], prog_name="msks", standalone_mode=False, obj=None
            )
        except CardBodyReached:
            continue
        pytest.fail(f"the card names an invocation the CLI refuses: {tokens}")


def test_the_docs_chapter_is_the_card_text() -> None:
    """docs/agents.md and the constant are one text on two
    surfaces; drift between them fails here (an installed package
    has no docs tree, and the card still prints)."""
    if not DOCS_CARD.exists():
        pytest.skip("no docs tree beside the package (installed run)")
    assert DOCS_CARD.read_text() == agentcard.AGENT_CARD_MD


def test_the_agent_card_command_prints_the_card(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.agent_card()
    assert rc == 0
    assert capsys.readouterr().out == agentcard.AGENT_CARD_MD


def test_the_card_teaches_the_session_identity_and_json_only_exits() -> None:
    """The #877 corrections stay on the card: a harness that
    knows only this text must learn that a session needs the
    planted key's private half (the agent or
    ``MSKSC_IDENTITY_FILE``), that the status-mapped codes
    3, 4, and 5 are the ``--json`` exits, and that a bare image
    name resolves where an exact ``name:version`` pair does
    not."""
    card = agentcard.AGENT_CARD_MD
    flat = " ".join(card.split())
    assert "MSKSC_IDENTITY_FILE" in flat
    assert "Rows 3, 4, and 5 hold under `--json`" in flat
    assert "A bare name resolves to the newest version" in flat
    assert "login_user" in flat


def test_card_requested_wants_the_first_positional() -> None:
    """The exemption keys on the first positional token: no
    positional (a bare option scan) or a ``--`` barrier means no
    card, and only ``agent-card`` itself is the card."""
    assert cli.card_requested(["agent-card"]) is True
    assert cli.card_requested([]) is False
    assert cli.card_requested(["--daemon", "url"]) is False
    assert cli.card_requested(["--help"]) is False
    assert cli.card_requested(["--", "agent-card"]) is False
    # An option's value must not read as the subcommand (#842
    # review): the exemption holds behind --config/--daemon, split
    # or attached.
    assert cli.card_requested(["--config", "broken.yaml", "agent-card"])
    assert cli.card_requested(["--daemon", "url", "agent-card"])
    assert cli.card_requested(["--config=broken.yaml", "agent-card"])
    assert not cli.card_requested(["--config", "agent-card", "ls"])


def test_agent_card_prints_without_a_bootstrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The card is reference text read offline (#842 review): the
    root callback skips the config bootstrap for it, and the full
    main() path (parse, root callback, body) prints it whole."""
    client_env(monkeypatch)

    def refused(*args, **kwargs):
        raise AssertionError("the bootstrap ran for agent-card")

    monkeypatch.setattr(cli.wscommands, "bootstrap", refused)
    rc = cli.main(["agent-card"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == agentcard.AGENT_CARD_MD
    assert captured.err == ""


def test_the_card_carries_the_whole_status_vocabulary() -> None:
    """A harness that reads a status the card never taught it was
    lied to by omission: every row status the daemon can hold has
    its meaning on the card."""
    from msks.model.workspaces import WORKSPACE_STATUSES

    for status in WORKSPACE_STATUSES:
        assert re.search(rf"\| `{status}`", agentcard.AGENT_CARD_MD), status


def test_the_card_carries_the_whole_exit_table() -> None:
    """Same honesty for exit codes: every code the client can exit
    with (#841) has its row on the card."""
    from msks.client.jsonio import (
        EXIT_CONFLICT,
        EXIT_FAILURE,
        EXIT_INTERRUPT,
        EXIT_NOT_FOUND,
        EXIT_OK,
        EXIT_USAGE,
        EXIT_WRONG_STATE,
    )

    for code in (
        EXIT_OK,
        EXIT_FAILURE,
        EXIT_USAGE,
        EXIT_NOT_FOUND,
        EXIT_CONFLICT,
        EXIT_WRONG_STATE,
        EXIT_INTERRUPT,
    ):
        assert re.search(rf"^\| {code}\s", agentcard.AGENT_CARD_MD, re.M), code
