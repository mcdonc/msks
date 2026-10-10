"""Test-only keypair minting: the fixture maker #486 removed.

The shipped tree generates no key anywhere (#486 — the operator's
own key is the identity), but the tests still need fresh keypairs
as fixtures: the ssh staging tests, the create tests, the smokes.
This helper keeps the mint's shape (OpenSSH private PEM, one-line
public half, the FIPS-approvable type set #138 carried) on the
test side of the tree.
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

#: The fixture key types: the set the pre-#486 mint carried.
KEY_TYPES = {
    "ecdsa": "ecdsa-sha2-nistp256",
    "ed25519": "ssh-ed25519",
    "rsa": "ssh-rsa",
}

#: The RSA bit size the pre-#486 mint used.
RSA_BITS = 3072


def mint(key_type: str) -> tuple[str, str]:
    """A fresh keypair: ``(private_pem, public_openssh)``.

    The private half is OpenSSH-format PEM ("OPENSSH PRIVATE KEY");
    the public half is one authorized_keys line without a comment.
    An unknown key type is a named error.
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
