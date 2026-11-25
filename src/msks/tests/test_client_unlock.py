"""The client's unlock flow (#515): the core's decrypt plan and
the ``msks secret unlock`` command's wiring, over the mock
transport the CLI tests use.

The happy path mints a real keypair, wraps a value to its public
half exactly the daemon's mint does (``agecrypto.encrypt``), and
asserts the POST the command sends back — possession proven by
the unwrap, one prompt per session for an encrypted key, and the
rows the key cannot open named, not retried.
"""

import base64
from pathlib import Path

import httpx
import pytest
from msks.client import cli
from msks.client.secrets import cmd_secret_unlock
from msks.client.unlock import (
    MISSING_IDENTITY_LINE,
    NOTHING_LOCKED_LINE,
    decrypt_rows,
    key_file_state,
    locked_rows,
    open_key_file,
    unlock_body,
    unlock_flow,
)
from test_client_cli import client_env, identity_env, mock
from testkeys import encrypted, mint

from msks import agecrypto


def pending_row(
    row_id: int,
    name: str,
    blobs: list[tuple[str, bytes]],
    unlocked: bool = False,
) -> dict:
    """One pending reply row (#514): identity, coverage, blobs as
    base64 ciphertext, the daemon's lock state."""
    return {
        "id": row_id,
        "workspaces": [],
        "name": name,
        "dests": ["api.example.com"],
        "created_at": "2026-10-11T00:00:00",
        "expires_at": None,
        "blobs": [
            {"recipient": r, "ciphertext": base64.b64encode(c).decode()}
            for r, c in blobs
        ],
        "staleness": {"missing": [], "stale": []},
        "unlocked": unlocked,
    }


def test_locked_rows_takes_only_cold_rows_with_blobs() -> None:
    """The flow's input set (#515): a row the cache already holds
    is not the unlock's business, and a row with no ciphertext
    has nothing to try — its own recovery is the mint that writes
    blobs again."""
    warm = pending_row(1, "warm", [("r1", b"x")], unlocked=True)
    cold = pending_row(2, "cold", [("r1", b"x")])
    bare = pending_row(3, "bare", [])
    assert [r["name"] for r in locked_rows([warm, cold, bare])] == ["cold"]


def test_decrypt_rows_splits_by_possession() -> None:
    """The core's split (#515): the rows the loaded identity
    opens, and the rows that stay ciphertext — the miss is the
    design. A multi-blob row opens under the first blob the key
    unwraps."""
    mine, my_pub = mint("ed25519")
    other, other_pub = mint("ed25519")
    identity = agecrypto.load_identity(mine.encode(), lambda: "")
    rows = [
        pending_row(1, "mine", [("h1", agecrypto.encrypt(b"v1", my_pub))]),
        pending_row(
            2, "theirs", [("h2", agecrypto.encrypt(b"v2", other_pub))]
        ),
        pending_row(
            3,
            "wide",
            [
                ("h3", agecrypto.encrypt(b"v3", other_pub)),
                ("h4", agecrypto.encrypt(b"v3", my_pub)),
            ],
        ),
    ]
    unlocked, left, corrupt = decrypt_rows(rows, identity)
    assert [(row["name"], value) for row, value in unlocked] == [
        ("mine", "v1"),
        ("wide", "v3"),
    ]
    assert [row["name"] for row in left] == ["theirs"]
    assert corrupt == []
    assert unlock_body(unlocked) == {
        "values": [
            {"placeholder_id": 1, "value": "v1"},
            {"placeholder_id": 3, "value": "v3"},
        ]
    }


def test_unlock_without_an_identity_names_the_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """No identity file configured: the one-line refusal names
    identity_file and its variable, and the command exits
    nonzero."""
    identity_env(monkeypatch, tmp_path)
    client_env(monkeypatch)
    rc = cmd_secret_unlock(
        transport=mock(lambda req: httpx.Response(200, json=[]))
    )
    assert rc == 1
    line = capsys.readouterr().err.strip()
    assert "identity_file" in line
    assert "MSKSC_IDENTITY_FILE" in line


