"""The minted workspace identity (#111): mint, seed script, compose."""

import base64
import os
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from msks.identity import (
    KEY_TYPES,
    MIME_BOUNDARY,
    compose_user_data,
    mint,
    operator_content_type,
    seed_script,
    trailing_newline,
    unique_boundary,
)

PUBLIC = "ecdsa-sha2-nistp256 AAAAE2VjZHNh user@host"


def minted_public(private_pem: str) -> str:
    """The public line a private half derives to, for round-trip
    checks that the two halves mint() returns are one keypair."""
    key = serialization.load_ssh_private_key(
        private_pem.encode(), password=b""
    )
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )


def test_mint_each_supported_type() -> None:
    """Every KEY_TYPES entry mints to its own OpenSSH name."""
    for key_type, ssh_name in KEY_TYPES.items():
        private_pem, public = mint(key_type)
        assert public.startswith(f"{ssh_name} ")
        assert minted_public(private_pem).split()[:2] == public.split()[:2]


def test_mint_rejects_unknown_type() -> None:
    try:
        mint("bogus")
    except ValueError as exc:
        assert "unknown ssh key type" in str(exc)
    else:
        raise AssertionError("mint accepted an unknown type")


def test_seed_script_plants_both_users_and_the_trust_store() -> None:
    """The script targets root and the msks user with the
    mkdir/chmod/append shape, and a key already present is not
    duplicated."""
    script = seed_script(PUBLIC, "ws-id")
    assert script.startswith("#!/bin/sh\n")
    assert f"key='{PUBLIC}'" in script
    assert "wsid='ws-id'" in script
    assert script.count("grep -qxF") == 3
    assert "/root/.ssh/authorized_keys" in script
    assert "/home/msks/.ssh/authorized_keys" in script
    assert "chown msks:msks" in script
    # The home comes from the seed too (#171): created owned by the
    # user, populated from skel once (the guard), .ssh private.
    assert "install -d -m 0755 -o msks -g msks /home/msks" in script
    assert "cp -a /etc/skel/. /home/msks/" in script
    assert "chown -R msks:msks /home/msks" in script
    assert "install -d -m 0700 -o msks -g msks /home/msks/.ssh" in script
    # Idempotent shape: the append only runs when grep misses.
    assert script.count("|| printf") == 3
    assert script.count(">> /root/.ssh/authorized_keys") == 1
    assert script.count(">> /home/msks/.ssh/authorized_keys") == 1
    # The console challenge's trust store (#123): the workspace id
    # principal, the key's own two fields (the authorized_keys
    # comment is not signers syntax), root-owned and private.
    assert "/etc/msks/console.allowed_signers" in script
    assert '"$wsid" "$1" "$2" >> "$signers"' in script
    assert 'chmod 0600 "$signers"' in script
    # No login user named: the shipped accounts are the whole
    # provisioning, and no useradd runs (#248).
    assert "useradd" not in script


def test_seed_script_provisions_a_named_login_user() -> None:
    """A login user the image does not ship (#248) is created at
    first boot: the account (only when missing), the #171 home
    shape, its authorized_keys, and the #169 passwordless-sudo
    grant — the same posture the msks account carries."""
    script = seed_script(PUBLIC, "ws-id", "alice")
    assert "luser='alice'" in script
    # The account is made only when passwd names it — a
    # re-provision or an operator-premade account keeps its uid.
    assert 'luid=$(getent passwd "$luser" 2>/dev/null | cut -d: -f3)' in script
    assert 'if [ -z "$luid" ]; then' in script
    assert 'useradd -m -s /bin/bash "$luser"' in script
    # The home and key, in the same shape the msks user gets;
    # ownership rides the chown colon form, which works whatever
    # the account's primary group is named.
    assert 'install -d -m 0755 "/home/$luser"' in script
    assert 'cp -a /etc/skel/. "/home/$luser/" || true' in script
    assert 'install -d -m 0700 "/home/$luser/.ssh"' in script
    assert script.count('>> "/home/$luser/.ssh/authorized_keys"') == 1
    assert 'chown -R "$luser:" "/home/$luser"' in script
    assert 'chmod 0600 "/home/$luser/.ssh/authorized_keys"' in script
    # The sudo grant, root-owned at the sudoers mode.
    assert "printf '%s ALL=(ALL) NOPASSWD:ALL\\n' \"$luser\" " in script
    assert 'chmod 0440 "/etc/sudoers.d/$luser"' in script
    # The root, msks, and signers lines ride along unchanged.
    assert script.count("grep -qxF") == 4
    assert 'chmod 0600 "$signers"' in script


