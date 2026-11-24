"""The loopback bridge (#1102): the ssh session the daemon holds
into a guest, so forward proxy rows can reach the guest's own
``127.0.0.1``.

The session is an OpenSSH control master (``-M`` with
``ControlPersist``) the daemon spawns at establish time,
connecting over the tap to the guest's sshd. Authentication uses
the operator's key through an agent relay: the master's
``IdentityAgent`` points at a unix socket the relay websocket
(``server.api.bridge``) serves, and the client on the other end
answers signature requests from its own agent. The private half
stays on the client machine; the daemon stores no key material.

Each accepted proxy connection opens a ``-W`` channel on the
master — no new authentication, so the bridge keeps serving after
the client that established it is gone. A dropped session, a
guest sshd restart, or a daemon crash that leaves the VM running
ends the bridge (the master's keepalives notice and it removes
its control socket); the next client visit re-establishes it, and
the proxy row records the cause until then.

A root leaf module like the proxy subsystem: the composition
layer sets it on ``app.state``, and the proxy subsystem and the
API routes reach it by attribute.
"""

import asyncio
import contextlib
from pathlib import Path


#: The bridge's own refusal: the named cause a proxy row records
#: (a socket path the kernel cannot address, a workspace not
#: running, no session standing). A local class on purpose — the
#: spec failure leaf sits at its import fan-in ceiling.
class BridgeError(Exception):
    """A bridge refusal a row or a close reason records."""


#: The ssh binary the bridge drives. The client commands shell
#: out to ssh the same way; the daemon host runs the same
#: contract.
SSH = "ssh"

#: Keepalives: the master notices a dead guest (or a restarted
#: sshd) within this many seconds, exits, and removes its control
#: socket — the fact the up-check reads.
ALIVE_INTERVAL_S = 15
ALIVE_COUNT = 3

#: How long an establish waits for the master to authenticate:
#: the client's agent answers in well under a second, so a stall
#: here is a client that stopped serving, and the bridge refuses
#: with the timeout as its cause rather than parking forever.
ESTABLISH_TIMEOUT_S = 30.0

#: The AF_UNIX sun_path ceiling, with the same headroom the
#: driver's own socket check keeps.
SOCKET_PATH_LIMIT = 100

#: The cause a row reads before any bridge session has been
#: established. The remedy is the client surface: start the
#: workspace from a machine that holds its identity, or run the
#: bridge command.
NO_IDENTITY = (
    "no bridge session — start the workspace from a client that "
    "holds its identity, or run 'msks proxy bridge <workspace>' "
    "(forward proxies need it)"
)

#: The cause a row reads when a live bridge session ended: the
#: master's keepalives expired (the guest's sshd restarted, the
#: session dropped, or the daemon crashed and the VM kept
#: running).
SESSION_ENDED = (
    "bridge session ended — run 'msks proxy bridge <workspace>' "
    "or restart the workspace"
)


def master_args(
    user: str,
    guest_ip: str,
    ctl: Path,
    relay: Path,
    identity: Path,
    known_hosts: Path,
) -> list[str]:
    """The establishing ssh argv: one control master, kept alive
    in the background after authentication, signing through the
    relayed agent and offering the workspace's own key only."""
    return [
        SSH,
        "-M",
        "-S",
        str(ctl),
        "-fN",
        "-o",
        "ControlPersist=yes",
        "-o",
        f"IdentityAgent={relay}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(identity),
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ServerAliveInterval={ALIVE_INTERVAL_S}",
        "-o",
        f"ServerAliveCountMax={ALIVE_COUNT}",
        "-l",
        user,
        guest_ip,
    ]


