"""The per-workspace ssh identity (#111): mint, seed, and shape.

msksd mints an identity at create and stores both halves with the
workspace's state; the public half reaches the guest through the
#41 user_data channel — the cidata seed — so a fresh workspace
accepts ssh with no manual key steps anywhere. The private half is
served over the authenticated API to whoever holds a token (a token
holder already owns the root console, so this grants nothing new).

The key type is a setting (#115): the default is Ed25519 (#138 —
FIPS 186-5 approves EdDSA), with ECDSA P-256 and RSA as choices,
and nothing in the daemon, the client, or the image depends
on which type a workspace carries — the algorithm name travels with
the key material itself. The no-escrow mode (#121) moves the minting
to the client: the daemon receives and stores the public half only,
validated here.
"""

import base64
import binascii
import re
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


#: The label charset of an algorithm name: OpenSSH's key types are
#: lowercase token shapes (`ssh-rsa`, `ecdsa-sha2-nistp256`,
#: `sk-ssh-ed25519@openssh.com`, `*-cert-v01@openssh.com`) — letters,
#: digits, dash, dot, at. The charset is not an algorithm policy
#: (any *shape-valid* type passes, #132); it is the guard that keeps
#: the interpolated label inside `seed_script`'s single-quoted
#: assignment — a quote or metacharacter in the label would close
#: the string and run as shell code in the guest's first boot.
LABEL_PATTERN = re.compile(r"[a-z0-9@.\-]+")


def normalize_public_key(line: str) -> tuple[str, str]:
    """Validate one supplied public key line: ``(algo, body)``.

    A supplied line may be a key the client minted (#121) or a key
    the operator already owns (#132) — any key type is accepted, at
    whatever type it carries: the guest's sshd, the platform's own
    (#115 posture), stays the authority on which keys it will
    authenticate. The daemon checks shape only — the label matches
    the algorithm-name charset, the body is base64, and the blob's
    embedded algorithm name agrees with its label. The mint paths
    stay separate and stay limited to the FIPS-approvable type set.
    The caller's comment is dropped: the daemon annotates provenance
    its own way, like the minted mode.
    """
    fields = line.split()
    if len(fields) < 2:
        raise ValueError("public key line needs an algorithm and a key body")
    algo, encoded = fields[0], fields[1]
    if LABEL_PATTERN.fullmatch(algo) is None:
        raise ValueError(f"public key algorithm {algo!r} is not a valid name")
    check_key_body(algo, encoded)
    return algo, encoded


def check_key_body(algo: str, encoded: str) -> None:
    """The body half of a supplied line: decodes as base64, carries a
    length-prefixed algorithm name that fits the blob and matches
    the label the line gave."""
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"public key body is not valid base64: {exc}"
        ) from None
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
        private = rsa.generate_private_key(
            public_exponent=65537, key_size=RSA_BITS
        )
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


def seed_script(public_key: str, workspace_id: str) -> str:
    """The seeding payload's script half: authorized_keys for root
    and the msks workspace user (#63) plus the console helper's
    allowed_signers (#123), written idempotently.

    The same mkdir/chmod/append shape #110's smoke planted by hand,
    now the daemon's own first-boot step. The msks user's home rides
    the persistent /home volume (#14): the seed makes it — owned by
    the user, populated from /etc/skel — before the console helper
    ever connects (#171; a bare ``install -d`` of the .ssh path
    would leave the home itself root-owned, because install -d
    applies -o/-g to the final component only). A home that already
    carries dotfiles keeps them (a factory reset preserves the
    user's files), and a key already present is left alone, so a
    re-provision cannot duplicate lines.

    The allowed_signers file is the console challenge's trust store
    (#123): the guest helper's ``ssh-keygen -Y verify`` checks the
    client's signature against it, principal-bound to this
    workspace's id. The daemon relays the challenge and the
    signature; it can answer for neither. ssh and console share the
    key: what authorized_keys accepts, allowed_signers accepts.
    """
    return (
        "#!/bin/sh\n"
        "# msks (#111, #123): the workspace identity — authorized_keys\n"
        "# for root and the msks user, and the console helper's\n"
        "# allowed_signers, planted first boot.\n"
        "set -eu\n"
        f"key='{public_key}'\n"
        f"wsid='{workspace_id}'\n"
        "install -d -m 0700 -o root -g root /root/.ssh\n"
        "touch /root/.ssh/authorized_keys\n"
        'grep -qxF "$key" /root/.ssh/authorized_keys '
        "|| printf '%s\\n' \"$key\" >> /root/.ssh/authorized_keys\n"
        "chown root:root /root/.ssh/authorized_keys\n"
        "chmod 0600 /root/.ssh/authorized_keys\n"
        # The workspace home (#171): install -d both creates the
        # home and repairs one a pre-#171 seed left root-owned (it
        # applies -o/-g to an existing directory too); the skel copy
        # is skipped when the home already has dotfiles, and cp -a
        # preserves the skel's root ownership, so the chown -R that
        # follows is what hands the copy to the user — the same
        # useradd -m shape. The copy is best-effort (set -eu would
        # otherwise abort the whole seed on an image without a
        # skeleton): a bare home still starts the shell, and the
        # chown runs on whatever is there.
        "install -d -m 0755 -o msks -g msks /home/msks\n"
        "if [ ! -e /home/msks/.profile ]; then\n"
        "cp -a /etc/skel/. /home/msks/ || true\n"
        "chown -R msks:msks /home/msks\n"
        "fi\n"
        "install -d -m 0700 -o msks -g msks /home/msks/.ssh\n"
        "touch /home/msks/.ssh/authorized_keys\n"
        'grep -qxF "$key" /home/msks/.ssh/authorized_keys '
        "|| printf '%s\\n' \"$key\" >> /home/msks/.ssh/authorized_keys\n"
        "chown msks:msks /home/msks/.ssh/authorized_keys\n"
        "chmod 0600 /home/msks/.ssh/authorized_keys\n"
        # The signers line: the workspace id principal, then the
        # key's own two fields (an authorized_keys comment is not
        # signers syntax). Splitting with globbing off — the key's
        # charset carries no glob characters.
        "set -f\n"
        "set -- $key\n"
        "install -d -m 0700 -o root -g root /etc/msks\n"
        "signers=/etc/msks/console.allowed_signers\n"
        'touch "$signers"\n'
        'grep -qxF "$wsid $1 $2" "$signers" '
        '|| printf \'%s %s %s\\n\' "$wsid" "$1" "$2" >> "$signers"\n'
        'chown root:root "$signers"\n'
        'chmod 0600 "$signers"\n'
    )


#: The multipart boundary for a seed that carries both the identity
#: script and the operator's own payload: cloud-init's NoCloud
#: datasource splits the user-data document on MIME parts and runs
#: each per its content type.
MIME_BOUNDARY = "============msks-identity=="


def compose_user_data(
    operator_payload: str | None,
    public_key: str | None,
    workspace_id: str = "",
) -> str:
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
    script = seed_script(public_key, workspace_id)
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
        f"Content-Type: {operator_content_type(operator_payload)}; "
        'charset="utf-8"',
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