def test_seed_script_skips_a_system_account_name() -> None:
    """A name that lands on a system account the image ships
    (Debian's base-passwd carries charset-valid names like
    ``sync``) seeds nothing: the console helper refuses those
    accounts on its own rule, and a sudoers grant against one
    would be a privilege write with no login behind it — the block
    says so on stderr, the provisioning sits behind a guard, and
    the rest of the script (the signers store included) still
    runs."""
    script = seed_script(PUBLIC, "ws-id", "sync")
    assert 'elif [ "$luid" -lt 1000 ] && [ "$luid" -ne 0 ]; then' in script
    assert "names a system account" in script
    assert "seed_user=no" in script
    assert 'if [ "$seed_user" = yes ]; then' in script
    guarded = script.split('if [ "$seed_user" = yes ]; then')[1]
    assert "sudoers.d" in guarded
    # The signers block stays outside the guard — a skipped login
    # user never costs the console challenge its trust store.
    assert script.index('chmod 0600 "$signers"') > script.index(
        'if [ "$seed_user" = yes ]; then'
    )


def sandboxed(script: str, sandbox) -> str:
    """The seed with every filesystem path pointed into
    ``sandbox`` — the logic (the guard above all) runs unchanged."""
    return (
        script.replace("/home/", f"{sandbox}/home/")
        .replace("/root", f"{sandbox}/root")
        .replace("/etc/sudoers.d", f"{sandbox}/sudoers.d")
        .replace("/etc/msks", f"{sandbox}/msks")
        .replace("/etc/skel", f"{sandbox}/skel")
    )