def channel_args(
    user: str, guest_ip: str, ctl: Path, host: str, port: int
) -> list[str]:
    """One forwarded connection's ssh argv: a ``-W`` channel on
    the workspace's master — no authentication of its own, so it
    works exactly as long as the master's session does."""
    return [
        SSH,
        "-S",
        str(ctl),
        "-o",
        "BatchMode=yes",
        "-o",
        f"ServerAliveInterval={ALIVE_INTERVAL_S}",
        "-o",
        f"ServerAliveCountMax={ALIVE_COUNT}",
        "-W",
        f"{host}:{port}",
        "-l",
        user,
        guest_ip,
    ]


def exit_args(ctl: Path) -> list[str]:
    """The master-shutdown argv: the control protocol's graceful
    ``exit``, which ends the session and removes the socket."""
    return [SSH, "-S", str(ctl), "-O", "exit", "localhost"]


class ChannelWriter:
    """The writer half of one ``-W`` channel: the subprocess's
    stdin, whose close also reaps the process — the channel ends
    when either side hangs up, and the ssh process must not
    outlive its pipes."""

    def __init__(self, process) -> None:
        self.process = process
        self.stream = process.stdin

    def write(self, data: bytes) -> None:
        self.stream.write(data)

    async def drain(self) -> None:
        await self.stream.drain()

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()

    async def wait_closed(self) -> None:
        if self.stream is not None:
            with contextlib.suppress(Exception):
                await self.stream.wait_closed()
        with contextlib.suppress(Exception):
            await self.process.wait()


def prepare_bridge_files(paths: dict, row: dict) -> None:
    """Lay the master's inputs in the workspace's bridge
    directory: the directory, the workspace's public half, and
    the host-key pin file the first accept-new writes into."""
    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["identity"].write_text(row["ssh_pubkey"] + "\n")
    paths["known_hosts"].touch(exist_ok=True)