def test_unlock_decrypts_and_uploads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """The happy path (#515): pending serves the blob wrapped to
    the configured key, the command decrypts it and POSTs the
    plaintext, and the reply's rows print unlocked — the daemon's
    refusal lines surface verbatim through the same exchange."""
    pem, pub = mint("ed25519")
    mine = tmp_path / "my-key"
    mine.write_text(pem)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(mine))
    client_env(monkeypatch)
    pending = [
        pending_row(7, "github_api", [("h", agecrypto.encrypt(b"tok", pub))])
    ]
    posts = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=pending)
        posts.append((req.url.path, req.read()))
        return httpx.Response(200, json={"unlocked": [7]})

    rc = cmd_secret_unlock(transport=mock(handler))
    assert rc == 0
    out = capsys.readouterr().out
    assert "unlocked */github_api" in out
    path, body = posts[0]
    assert path == "/api/v1/secrets/unlock"
    assert b'"placeholder_id":7' in body.replace(b" ", b"")
    assert b'"value":"tok"' in body.replace(b" ", b"")


def test_unlock_names_the_rows_the_key_cannot_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Another operator's rows (#515): the key opens nothing, the
    POST never flies, and the line names the rows that stayed
    ciphertext — the daemon stays cold for them."""
    other, other_pub = mint("ed25519")
    mine, _my_pub = mint("ed25519")
    keyfile = tmp_path / "my-key"
    keyfile.write_text(mine)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    client_env(monkeypatch)
    pending = [
        pending_row(9, "pypi", [("h", agecrypto.encrypt(b"v", other_pub))])
    ]
    posts = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=pending)
        posts.append(req)
        return httpx.Response(200, json={"unlocked": []})

    rc = cmd_secret_unlock(transport=mock(handler))
    assert rc == 0  # another operator's rows: a clean run
    assert posts == []
    out = capsys.readouterr().out
    assert "no stanza matches this key" in out
    assert "*/pypi" in out


def test_unlock_prompts_once_for_an_encrypted_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """The acceptance bar (#510): a passphrase-encrypted key file
    unlocks with one prompt per session — the prompt answers once
    and the loaded identity decrypts every row."""
    pem, pub = mint("ed25519")
    from cryptography.hazmat.primitives import serialization

    enc = serialization.load_ssh_private_key(
        pem.encode(), password=None
    ).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
    )
    keyfile = tmp_path / "enc-key"
    keyfile.write_bytes(enc)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    client_env(monkeypatch)
    asked = []

    def fake_prompt(_label):
        asked.append(True)
        return "pw"

    monkeypatch.setattr("getpass.getpass", fake_prompt)
    pending = [
        pending_row(1, "a", [("h1", agecrypto.encrypt(b"v1", pub))]),
        pending_row(2, "b", [("h2", agecrypto.encrypt(b"v2", pub))]),
    ]
    posts = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=pending)
        posts.append(req.read())
        return httpx.Response(200, json={"unlocked": [1, 2]})

    rc = cmd_secret_unlock(transport=mock(handler))
    assert rc == 0
    assert len(asked) == 1
    assert len(posts) == 1
    out = capsys.readouterr().out
    assert "unlocked */a" in out and "unlocked */b" in out


def test_unlock_reports_an_already_warm_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Nothing locked (#515): the command says so and sends no
    plaintext anywhere."""
    pem, _pub = mint("ed25519")
    keyfile = tmp_path / "my-key"
    keyfile.write_text(pem)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    client_env(monkeypatch)
    pending = [
        pending_row(1, "warm", [("h", b"x")], unlocked=True),
    ]
    rc = cmd_secret_unlock(
        transport=mock(lambda req: httpx.Response(200, json=pending))
    )
    assert rc == 0
    assert "every placeholder row is unlocked" in capsys.readouterr().out


def test_unlock_dispatches_through_the_parser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The route: ``msks secret unlock`` through main()."""
    identity_env(monkeypatch, tmp_path)
    client_env(monkeypatch)
    rc = cli.main(["secret", "unlock"])
    assert rc == 1


def test_unlock_names_an_unreadable_identity_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """A configured path that cannot be read (#515): the one
    named refusal with the file's name — the unlock reads the
    private half in place, and a missing file is not a prompt."""
    missing = tmp_path / "gone"
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(missing))
    client_env(monkeypatch)
    locked = [
        {
            "unlocked": False,
            "blobs": [{"recipient": "h", "ciphertext": "eA=="}],
        }
    ]
    with pytest.raises(SystemExit) as refused:
        cmd_secret_unlock(
            transport=mock(lambda req: httpx.Response(200, json=locked))
        )
    assert "cannot read the identity file" in str(refused.value)
    assert str(missing) in str(refused.value)


def test_unlock_reports_mixed_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """One row ours, one theirs (#515): the report prints the
    unlocked line and names the row that stayed ciphertext —
    two operators keyed to different workspaces each unlock
    their own."""
    pem, pub = mint("ed25519")
    other, other_pub = mint("ed25519")
    keyfile = tmp_path / "my-key"
    keyfile.write_text(pem)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    client_env(monkeypatch)
    pending = [
        pending_row(1, "ours", [("h1", agecrypto.encrypt(b"v1", pub))]),
        pending_row(
            2, "theirs", [("h2", agecrypto.encrypt(b"v2", other_pub))]
        ),
    ]
    rc = cmd_secret_unlock(
        transport=mock(
            lambda req: (
                httpx.Response(200, json=pending)
                if req.method == "GET"
                else httpx.Response(200, json={"unlocked": [1]})
            )
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "unlocked */ours" in out
    assert "stayed ciphertext (no stanza matches this key): */theirs" in out


def corrupt_ours(pub: str) -> dict:
    """One pending row whose only blob is addressed to our key
    and broken (#515 review): a flipped payload byte fails the
    STREAM authentication — corruption, not another operator's
    row."""

    from msks import agecrypto

    blob = bytearray(agecrypto.encrypt(b"v", pub))
    blob[-1] ^= 0xFF
    return pending_row(5, "broken", [("h", bytes(blob))])


def test_decrypt_rows_names_corruption_differently() -> None:
    """The two remedies name differently (#515 review): a blob
    addressed to this key that fails its authentication is
    corruption (re-mint), a blob with no stanza for this key is
    another operator's row."""
    mine, my_pub = mint("ed25519")
    other, other_pub = mint("ed25519")
    identity = agecrypto.load_identity(mine.encode(), lambda: "")
    rows = [
        corrupt_ours(my_pub),
        pending_row(6, "theirs", [("h2", agecrypto.encrypt(b"v", other_pub))]),
    ]
    unlocked, left, corrupt = decrypt_rows(rows, identity)
    assert unlocked == []
    assert [row["name"] for row in corrupt] == ["broken"]
    assert [row["name"] for row in left] == ["theirs"]


def test_a_row_of_undecodable_base64_survives_the_flow() -> None:
    """One bad blob never kills the run (#515 review): a blob
    that is not base64 skips (the row reads as not ours), a blob
    that is not an age file reads as corruption, and the rest of
    the session proceeds."""
    mine, my_pub = mint("ed25519")
    identity = agecrypto.load_identity(mine.encode(), lambda: "")
    rows = [
        pending_row(1, "fine", [("h", agecrypto.encrypt(b"v1", my_pub))]),
        pending_row(2, "garbage", [("h", b"!!not an age file!!")]),
    ]
    unlocked, left, corrupt = decrypt_rows(rows, identity)
    assert [(row["name"], value) for row, value in unlocked] == [
        ("fine", "v1")
    ]
    assert [row["name"] for row in corrupt] == ["garbage"]
    assert left == []
    undecodable = dict(
        pending_row(1, "fine", [("h", agecrypto.encrypt(b"v1", my_pub))]),
        id=3,
        name="b64",
        blobs=[{"recipient": "h", "ciphertext": "!!!not base64!!!"}],
    )
    unlocked, left, corrupt = decrypt_rows([undecodable], identity)
    assert unlocked == [] and corrupt == []
    assert [row["name"] for row in left] == ["b64"]


def test_unlock_names_a_key_the_crypto_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """The load's own refusals (#515 review): a file that is no
    key, and a wrong passphrase, answer one named line and a
    nonzero exit — never a traceback."""
    junk = tmp_path / "junk"
    junk.write_text("not a key at all")
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(junk))
    client_env(monkeypatch)
    pending = [
        {
            "unlocked": False,
            "blobs": [{"recipient": "h", "ciphertext": "eA=="}],
        }
    ]
    rc = cmd_secret_unlock(
        transport=mock(lambda req: httpx.Response(200, json=pending))
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "refused" in err
    assert "not a private key file" in err


def test_unlock_names_a_wrong_passphrase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """The encrypted-key miss (#515 review): the named line, no
    traceback."""
    from cryptography.hazmat.primitives import serialization

    pem, pub = mint("ed25519")
    enc = serialization.load_ssh_private_key(
        pem.encode(), password=None
    ).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
    )
    keyfile = tmp_path / "enc-key"
    keyfile.write_bytes(enc)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    client_env(monkeypatch)
    monkeypatch.setattr("getpass.getpass", lambda _l: "wrong")
    pending = [pending_row(1, "a", [("h", agecrypto.encrypt(b"v1", pub))])]
    rc = cmd_secret_unlock(
        transport=mock(lambda req: httpx.Response(200, json=pending))
    )
    assert rc == 1
    assert "passphrase" in capsys.readouterr().err


def test_a_warm_store_never_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """The fetch precedes the load (#515 review): nothing locked
    means no passphrase prompt at all."""
    pem, pub = mint("ed25519")
    from cryptography.hazmat.primitives import serialization

    enc = serialization.load_ssh_private_key(
        pem.encode(), password=None
    ).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
    )
    keyfile = tmp_path / "enc-key"
    keyfile.write_bytes(enc)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    client_env(monkeypatch)
    asked = []
    monkeypatch.setattr(
        "getpass.getpass", lambda _l: asked.append(True) or "pw"
    )
    pending = [pending_row(1, "warm", [("h", b"x")], unlocked=True)]
    rc = cmd_secret_unlock(
        transport=mock(lambda req: httpx.Response(200, json=pending))
    )
    assert rc == 0
    assert asked == []
    assert "every placeholder row is unlocked" in capsys.readouterr().out


def test_the_ls_state_cell_reads_the_flag() -> None:
    """The listing's new cell (#515): locked reads locked, an
    unlocked row open."""
    from msks.client.secrets import secret_cells

    def row(**extra):
        base = {
            "id": 1,
            "workspaces": [],
            "name": "github_api",
            "dests": [],
            "expires_at": None,
        }
        base.update(extra)
        return base

    assert secret_cells(row(locked=True))[-1] == "locked"
    assert secret_cells(row())[-1] == "open"


def test_unlock_names_a_corrupt_blob_for_the_re_mint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """The corruption remedy (#515 review): a blob addressed to
    the configured key that fails its authentication prints the
    re-mint line, distinct from another operator's rows."""
    pem, pub = mint("ed25519")
    keyfile = tmp_path / "my-key"
    keyfile.write_text(pem)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    client_env(monkeypatch)
    from msks import agecrypto

    blob = bytearray(agecrypto.encrypt(b"v", pub))
    blob[-1] ^= 0xFF
    pending = [pending_row(4, "broken", [("h", bytes(blob))])]
    rc = cmd_secret_unlock(
        transport=mock(
            lambda req: (
                httpx.Response(200, json=pending)
                if req.method == "GET"
                else httpx.Response(200, json={"unlocked": []})
            )
        )
    )
    assert rc == 1  # nothing opened and the row broke: incomplete
    out = capsys.readouterr().out
    assert "ciphertext failed to open (re-mint): */broken" in out


# --- the shared flow's halves (#1147) -----------------------------------


class FlowData:
    """The flow's data seam (#515, #1147): the pending rows it
    serves and the uploads it takes — no transport, the halves'
    own pins."""

    def __init__(self, pending: list[dict]) -> None:
        self.pending = pending
        self.uploads: list[dict] = []

    async def pending_secrets(self) -> list[dict]:
        return self.pending

    async def unlock_secrets(self, values: list[dict]) -> dict:
        self.uploads.append({"values": values})
        return {"unlocked": [value["placeholder_id"] for value in values]}


async def never_ask(path) -> str:
    """The prompt seam the quiet cases must never reach."""
    raise AssertionError(f"the flow prompted for {path}")


def locked_key_file(tmp_path: Path, pem: str, name: str = "enc-key") -> Path:
    """A passphrase-encrypted key file under the test's tree."""
    keyfile = tmp_path / name
    keyfile.write_text(encrypted(pem))
    return keyfile


async def test_the_flow_ends_quietly_without_an_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first quiet case (#515, #1147): no identity file
    configured ends the flow before the daemon is asked — nothing
    to say for the automatic flow, the setting's line for an
    operator's by-hand ask."""
    identity_env(monkeypatch, tmp_path)
    data = FlowData([pending_row(1, "a", [("h", b"x")])])
    assert await unlock_flow(data, never_ask) is None
    assert (
        await unlock_flow(data, never_ask, explicit=True)
        == MISSING_IDENTITY_LINE
    )
    assert "identity_file" in MISSING_IDENTITY_LINE


async def test_a_warm_store_ends_the_flow_before_the_key_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fetch precedes the load (#515 review): a daemon
    holding every row's plaintext ends the flow without reading
    the key file — an unreadable file configured, nothing is
    read and nothing prompted, and the by-hand ask names the
    warm store."""
    missing = tmp_path / "no-such-key"
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(missing))
    data = FlowData([pending_row(1, "warm", [("h", b"x")], unlocked=True)])
    asked: list = []

    async def ask(path) -> str:
        asked.append(path)
        return "pw"

    assert await unlock_flow(data, ask) is None
    assert await unlock_flow(data, ask, explicit=True) == NOTHING_LOCKED_LINE
    assert asked == []


async def test_the_flow_prompts_once_for_an_encrypted_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shared flow end to end (#515, #1147): the fetch, one
    prompt through the caller's seam for an encrypted file, the
    upload of what the loaded key opened, and the outline."""
    pem, pub = mint("ed25519")
    keyfile = locked_key_file(tmp_path, pem)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    data = FlowData(
        [
            pending_row(1, "a", [("h1", agecrypto.encrypt(b"v1", pub))]),
            pending_row(2, "b", [("h2", agecrypto.encrypt(b"v2", pub))]),
        ]
    )
    asked: list = []

    async def ask(path) -> str:
        asked.append(path)
        return "pw"

    assert await unlock_flow(data, ask) == "unlocked 2 placeholder rows"
    assert asked == [keyfile]
    assert data.uploads == [
        {
            "values": [
                {"placeholder_id": 1, "value": "v1"},
                {"placeholder_id": 2, "value": "v2"},
            ]
        }
    ]


async def test_a_dismissed_prompt_leaves_the_rows_named_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The empty prompt answer (#515, #920): the cancel line names
    the passphrase the file needs, the spent line replaces it
    when the caller knows the prompt was spent, and nothing is
    uploaded either way."""
    pem, _pub = mint("ed25519")
    keyfile = locked_key_file(tmp_path, pem)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    data = FlowData([pending_row(1, "a", [("h1", b"x")])])

    async def ask(path) -> str:
        return ""

    assert await unlock_flow(data, ask) == (
        "secrets stay locked — the unlock needs the key file's "
        "passphrase (enc-key)"
    )
    assert await unlock_flow(data, ask, spent_line="spent line") == (
        "spent line"
    )
    assert data.uploads == []


async def test_the_flow_names_an_unreadable_key_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The load's own refusals reach the flow as one line (#515
    review): a file that cannot be read ends it before any
    prompt, the file's name and the setting in the line."""
    missing = tmp_path / "no-such-key"
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(missing))
    data = FlowData([pending_row(1, "a", [("h", b"x")])])
    line = await unlock_flow(data, never_ask)
    assert "cannot read the identity file" in line
    assert "no-such-key" in line


async def test_the_flow_names_a_wrong_passphrase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The encrypted-key miss (#515 review): the crypto's named
    refusal, one line, nothing uploaded."""
    pem, pub = mint("ed25519")
    keyfile = locked_key_file(tmp_path, pem)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    data = FlowData(
        [pending_row(1, "a", [("h", agecrypto.encrypt(b"v", pub))])]
    )

    async def ask(path) -> str:
        return "wrong"

    line = await unlock_flow(data, ask)
    assert line.startswith("unlock refused:")
    assert "passphrase" in line
    assert data.uploads == []


async def test_the_flow_names_the_rows_the_key_opens_none_of(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Another operator's rows (#515): the key opens nothing, the
    upload never flies, and the line counts the rows that stay
    locked."""
    _other, other_pub = mint("ed25519")
    mine, _my_pub = mint("ed25519")
    keyfile = tmp_path / "my-key"
    keyfile.write_text(mine)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    data = FlowData(
        [pending_row(9, "pypi", [("h", agecrypto.encrypt(b"v", other_pub))])]
    )
    assert await unlock_flow(data, never_ask) == (
        "1 placeholder row stay locked — the key opens none of them"
    )
    assert data.uploads == []


async def test_the_outline_names_corruption_beside_the_opened_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The three outcomes on one line (#515 review): the rows that
    opened upload, the blob addressed to this key that failed its
    authentication is named for its re-mint, and another
    operator's row stays ciphertext."""
    _other, other_pub = mint("ed25519")
    mine, my_pub = mint("ed25519")
    keyfile = tmp_path / "my-key"
    keyfile.write_text(mine)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(keyfile))
    blob = bytearray(agecrypto.encrypt(b"v", my_pub))
    blob[-1] ^= 0xFF
    data = FlowData(
        [
            pending_row(1, "good", [("h", agecrypto.encrypt(b"tok", my_pub))]),
            pending_row(2, "broken", [("h", bytes(blob))]),
            pending_row(
                3, "theirs", [("h", agecrypto.encrypt(b"v", other_pub))]
            ),
        ]
    )
    assert await unlock_flow(data, never_ask) == (
        "unlocked 1 placeholder row; 1 failed to open (re-mint); "
        "1 stay ciphertext (no stanza matches this key)"
    )
    assert data.uploads == [
        {"values": [{"placeholder_id": 1, "value": "tok"}]}
    ]

    # A corrupt blob alone keeps every row locked and names the
    # re-mint for it.
    data = FlowData([pending_row(2, "broken", [("h", bytes(blob))])])
    assert await unlock_flow(data, never_ask) == (
        "1 placeholder row stay locked — the key opens none of them "
        "(1 failed to open; re-mint them)"
    )
    assert data.uploads == []


def test_the_key_file_state_reads_the_passphrase_need(tmp_path: Path) -> None:
    """The GUI's prompt decision (#1147): a plain key file needs
    no passphrase, an encrypted one does, and a file that cannot
    be read answers the refusal in the flag's place."""
    pem, _pub = mint("ed25519")
    plain = tmp_path / "plain"
    plain.write_text(pem)
    assert key_file_state(plain) == (False, None)
    assert key_file_state(locked_key_file(tmp_path, pem)) == (True, None)
    missing = tmp_path / "missing"
    flag, line = key_file_state(missing)
    assert flag is False
    assert "cannot read the identity file" in line


def test_the_key_file_opens_under_the_answer(tmp_path: Path) -> None:
    """The GUI's load (#1147): the passphrase opens the encrypted
    file into the identity that unwraps the daemon's blobs, and
    the three refusals — a wrong passphrase, a file that is no
    key, a file that cannot be read — each answer one line."""
    pem, pub = mint("ed25519")
    keyfile = locked_key_file(tmp_path, pem)
    identity, refused = open_key_file(keyfile, "pw")
    assert refused is None
    row = pending_row(7, "a", [("h", agecrypto.encrypt(b"tok", pub))])
    assert decrypt_rows([row], identity)[0] == [(row, "tok")]

    identity, refused = open_key_file(keyfile, "wrong")
    assert identity is None
    assert refused.startswith("unlock refused:")

    junk = tmp_path / "junk"
    junk.write_text("not a key at all")
    identity, refused = open_key_file(junk, "")
    assert identity is None
    assert refused.startswith("unlock refused:")

    identity, refused = open_key_file(tmp_path / "missing", "")
    assert identity is None
    assert "cannot read the identity file" in refused
