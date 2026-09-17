"""The per-workspace ssh identity (#111): mint, seed, and shape.

msksd mints an identity at create and stores both halves with the
workspace's state; the public half reaches the guest through the
#41 user_data channel — the cidata seed — so a fresh workspace
accepts ssh with no manual key steps anywhere. The private half is
served over the authenticated API to whoever holds a token (a token
holder already owns the root console, so this grants nothing new).

The key type is a setting (#115): ECDSA P-256 is the FIPS-approvable
default, and nothing in the daemon, the client, or the image depends
on which type a workspace carries — the algorithm name travels with
the key material itself. The no-escrow mode (#121) moves the minting
to the client: the daemon receives and stores the public half only,
validated here.
"""

import base64
import binascii
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

#: The identity key types a daemon may mint (#115): the setting names
#: one of these, and the OpenSSH name each maps to is the wire format
#: the public half carries.
KEY_TYPES = {
    "ecdsa": "ecdsa-sha2-nistp256",
    "ed25519": "ssh-ed25519",
    "rsa": "ssh-rsa",
}

#: The RSA bit size — 3072 stays inside every FIPS policy that admits
#: RSA while remaining fast to mint at create.
RSA_BITS = 3072


def normalize_public_key(line: str) -> tuple[str, str]:
    """Validate one supplied public key line: ``(algo, body)``.

    A supplied line may be a key the client minted (#121) or a key
    the operator already owns (#132) — any key type is accepted, at
    whatever type it carries: the guest's sshd, the platform's own
    (#115 posture), stays the authority on which keys it will
    authenticate. The daemon checks shape only — fields, base64
    body, and a blob whose embedded algorithm name agrees with its
    label. The mint paths stay separate and stay limited to the
    FIPS-approvable type set. The caller's comment is dropped: the
    daemon annotates provenance its own way, like the minted mode.
    """
    fields = line.split()
    if len(fields) < 2:
        raise ValueError("public key line needs an algorithm and a key body")
    algo, encoded = fields[0], fields[1]
    check_key_body(algo, encoded)
    return algo, encoded


def check_key_body(algo: str, encoded: str) -> None:
    """The body half of a supplied line: decodes as base64, carries a
    length-prefixed algorithm name that fits the blob and matches
    the label the line gave."""
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"public key body is not valid base64: {exc}") from None
    if len(blob) < 4:
        raise ValueError("public key body is truncated")
    length = int.from_bytes(blob[:4], "big")
    if length + 4 > len(blob):
        raise ValueError("public key body is truncated")
    if blob[4 : 4 + length] != algo.encode():
        raise ValueError("public key algorithm does not match its key body")


def mint(key_type: str) -> tuple[str, str]:
    """A fresh keypair: ``(private_pem, public_openssh)``.

    The private half is OpenSSH-format PEM ("OPENSSH PRIVATE KEY");
    the public half is one authorized_keys line without a comment.
    An unknown key type is a named error — the settings parser
    validates against :data:`KEY_TYPES` at load, so this fires only
    on a directly-constructed Settings carrying a bad value.
    """
    if key_type == "ecdsa":
        private = ec.generate_private_key(ec.SECP256R1())
    elif key_type == "ed25519":
        private = ed25519.Ed25519PrivateKey.generate()
    elif key_type == "rsa":
        private = rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
    else:
        raise ValueError(f"unknown ssh key type {key_type!r}")
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    return private_pem, public