async def master_verdict(
    process, workspace_id: str, causes: dict
) -> str | None:
    """The establishing master's outcome: None when it came up,
    the recorded cause when it did not — a timeout (a client that
    stopped serving) or ssh's own stderr tail."""
    try:
        await asyncio.wait_for(process.wait(), ESTABLISH_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        cause = (
            "bridge establish timed out — the client stopped "
            "serving signature requests"
        )
        causes[workspace_id] = cause
        return cause
    if process.returncode != 0:
        stderr = await process.stderr.read()
        detail = stderr.decode(errors="replace").strip().splitlines()
        tail = detail[-1] if detail else f"ssh exited {process.returncode}"
        causes[workspace_id] = f"bridge establish failed: {tail}"
        return causes[workspace_id]
    return None


class BridgeManager:
    """The bridge sessions on ``app.state.bridge``, one per
    workspace, keyed by workspace id.

    Owns each workspace's bridge directory under the daemon's
    state (the control socket, the host-key pin, the workspace's
    public half, the relay socket while a relay stands) and the
    per-workspace cause a proxy row reads when no session is up.
    """

    def __init__(self, app) -> None:
        self.app = app
        #: The per-workspace failure cause, None while a session
        #: is up. The spawn seam lets the tests drive a fake ssh.
        self.causes: dict[str, str] = {}
        self.spawn = asyncio.create_subprocess_exec
        #: The workspaces that have held a session — the line
        #: between "never established" (the identity cause) and
        #: "ended" (the session cause) a down row reads.
        self.established: set[str] = set()
        #: The workspaces a relay websocket currently serves — one
        #: at a time: a second relay while the bridge comes up
        #: would race the first's establish.
        self.relays: set[str] = set()

    def claim_relay(self, workspace_id: str) -> bool:
        """Take the workspace's relay slot, False when one serves."""
        if workspace_id in self.relays:
            return False
        self.relays.add(workspace_id)
        return True

    def release_relay(self, workspace_id: str) -> None:
        """Give the workspace's relay slot back (relay end)."""
        self.relays.discard(workspace_id)

    # --- paths -----------------------------------------------------

    def bridge_dir(self, workspace_id: str) -> Path:
        """The workspace's bridge directory under the daemon's
        state root — the same parent the per-VM directories
        live in."""
        root = self.app.state.settings.vmm.state_dir
        return root / "bridges" / workspace_id

    def paths(self, workspace_id: str) -> dict:
        """The workspace's bridge paths (control socket, agent
        relay socket, identity file, host-key pin), refusing a
        control socket the kernel could not address."""
        directory = self.bridge_dir(workspace_id)
        ctl = directory / "ctl.sock"
        if len(str(ctl)) > SOCKET_PATH_LIMIT:
            raise BridgeError(
                f"bridge socket path {ctl} is too long; use a "
                "shorter state_dir or workspace id"
            )
        return {
            "dir": directory,
            "ctl": ctl,
            "relay": directory / "relay.sock",
            "identity": directory / "identity.pub",
            "known_hosts": directory / "known_hosts",
        }

    # --- state -----------------------------------------------------

    def up(self, workspace_id: str) -> bool:
        """Whether the workspace's bridge session stands. The
        master's exit removes its control socket, so the socket's
        presence is the fact; a session that was up and now is
        not records the ended cause."""
        ctl = self.bridge_dir(workspace_id) / "ctl.sock"
        if ctl.exists():
            self.causes.pop(workspace_id, None)
            return True
        if workspace_id not in self.causes:
            self.causes[workspace_id] = (
                SESSION_ENDED
                if workspace_id in self.established
                else NO_IDENTITY
            )
        return False

    def cause(self, workspace_id: str) -> str:
        """The named cause a proxy row reads when no session is
        up."""
        return self.causes.get(workspace_id) or NO_IDENTITY

    # --- establish, dial, stop ---------------------------------------

    async def establish(
        self, row: dict, guest_ip: str, relay: Path
    ) -> str | None:
        """Start the workspace's bridge master through a live
        agent relay. None when a session already stands or the
        master came up; ssh's own stderr (or the timeout) as the
        cause when it did not. The relay socket must exist before
        the call: the master signs through it."""
        paths = self.paths(row["id"])
        ctl = paths["ctl"]
        if ctl.exists():
            self.causes.pop(row["id"], None)
            self.established.add(row["id"])
            return None
        prepare_bridge_files(paths, row)
        process = await self.spawn(
            *master_args(
                row.get("user") or "root",
                guest_ip,
                ctl,
                relay,
                paths["identity"],
                paths["known_hosts"],
            ),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        cause = await master_verdict(process, row["id"], self.causes)
        if cause is not None:
            return cause
        self.causes.pop(row["id"], None)
        self.established.add(row["id"])
        return None

    async def dial(self, workspace_id: str, port: int):
        """(reader, writer) for one connection to the guest's own
        loopback port, a ``-W`` channel on the workspace's master.
        Resolves the workspace's login user and live attachment
        itself; refuses with the named cause when the workspace is
        not running or no session stands."""
        attachment = self.app.state.net.attachment(workspace_id)
        if attachment is None:
            raise BridgeError(
                f"workspace {workspace_id} has no live network "
                "attachment (not running, or created without egress)"
            )
        row = await self.app.state.model.get_workspace(workspace_id)
        if row is None:
            raise BridgeError(f"workspace {workspace_id} is gone")
        user = row.get("user") or "root"
        paths = self.paths(workspace_id)
        if not paths["ctl"].exists():
            raise BridgeError(self.cause(workspace_id))
        process = await self.spawn(
            *channel_args(
                user, attachment.guest_ip, paths["ctl"], "127.0.0.1", port
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return process.stdout, ChannelWriter(process)

    async def stop(self, workspace_id: str) -> None:
        """End the workspace's bridge session (the detach hook;
        idempotent): the master's graceful exit, then the relay
        socket if one was left. The host-key pin and the public
        half stay — the next establish of the same workspace
        reuses them."""
        paths = self.paths(workspace_id)
        if paths["ctl"].exists():
            process = await self.spawn(
                *exit_args(paths["ctl"]),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5.0)
        with contextlib.suppress(OSError):
            paths["relay"].unlink()
        self.causes.pop(workspace_id, None)