def run_seed(sandbox, guest_passwd_line: str):
    """Execute the sandboxed seed under stubbed guest tools.

    ``getent`` answers the passwd line the scenario wants (empty
    for a name the guest does not know); ``useradd`` and ``chown``
    log instead of touching the host's accounts; ``install``
    mkdirs its final argument, ignoring the ownership flags only
    root could apply. Everything else — the shell, cut, grep,
    printf, cp, touch, chmod — runs for real against the sandbox.
    """
    stubs = sandbox / "bin"
    stubs.mkdir(exist_ok=True)
    log = sandbox / "stub.log"
    stubs.joinpath("getent").write_text(
        "#!/bin/sh\n"
        '[ -n "$GUEST_PASSWD_LINE" ] && echo "$GUEST_PASSWD_LINE" '
        "|| exit 2\n"
    )
    for name in ("useradd", "chown"):
        stubs.joinpath(name).write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{log}"\n'
        )
    stubs.joinpath("install").write_text(
        "#!/bin/sh\n"
        f'echo "install $*" >> "{log}"\n'
        "for dir; do :; done\n"
        'exec mkdir -p "$dir"\n'
    )
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "GUEST_PASSWD_LINE": guest_passwd_line,
    }
    return subprocess.run(
        [
            "sh",
            "-c",
            sandboxed(seed_script(PUBLIC, "ws-id", "alice"), sandbox),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def prepare_sandbox(tmp_path):
    """The sandbox tree the rewritten paths land in."""
    for member in ("root", "sudoers.d", "msks", "skel"):
        (tmp_path / member).mkdir()
    (tmp_path / "skel" / ".profile").write_text("# skel\n")
    return tmp_path


def test_seed_script_executes_provisioning(tmp_path) -> None:
    """The guard's executable pin, missing-account shape: useradd
    runs, the key lands in the named user's authorized_keys at mode
    0600, and the sudoers entry lands at 0440 — the whole block
    exits clean under ``set -eu``."""
    sandbox = prepare_sandbox(tmp_path)
    done = run_seed(sandbox, guest_passwd_line="")
    assert done.returncode == 0, done.stderr
    assert "useradd -m -s /bin/bash alice" in (
        sandbox.joinpath("stub.log").read_text()
    )
    keys = sandbox / "home" / "alice" / ".ssh" / "authorized_keys"
    assert keys.read_text().strip() == PUBLIC
    assert keys.stat().st_mode & 0o777 == 0o600
    sudoers = sandbox / "sudoers.d" / "alice"
    assert sudoers.read_text() == "alice ALL=(ALL) NOPASSWD:ALL\n"
    assert sudoers.stat().st_mode & 0o777 == 0o440


def test_seed_script_executes_the_skip_for_a_system_uid(tmp_path) -> None:
    """The guard's executable pin, system-account shape: a passwd
    entry under uid 1000 seeds nothing for the name (no account
    write, no sudoers grant) while the rest of the script — root's
    keys, the msks home, the signers store — still lands."""
    sandbox = prepare_sandbox(tmp_path)
    done = run_seed(
        sandbox,
        guest_passwd_line="sync:x:15:15:sync:/home/sync:/usr/sbin/nologin",
    )
    assert done.returncode == 0, done.stderr
    assert "names a system account" in done.stderr
    log = sandbox.joinpath("stub.log").read_text()
    assert "useradd" not in log
    assert not (sandbox / "home" / "sync").exists()
    assert not (sandbox / "sudoers.d" / "sync").exists()
    # The rest of the seed still ran.
    assert (sandbox / "root" / ".ssh" / "authorized_keys").read_text()
    assert (sandbox / "home" / "msks" / ".ssh" / "authorized_keys").exists()
    assert (sandbox / "msks" / "console.allowed_signers").exists()


def test_seed_script_executes_the_keep_for_a_regular_uid(tmp_path) -> None:
    """The guard's executable pin, existing-regular-account shape:
    uid 1000 keeps its account (no useradd) and takes the
    provisioning."""
    sandbox = prepare_sandbox(tmp_path)
    done = run_seed(
        sandbox,
        guest_passwd_line="alice:x:1000:1000:Alice:/home/alice:/bin/bash",
    )
    assert done.returncode == 0, done.stderr
    assert "useradd" not in sandbox.joinpath("stub.log").read_text()
    assert (sandbox / "sudoers.d" / "alice").exists()
    assert (
        sandbox / "home" / "alice" / ".ssh" / "authorized_keys"
    ).read_text().strip() == PUBLIC


def test_seed_script_skips_provisioning_for_shipped_users() -> None:
    """The image already ships root and the msks account: naming
    either as the login user records it on the row and seeds
    nothing new — no useradd, no second sudoers entry."""
    for shipped in ("root", "msks"):
        script = seed_script(PUBLIC, "ws-id", shipped)
        assert "useradd" not in script
        assert "sudoers.d" not in script
        assert script == seed_script(PUBLIC, "ws-id")


def test_compose_without_key_is_verbatim() -> None:
    """No minted identity: the operator payload travels unchanged
    (the #41 contract)."""
    payload = "#!/bin/sh\necho hi\n"
    assert compose_user_data(payload, None) == payload


def test_compose_without_payload_is_the_script() -> None:
    """A minted key and no operator payload: the seed is the script
    alone, one plain document."""
    assert compose_user_data(None, PUBLIC, "ws-id") == seed_script(
        PUBLIC, "ws-id"
    )


def test_compose_carries_the_login_user_into_the_script() -> None:
    """The login user rides the composed document the same way it
    rides the bare script (#248): the seed the guest runs is the
    one for THIS workspace's user."""
    assert compose_user_data(None, PUBLIC, "ws-id", "alice") == seed_script(
        PUBLIC, "ws-id", "alice"
    )


def test_compose_merges_script_and_script_payload() -> None:
    """Both halves present: MIME multipart, script first, the
    operator's shell payload second with its sniffed type."""
    payload = "#!/bin/sh\necho operator\n"
    composed = compose_user_data(payload, PUBLIC, "ws-id")
    assert composed.startswith(
        f'Content-Type: multipart/mixed; boundary="{MIME_BOUNDARY}"'
    )
    assert 'Content-Type: text/x-shellscript; charset="utf-8"' in composed
    assert 'Content-Type: text/cloud-config; charset="utf-8"' not in composed
    assert seed_script(PUBLIC, "ws-id") in composed
    assert payload in composed
    assert composed.endswith(f"--{MIME_BOUNDARY}--\n")


def test_compose_merges_cloud_config_payload() -> None:
    """A #cloud-config operator payload rides as text/cloud-config."""
    payload = "#cloud-config\npackages: []\n"
    composed = compose_user_data(payload, PUBLIC, "ws-id")
    assert 'Content-Type: text/cloud-config; charset="utf-8"' in composed


def test_compose_appends_missing_trailing_newline() -> None:
    """A payload without a final newline gets one: the closing
    boundary must start on its own line."""
    payload = "#!/bin/sh\necho operator"
    composed = compose_user_data(payload, PUBLIC, "ws-id")
    assert f"\n--{MIME_BOUNDARY}--" in composed


def test_unique_boundary_falls_back_when_embedded() -> None:
    """A payload embedding the boundary string forces a random one;
    ordinary payloads keep the documented boundary."""
    hostile = f"echo {MIME_BOUNDARY}\n"
    assert MIME_BOUNDARY not in unique_boundary(
        hostile, seed_script(PUBLIC, "ws-id")
    )
    assert MIME_BOUNDARY in unique_boundary(
        "echo hi\n", seed_script(PUBLIC, "ws-id")
    )


def test_operator_content_type_by_first_line() -> None:
    assert operator_content_type("#cloud-config\n") == "text/cloud-config"
    assert operator_content_type("#!/bin/sh\n") == "text/x-shellscript"
    # Anything without a recognized first line is a shell script —
    # the #41 forms are exactly these.
    assert operator_content_type("echo bare") == "text/x-shellscript"
    # Whole-line match: cloud-config-archive is its own cloud-init
    # handler, not a prefix of cloud-config.
    assert (
        operator_content_type("#cloud-config-archive\n")
        == "text/cloud-config-archive"
    )
    assert operator_content_type("#cloud-config-with-suffix\n") == (
        "text/x-shellscript"
    )


def test_trailing_newline() -> None:
    assert trailing_newline("x\n") == "x\n"
    assert trailing_newline("x") == "x\n"


def test_normalize_public_key_round_trips_every_minted_type() -> None:
    """A supplied line validates for each type the daemon itself
    mints (#121), and the comment is dropped — the daemon annotates
    provenance its own way."""
    from msks.identity import normalize_public_key

    for key_type in KEY_TYPES:
        _private, public = mint(key_type)
        algo, body = normalize_public_key(f"{public} operator@laptop")
        expected = public.split()
        assert (algo, body) == (expected[0], expected[1])
        # The minted line's own form (no comment) validates identically.
        assert normalize_public_key(public) == (algo, body)


def test_normalize_public_key_accepts_any_supplied_type() -> None:
    """A supplied line passes shape validation at any key type
    (#132): the types the daemon mints, a non-default RSA size,
    another ECDSA curve, and hardware/certificate labels — the
    guest's sshd stays the authority on what it authenticates."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from msks.identity import normalize_public_key

    lines = [mint(key_type)[1] for key_type in KEY_TYPES]
    lines.append(
        rsa.generate_private_key(public_exponent=65537, key_size=4096)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    lines.append(
        ec.generate_private_key(ec.SECP384R1())
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    # A hardware-key label the daemon cannot mint: structurally the
    # same wire shape, synthesized (embedded name matches label).
    sk = "sk-ssh-ed25519@openssh.com"
    lines.append(
        f"{sk} "
        + base64.b64encode(
            len(sk).to_bytes(4, "big") + sk.encode() + b"rest"
        ).decode()
    )
    for line in lines:
        algo, body = normalize_public_key(f"{line} operator@laptop")
        assert (algo, body) == (line.split()[0], line.split()[1])


def test_normalize_public_key_rejects_a_shell_crafted_label() -> None:
    """A label carrying shell metacharacters cannot ride the
    annotation into the seed script's single-quoted assignment
    (#132): the blob may embed the crafted label consistently — the
    charset check is what refuses it, not the shape."""
    from msks.identity import normalize_public_key

    for label in (
        "x';poweroff;'",
        'x" && rm -rf / && "',
        "a;b",
        "x$HOME",
        "a%b",
        "SSH-RSA",
        "café",
    ):
        blob = base64.b64encode(
            len(label).to_bytes(4, "big") + label.encode() + b"rest"
        ).decode()
        with pytest.raises(ValueError, match="not a valid name"):
            normalize_public_key(f"{label} {blob}")


def test_normalize_public_key_rejects_malformed_lines() -> None:
    """Each way a public line can lie: no body, a body that is not
    base64, a truncated blob, and a label that disagrees with the
    body it carries — at any type, the label/body agreement is the
    one check that pins the line to its own material."""
    from msks.identity import normalize_public_key

    _private, ecdsa = mint("ecdsa")
    _private, ed25519 = mint("ed25519")
    # A blob whose embedded length runs past its own end: decodes
    # fine, claims more than it carries.
    oversized = base64.b64encode(b"\x00\x00\x00\x10AB").decode()
    dss = "ssh-dss"
    dss_mislabeled = base64.b64encode(
        len(dss).to_bytes(4, "big") + dss.encode() + b"rest"
    ).decode()
    bad_lines = [
        "lonely-label",
        "ecdsa-sha2-nistp256 !!not-base64!!",
        "ecdsa-sha2-nistp256 QUJD",
        f"ecdsa-sha2-nistp256 {oversized}",
        f"ecdsa-sha2-nistp256 {ed25519.split()[1]}",
        f"ssh-rsa {dss_mislabeled}",
    ]
    details = [
        "needs an algorithm",
        "not valid base64",
        "truncated",
        "truncated",
        "does not match its key body",
        "does not match its key body",
    ]
    for line, detail in zip(bad_lines, details, strict=True):
        with pytest.raises(ValueError, match=detail):
            normalize_public_key(line)