def seed_script(public_key: str) -> str:
    """The seeding payload's script half: authorized_keys for root
    and the msks workspace user (#63), written idempotently.

    The same mkdir/chmod/append shape #110's smoke planted by hand,
    now the daemon's own first-boot step. The msks user's home rides
    the persistent /home volume (#14) — ``install -d`` makes it (and
    its .ssh) with the right ownership when the console helper has
    not yet. A key already present is left alone, so a re-provision
    (a factory reset) cannot duplicate lines.
    """
    return (
        "#!/bin/sh\n"
        "# msks (#111): the minted workspace identity — authorized_keys\n"
        "# for root and the msks workspace user, planted first boot.\n"
        "set -eu\n"
        f"key='{public_key}'\n"
        "install -d -m 0700 -o root -g root /root/.ssh\n"
        "touch /root/.ssh/authorized_keys\n"
        'grep -qxF "$key" /root/.ssh/authorized_keys '
        "|| printf '%s\\n' \"$key\" >> /root/.ssh/authorized_keys\n"
        "chown root:root /root/.ssh/authorized_keys\n"
        "chmod 0600 /root/.ssh/authorized_keys\n"
        "install -d -m 0700 -o msks -g msks /home/msks/.ssh\n"
        "touch /home/msks/.ssh/authorized_keys\n"
        'grep -qxF "$key" /home/msks/.ssh/authorized_keys '
        "|| printf '%s\\n' \"$key\" >> /home/msks/.ssh/authorized_keys\n"
        "chown msks:msks /home/msks/.ssh/authorized_keys\n"
        "chmod 0600 /home/msks/.ssh/authorized_keys\n"
    )


#: The multipart boundary for a seed that carries both the identity
#: script and the operator's own payload: cloud-init's NoCloud
#: datasource splits the user-data document on MIME parts and runs
#: each per its content type.
MIME_BOUNDARY = "============msks-identity=="


def compose_user_data(operator_payload: str | None, public_key: str | None) -> str:
    """The seed's user-data document: what cidata actually carries.

    With no minted key the operator's payload travels verbatim (the
    #41 contract, unchanged); with a key and no payload the seed is
    the identity script alone; with both, a MIME multipart carries
    the script and the payload as sibling parts. The content type of
    the operator part is sniffed from its first line — the two forms
    the #41 contract documents are a ``#!`` script and a
    ``#cloud-config`` document.
    """
    if public_key is None:
        return operator_payload
    script = seed_script(public_key)
    if operator_payload is None:
        return script
    boundary = unique_boundary(operator_payload, script)
    parts = [
        f'Content-Type: multipart/mixed; boundary="{boundary}"',
        "MIME-Version: 1.0",
        "",
        f"--{boundary}",
        'Content-Type: text/x-shellscript; charset="utf-8"',
        "MIME-Version: 1.0",
        "",
        script,
        f"--{boundary}",
        f'Content-Type: {operator_content_type(operator_payload)}; charset="utf-8"',
        "MIME-Version: 1.0",
        "",
        trailing_newline(operator_payload),
        f"--{boundary}--",
        "",
    ]
    return "\n".join(parts)


def unique_boundary(operator_payload: str, script: str) -> str:
    """The multipart boundary, made uncollidable: a payload that
    embeds the standard boundary string gets a random one instead
    (an embedded boundary would split the document mid-payload)."""
    if MIME_BOUNDARY in operator_payload or MIME_BOUNDARY in script:
        return f"==msks-{secrets.token_hex(16)}"
    return MIME_BOUNDARY


#: The first line → MIME type table for the operator part: both
#: cloud-init document forms by their exact label (a whole-line match
#: — "#cloud-config-archive" is a different handler, not a prefix
#: of "#cloud-config"), and a shell script for everything else (the
#: #41 forms are exactly these).
OPERATOR_CONTENT_TYPES = {
    "#cloud-config": "text/cloud-config",
    "#cloud-config-archive": "text/cloud-config-archive",
}


def operator_content_type(payload: str) -> str:
    """The operator part's MIME type, by payload form."""
    first_line = payload.split("\n", 1)[0].strip()
    return OPERATOR_CONTENT_TYPES.get(first_line, "text/x-shellscript")


def trailing_newline(payload: str) -> str:
    """A body that ends without a newline gets one: the closing
    boundary must start on its own line."""
    return payload if payload.endswith("\n") else f"{payload}\n"
