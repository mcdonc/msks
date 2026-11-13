"""Phase 4: the verify pass.

The daemon's health answer, the CA chain the client will pin,
and the client preset the verify pass prints.
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.request
from pathlib import Path

from ...client.config import default_config_path
from ..tls import CA_CERT
from .agent import MAC_CA, MAC_TOKEN, MANAGED_ALIAS
from .base import VM_NAME, VM_STATE, MacSetupError, fetch_vm_file, say
from .clientconfig import alias_entry, ensure_client_config, write_private

# --- phase 4: the verify pass and the preset --------------------------


def verify_daemon(port: int, timeout_s: float = 90.0) -> None:
    """The daemon answers through the forward: a TLS connect to
    the listener is enough (any HTTP answer — a 401 included —
    proves the forward, the listener, and the daemon's TLS at
    once). Bounded: a first boot's pip install takes a while."""
    # The probe does not verify: the daemon's CA is self-signed,
    # and the preset step (which fetches the CA) is what arms the
    # client's verification — any TLS answer here proves the
    # stack, whatever the chain says.
    context = ssl._create_unverified_context()
    deadline = time.monotonic() + timeout_s
    last = "no attempt was made"
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"https://127.0.0.1:{port}/", timeout=5, context=context
            )
            return
        except Exception as exc:  # any answer proves the stack
            if getattr(exc, "code", None) is not None:
                return
            last = str(exc)
        time.sleep(2.0)
    raise MacSetupError(
        f"the daemon never answered https://127.0.0.1:{port}/ within "
        f"{timeout_s:.0f}s ({last}); either the VM's journal names the "
        f"cause (limactl shell {VM_NAME} -- sudo journalctl -u msksd -e) "
        "or another listener answers this host port (pass --port)"
    )


def verify_chain(port: int, ca_pem: str) -> None:
    """The listener that answers is THIS daemon: the served
    certificate chains to the VM's CA. A mismatch names the
    collision — something else on the Mac already answers this
    port (another VM's forward, a local daemon), and the remedy
    is another ``--port``. A CA that will not parse (a torn read
    inside the daemon's write window) refuses with the re-run
    remedy, never a raw traceback. A CA that never came back
    (a fetch before the daemon wrote the file) refuses before
    the context is built (#738): empty ``cadata`` silently
    loads the system trust store, and the chain would hold for
    any publicly-trusted listener on the port, not this
    daemon's alone."""
    if not ca_pem.startswith("-----BEGIN CERTIFICATE-----"):
        raise MacSetupError(
            "the daemon's CA did not come back from the VM (the "
            "daemon may still be generating it; re-run)"
        )
    try:
        context = ssl.create_default_context(cadata=ca_pem)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1"):
                return
    except ssl.SSLCertVerificationError as exc:
        # The chain does not hold: the listener answering this
        # port serves another daemon's certificate.
        raise MacSetupError(
            f"something other than the msksd VM's daemon answers "
            f"127.0.0.1:{port} ({exc}); pass --port to use another "
            "host port"
        ) from None
    except ssl.SSLError as exc:
        raise MacSetupError(
            f"the daemon's CA did not parse ({exc}); re-run the tool"
        ) from None
    except OSError as exc:
        raise MacSetupError(
            f"something other than the msksd VM's daemon answers "
            f"127.0.0.1:{port} ({exc}); pass --port to use another "
            "host port"
        ) from None


def client_preset(port: int) -> str:
    """The CA and the token out of the VM, the client's three
    files written (the CA, the token beside it, the config naming
    both), the preset block returned for printing — the exports a
    shell can paste when it prefers them to the config. The CA
    fetch retries briefly: the restart this run's bootstrap just
    issued returns before the daemon's first startup writes the
    file. The chain is proven against the in-memory CA before
    any file lands (#733): a verify refusal leaves the previous
    CA, token, and config untouched, so client runs between the
    refused setup and the re-run keep working."""
    ca = fetch_vm_file(f"{VM_STATE}/{CA_CERT}").strip()
    verify_chain(port, ca)
    MAC_CA.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(MAC_CA, ca + "\n")
    token = fetch_vm_file(f"{VM_STATE}/bootstrap-token").strip()
    write_private(MAC_TOKEN, token + "\n")
    status = ensure_client_config(port)
    say(status)
    exports = (
        f"export MSKSC_URL=https://127.0.0.1:{port}\n"
        f"export MSKSC_TOKEN={token}\n"
        f"export MSKSC_CAFILE={MAC_CA}"
    )
    if status.startswith("kept"):
        # Any existing file the run kept and cannot vouch for
        # (#730): the paste-able half rides beside the exports —
        # the entry block for the config (no top-level key, so a
        # verbatim paste cannot duplicate one and corrupt the
        # file), where the exports are the shell's fallback. A
        # file already carrying the alias updates the block in
        # place; the wording says so, and the last line names
        # what a bare msks reaches until the edit lands.
        return (
            exports
            + "\n\n# update this entry under the daemons: mapping in "
            + str(Path(default_config_path()))
            + "\n# (add it, and the daemons: line, when the file "
            "carries none),\n# then set active_daemon: "
            + MANAGED_ALIAS
            + " there:\n"
            + alias_entry(port)
            + "# a bare msks reaches the daemon once the edit "
            "lands;\n# until then the exports above do\n"
        )
    return exports


# --- phase 5: the client config and the login agent -------------------
