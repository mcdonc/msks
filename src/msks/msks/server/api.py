"""The /api/v1 surface: versioned, token-authenticated, one listener (#8).

Routes are thin: they parse, call the model layer or the microvm seam,
and shape responses. No business logic lives here, and no database
query is built outside ``msks.model``.
"""

import asyncio
import contextlib
import json
import os
import re
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi import __version__ as fastapi_version
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from starlette.requests import ClientDisconnect

from .. import __version__, imagestore, persist, storage
from ..consent.specs import EGRESS_MODES, parse_allowlist
from ..identity import (
    LEGACY_LOGIN_USER,
    LOGIN_NAME_RE,
    mint,
    normalize_public_key,
)
from ..imagestore import ImageError
from ..microvm.errors import MicrovmError
from ..microvm.spec import VmSpec, VmStatus
from ..model.egress_consent import DECISIONS, DURATIONS
from ..secretstore import (
    SecretStoreError,
    backend_ref,
    new_sentinel,
    valid_name,
)
from .auth import require_token
from .events import relay
from .watcher import watch_loop


class TokenCreate(BaseModel):
    name: str = "api"


class SecretMint(BaseModel):
    """A mint request (#198): one placeholder for one workspace.

    ``workspace_id`` names the workspace by id or name (#246) — the
    placeholder itself binds to the row's immutable id. The real
    secret rides the request body (the client read it from
    a file or stdin); it is never echoed in a response.
    """

    workspace_id: str
    name: str = Field(min_length=1, max_length=128)
    dests: list[str] = Field(min_length=1, max_length=32)
    # The same cap user_data carries: far more than any token or
    # key, small enough that a runaway upload fails validation.
    secret: str = Field(min_length=1, max_length=65536)
    ttl_s: int | None = Field(default=None, ge=1)


class SecretRenew(BaseModel):
    """A renew request: the new lifetime in seconds from now."""

    ttl_s: int = Field(ge=1)


#: One destination allowlist entry: an exact hostname or a
#: label-anchored suffix (``.example.com`` matches every host under
#: example.com and never ``notexample.com``) — the forms the #194
#: spike's matcher binds.
DEST_PATTERN = re.compile(
    r"^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)


WORKSPACE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"

#: The #41 payload cap, in characters (pydantic max_length): far more
#: script or cloud-config than any first boot needs, while keeping the
#: seed disk's staging small.
USER_DATA_MAX = 65536


class WorkspaceResize(BaseModel):
    """A resize request (#184): the new sizes, either side optional.

    The bounds match create — a resize is the create-time sizing
    revisited, so the same floors and ceilings hold.
    """

    root_mib: int | None = Field(default=None, ge=256, le=65536)
    home_mib: int | None = Field(default=None, ge=64, le=65536)


class EgressDecide(BaseModel):
    """A decider's verdict on a held request (#69)."""

    decision: str  # "allow" | "deny"
    duration: str = "tilrestart"  # once | 5m | 15m | tilrestart | forever


class ImageImport(BaseModel):
    """An import request: a host-side path to a container-image tar.

    The daemon's filesystem must reach it (a store path via the
    share, or a state-dir path) — the API deliberately
    does not accept uploads yet.
    """

    source: str


class WorkspaceCreate(BaseModel):
    # The workspace's name (#246): the operator-chosen label the CLI
    # addresses the workspace by — the same DNS-label charset the
    # id carried before the split (it still lands in display
    # surfaces, never in artifact paths). Optional: a nameless
    # workspace is addressable by its minted id only.
    name: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=WORKSPACE_ID_PATTERN
    )
    # The pre-#246 spelling of the same field, still accepted so a
    # client one version behind keeps working: the daemon treats it
    # as the name and mints the id either way.
    id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=WORKSPACE_ID_PATTERN
    )
    # Either a catalog reference (image: "name:version", "name", or
    # hash — the default image when omitted) or explicit boot
    # artifacts (kernel/rootfs paths; the pre-catalog shape the
    # tests and dev flows still use).
    image: str | None = None
    kernel: str | None = None
    initrd: str | None = None
    rootfs: str | None = None
    cmdline: str | None = None
    cpus: int = Field(default=2, ge=1, le=64)
    mem_mib: int = Field(default=1024, ge=64, le=1 << 15)
    # Persistent-artifact sizes (#14), fixed at create; unset takes
    # the MSKSD_ROOT_MIB / MSKSD_HOME_MIB defaults.
    root_mib: int | None = Field(default=None, ge=256, le=65536)
    home_mib: int | None = Field(default=None, ge=64, le=65536)
    # Egress networking (#52): boots with a virtio-net NIC onto a
    # per-VM host tap — the default. "egress": false opts
    # into the no-NIC posture.
    egress: bool = True
    # The consent mode and static allowlist (#69), fixed at create.
    # ``egress_mode`` picks the enforcement posture: ``allow``
    # (default — new flows pass, off-list names are recorded),
    # ``static`` (the allowlist only; off-list names never resolve),
    # ``interactive`` (each new flow's first packet holds until a
    # decider allows or denies it). The allowlist is specs:
    # ``host``/``host:port`` names (a leading ``.`` includes
    # subdomains, ``*.`` matches subdomains only) gate at the
    # daemon's resolver; ``cidr[:port]`` address specs accept in the
    # per-VM chain.
    egress_mode: str | None = None
    egress_allowlist: list[str] | None = Field(default=None, max_length=256)
    # First-boot provisioning (#41): a shell script (leading "#!") or
    # cloud-config YAML — cloud-init runs both — delivered on the
    # workspace's read-only cidata seed disk, composed beside the
    # minted identity's seeding script when one was minted (#111).
    # Create-time and immutable: a workspace keeps its payload until
    # it is deleted and recreated.
    user_data: str | None = Field(default=None, max_length=USER_DATA_MAX)
    # The no-escrow identity mode (#121): a public key line the
    # client minted, or one the operator already owns (#132) — any
    # well-formed key type. Present → the daemon stores and seeds the
    # public half only — no private half ever reaches it. Absent →
    # the daemon mints both halves itself (#111).
    ssh_pubkey: str | None = Field(default=None, max_length=16384)
    # The workspace's login user (#248): recorded on the row, seeded
    # into the guest at first boot (the account and its authorized_keys
    # when the image does not ship it), and served back as the
    # client's default login. Create-time and immutable, like the
    # specs. Absent → the workspace keeps the image's own login user
    # (the pre-#248 posture); the client fills its invoking user's
    # name before the request ever leaves.
    user: str | None = Field(default=None, pattern=LOGIN_NAME_RE.pattern)


#: The daemon's boot cmdline — a deployment names the
#: image it booted on it (``msksd.image=<store path>``), and
#: :func:`cmdline_image` reports that in ``/health`` so a client
#: can name drift between a running daemon and the tree's
#: current build (#160).
CMDLINE = Path("/proc/cmdline")


def cmdline_image() -> str | None:
    """The image this daemon booted from, when it says.

    A bare ``msksd`` carries no ``msksd.image`` pair
    and reports ``None``; clients treat that as "unknown" and skip
    the drift comparison.
    """
    with contextlib.suppress(OSError):
        for pair in CMDLINE.read_text().split():
            if pair.startswith("msksd.image="):
                return pair.split("=", 1)[1]
    return None


def bootstrap_default_image(app) -> None:
    """Import MSKSD_DEFAULT_IMAGE once, as the catalog default.

    Failure is loud but non-fatal: a bad pointer must not take the
    daemon down with it (the operator can still import by API).
    """
    source = app.state.settings.vmm.default_image
    if not source:
        return
    state_dir = app.state.settings.vmm.state_dir
    imagestore.sweep_crash_leftovers(state_dir)
    try:
        warm = imagestore.warm_import(Path(source), state_dir)
        if warm is not None:
            return
        record = imagestore.import_archive(Path(source), state_dir)
    except (ImageError, OSError) as exc:
        # Genuinely non-fatal: a bad pointer or a full state disk must
        # not take the daemon down with it (import remains available
        # by API once the operator clears it).
        print(f"msksd: default image import failed: {exc}")
        return
    # A FRESH import of MSKSD_DEFAULT_IMAGE owns the default slot,
    # however many images the catalog holds (#141): the setting points
    # at the archive the environment just built, and a rebuild whose
    # content changed must become what `msks create` boots — the first
    # implementation only designated when the catalog was empty, so
    # every rebuild after the first landed silently while creates kept
    # booting the old default. A warm hit (content unchanged) leaves
    # the pointer alone, so an operator's later API designation is
    # never stolen back by a restart.
    imagestore.set_default(record.hash, state_dir)
    print(f"msksd: default image {record.ref} ({record.hash[:12]}) imported")


def image_record(app, body: WorkspaceCreate):
    """The requested catalog record, or the default when omitted."""
    state_dir = app.state.settings.vmm.state_dir
    if body.image is not None:
        try:
            record = imagestore.resolve(body.image, state_dir)
        except ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if record is None:
            raise HTTPException(
                status_code=404, detail=f"no such image: {body.image}"
            )
        return record
    return imagestore.default_image(state_dir)


def create_name(body: WorkspaceCreate) -> str | None:
    """The create's label (#246): ``name``, the legacy ``id``
    spelling of it, or None for a nameless workspace.

    Both fields carry the same constraints, so a client one version
    behind keeps working (its ``id`` is treated as the name); sending
    both is accepted when they agree and refused when they differ —
    two different labels for one workspace is a client bug, not a
    coin flip.
    """
    if body.name is not None and body.id is not None:
        if body.name != body.id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "name and id disagree; send one (id is the "
                    "pre-#246 spelling of name)"
                ),
            )
        return body.name
    return body.name if body.name is not None else body.id


async def mint_workspace_id(model) -> str:
    """One fresh #246 instance id: 10 hex digits (5 random bytes).

    Short enough to read and copy from a listing, long enough that
    collisions need ~1M live workspaces to become likely — and the
    mint re-rolls while a candidate already answers on this daemon
    (an id AND a name block it: ref resolution prefers the id, so a
    workspace named like another's id would be shadowed by it). The
    insert's primary-key index is the backstop for a same-id race
    between two creates of different names.
    """
    while True:
        candidate = secrets.token_hex(5)
        if await model.get_workspace(candidate) is None:
            return candidate


def validated_user_data(body: WorkspaceCreate) -> str | None:
    """The #41 payload: present-and-nonempty.

    cloud-init runs both payload forms (#! scripts and cloud-config
    YAML), so the daemon accepts both; an image declares its
    provisioner in ``image.json`` for the operator and the docs, not
    for create-time policing. Explicit boot artifacts have no
    manifest at all — the same acceptance applies.
    """
    if body.user_data is None:
        return None
    if not body.user_data.strip():
        raise HTTPException(status_code=400, detail="user_data is empty")
    return body.user_data


def resolve_boot(app, body: WorkspaceCreate, workspace_id: str) -> dict:
    """Fill kernel/initrd/rootfs/cmdline from the image catalog.

    Explicit fields win over the image; the image wins over the
    default; nothing resolves at all is a client error. The result
    also carries the #14 facts: the catalog hash the overlay will
    bind to (None for explicit boot artifacts) and the artifact
    sizes. ``workspace_id`` is the daemon-minted instance id
    (#246) — the artifact paths derive from it, never from the
    operator's label.
    """
    record = image_record(app, body)
    kernel, rootfs = boot_pair(body, record)
    if (body.kernel is None) != (body.rootfs is None):
        raise HTTPException(
            status_code=400, detail="kernel and rootfs come together"
        )
    return {
        "id": workspace_id,
        "kernel": kernel,
        "initrd": default_initrd(body, record),
        "rootfs": rootfs,
        "cmdline": default_cmdline(body, record),
        "cpus": body.cpus,
        "mem_mib": body.mem_mib,
        "image_hash": bound_image_hash(body, record),
        "egress": body.egress,
        "user_data": validated_user_data(body),
        "login_user": body.user,
        **artifact_sizes(app, body),
    }


#: The console protocol the daemon negotiates (#63); manifest-keyed.
CONSOLE_PROTOCOL_PRELUDE = "prelude-v1"

#: The wire charset for a console user name — the same shape the
#: guest helper's prelude accepts, checked before anything is
#: forwarded. The login name's charset is the same one (#248's
#: create field, the seed's interpolation guard): one pattern, the
#: helper's own rule.
USER_NAME_RE = LOGIN_NAME_RE

#: The wire charset for a TERM value: printable ASCII minus space
#: (every terminfo name fits), matching the guest helper's check.
TERM_RE = re.compile(r"^[!-~]{1,32}$")


def close_reason(text: str, limit: int = 120) -> str:
    """A websocket close reason that fits its wire budget in bytes.

    Close reasons carry at most 123 bytes; a multibyte character at
    the cut makes a non-conformant frame, so the truncation happens
    on the UTF-8 bytes.
    """
    encoded = text.encode()[:limit]
    return encoded.decode(errors="ignore")


def console_request(params) -> tuple[str, int, int, str, str | None]:
    """The console websocket's user/rows/cols/term, or the refusal
    reason.

    The daemon validates what it can before opening the vsock stream:
    free-form strings never reach the guest-side parser, and the
    window size is bounded to what a pty can carry.
    """
    user = params.get("user", "root")
    if not USER_NAME_RE.fullmatch(user):
        return user, 24, 80, "xterm", f"invalid console user {user!r}"
    term = params.get("term", "xterm")
    if not TERM_RE.fullmatch(term):
        return user, 24, 80, "xterm", f"invalid console term {term!r}"
    rows, cols, problem = console_dimensions(params)
    return user, rows, cols, term, problem


def bearer_token(socket: WebSocket) -> str | None:
    """The Authorization header's Bearer token, or None.

    The forward websocket (#109) authenticates with the same header
    form as the REST surface. The console and events websockets carry
    their token in the query string because a browser cannot attach
    headers to a websocket; the forward is a CLI/tool endpoint with
    no browser caller, and a query string would land the token in
    proxy and process logs.
    """
    authorization = socket.headers.get("authorization", "")
    scheme, _, plaintext = authorization.partition(" ")
    if scheme.lower() != "bearer" or not plaintext:
        return None
    return plaintext


def forward_port(raw: str) -> tuple[int, str | None]:
    """The forward's TCP port from the path, or the refusal reason."""
    try:
        port = int(raw)
    except ValueError:
        return 0, f"port must be an integer, got {raw!r}"
    if not 1 <= port <= 65535:
        return 0, f"port={port} out of range"
    return port, None


def forward_allowed(app, row: dict, port: int) -> str | None:
    """The policy seam between auth and dial (#108).

    Every forward passes today: a token that reached this far already
    owns the workspace's root console, so no guest port is a privilege
    escalation. When port-scoped tokens arrive, the refusal reason
    this returns is the whole mechanism.
    """
    return None


def console_dimensions(params) -> tuple[int, int, str | None]:
    """rows/cols from the query string, or the refusal reason."""
    rows = console_dimension("rows", params.get("rows"), 24)
    cols = console_dimension("cols", params.get("cols"), 80)
    for value in (rows, cols):
        if isinstance(value, str):
            return 24, 80, value
    return rows, cols, None


def console_dimension(name: str, raw: str | None, default: int) -> int | str:
    """One window dimension: absent means the default, anything else
    must be an integer within a pty's range."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return f"{name} must be an integer, got {raw!r}"
    if not 1 <= value <= 65535:
        return f"{name}={value} out of range"
    return value


def console_image_policy(
    app, row: dict
) -> tuple[str, tuple[str, ...], str | None]:
    """The workspace image's console protocol, served users, and the
    refusal reason when the record cannot be read.

    A workspace bound to a catalog image gets that image's markers;
    anything booted from explicit artifacts is legacy (root only) —
    the raw root shell those images serve is exactly today's
    behavior. A bound image whose record is unreadable (corrupt
    image.json, out-of-band deletion) is refused loudly: piping a raw
    stream at a prelude guest yields an opaque refusal, and silently
    treating it as legacy would mask the corruption.
    """
    state_dir = app.state.settings.vmm.state_dir
    image_hash = row.get("image_hash")
    if not image_hash:
        return "legacy", ("root",), None
    cache = imagestore.images_dir(state_dir) / image_hash
    if not (cache / "image.json").is_file():
        return (
            "legacy",
            ("root",),
            f"image record unreadable: {image_hash[:12]}",
        )
    record = imagestore.load_record(cache)
    if record is None:
        return (
            "legacy",
            ("root",),
            f"image record unreadable: {image_hash[:12]}",
        )
    return record.console_protocol, record.console_users, None


def bound_image_hash(body: WorkspaceCreate, record) -> str | None:
    """The catalog hash the workspace's overlay binds to, if any.

    Binding follows the root disk: the image's rootfs only carries a
    workspace that did not override it with an explicit path."""
    if body.rootfs is None and record is not None:
        return record.hash
    return None


def artifact_sizes(app, body: WorkspaceCreate) -> dict:
    """root/home sizes: the request's, else the settings defaults."""
    vmm = app.state.settings.vmm
    return {
        "root_mib": body.root_mib
        if body.root_mib is not None
        else vmm.root_mib,
        "home_mib": body.home_mib
        if body.home_mib is not None
        else vmm.home_mib,
    }


def boot_pair(body: WorkspaceCreate, record) -> tuple[str, str]:
    """kernel/rootfs: explicit fields win, the record fills the rest."""
    if body.kernel is not None and body.rootfs is not None:
        return body.kernel, body.rootfs
    if record is None:
        raise HTTPException(
            status_code=400,
            detail="kernel/rootfs (or image, or a default image) required",
        )
    return fill(body.kernel, record.kernel), fill(body.rootfs, record.rootfs)


def fill(explicit: str | None, from_record: Path) -> str:
    """One field: the explicit value, else the record's path."""
    return explicit if explicit is not None else str(from_record)


def default_initrd(body: WorkspaceCreate, record) -> str | None:
    """The image's initrd when booting wholly from the catalog."""
    if body.initrd is None and record is not None and body.kernel is None:
        return str(record.initrd)
    return body.initrd


def default_cmdline(body: WorkspaceCreate, record) -> str:
    """The image's cmdline, or the legacy default with no record."""
    if body.cmdline is not None:
        return body.cmdline
    if record is not None:
        return record.cmdline
    return "console=hvc0 root=/dev/vda rw"


def spec_for(row: dict) -> VmSpec:
    """Rebuild the seam's VmSpec from a workspace row."""
    initrd = None if row["initrd"] is None else Path(row["initrd"])
    return VmSpec(
        workspace_id=row["id"],
        kernel=Path(row["kernel"]),
        rootfs=Path(row["rootfs"]),
        cmdline=row["cmdline"],
        cpus=row["cpus"],
        mem_mib=row["mem_mib"],
        initrd=initrd,
        root_mib=row["root_mib"],
        home_mib=row["home_mib"],
        egress=bool(row.get("egress", False)),
        egress_mode=row.get("egress_mode") or "allow",
        egress_allowlist=tuple(row.get("egress_allowlist") or ()),
        user_data=row.get("user_data"),
        ssh_pubkey=row.get("ssh_pubkey"),
        login_user=row.get("login_user"),
    )


def owner_host(app) -> str | None:
    """The host recorded as owning a new workspace's artifacts.

    Placement is a fact of the local backend: the artifacts are
    files on one host, and that host's name is recorded at create
    (#14) so only it may boot the workspace.
    """
    return app.state.settings.vmm.host_name


def host_mismatch(app, row: dict) -> str | None:
    """The named error when the artifacts live on another host.

    Placement is recorded at create (#14): a workspace's overlay and
    home volume live on one host, and only that host may boot it. A
    row without a host predates #14 — the artifacts are wherever
    this daemon finds them, so this host adopts the start.
    """
    recorded = row.get("host")
    local = app.state.settings.vmm.host_name
    if recorded is None or recorded == local:
        return None
    return (
        f"home volume for workspace {row['id']} lives on host {recorded}; "
        f"this host is {local}"
    )


#: The lifecycle statuses a home-volume move serves (#80): an
#: allow-list, so ``unknown`` — a possibly-live VM the watcher
#: could not probe — and any future status refuse until named here.
#: ``starting``/``running``/``paused`` all keep the volume: it is a
#: live block device in each of them.
HOME_FREE_STATUSES = ("created", "stopped", "absent")


def home_volume_guard(app, row: dict) -> tuple[int, str] | None:
    """(status, refusal) when this daemon cannot move the volume.

    Placement (the artifacts live on one host) and a possibly-live
    attachment are the two facts that block a move: the free
    statuses are named, everything else refuses.
    """
    mismatch = host_mismatch(app, row)
    if mismatch is not None:
        return 409, mismatch
    if row["status"] not in HOME_FREE_STATUSES:
        return 409, (
            f"workspace {row['id']} is {row['status']}; "
            f"stop it before moving its home volume"
        )
    return None


async def home_volume_lock(app, workspace_id: str) -> asyncio.Lock:
    """Serialize a workspace's volume moves against its boots (#80).

    A boot attaches the volume file by path; an install that
    renames a new volume over that path mid-boot silently loses
    every guest write after the rename. One lock per workspace,
    held by start across launch and by both volume routes across
    their whole exchange, orders the pair: the boot waits out an
    in-flight move and boots the installed volume, and a move that
    arrives after a boot sees the running row and answers 409.
    """
    lock = app.state.home_locks.setdefault(workspace_id, asyncio.Lock())
    return lock


async def acquire_move_lock(app, workspace_id: str) -> asyncio.Lock:
    """Acquire the workspace's move-lock, or answer the named 409.

    A stalled reader holds an export's lock as long as its
    connection lives; a waiter that blocked on it would hang with
    it. Waiters give up after ``move_wait_timeout_s`` and name the
    move in flight (#80 review).
    """
    lock = await home_volume_lock(app, workspace_id)
    try:
        async with asyncio.timeout(app.state.settings.vmm.move_wait_timeout_s):
            await lock.acquire()
    except TimeoutError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace {workspace_id} has a volume move in flight; "
                f"retry when it finishes"
            ),
        ) from None
    return lock


@contextlib.asynccontextmanager
async def move_lock(app, workspace_id: str):
    """acquire_move_lock as a context: start, delete, and the
    import route hold it this way."""
    lock = await acquire_move_lock(app, workspace_id)
    try:
        yield lock
    finally:
        lock.release()


async def rechecked_row(app, workspace_id: str) -> dict:
    """The row re-read under the move-lock, with both guards applied.

    The row's status is the cheap guard; the live seam is the
    truth-guard: a watcher scan that probed a launch's spawn window
    can leave a live VM's row at ``stopped`` for one poll interval,
    and the seam's answer refuses where the row lies (#80 review).
    """
    row = await app.state.model.get_workspace(workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such workspace")
    guard = home_volume_guard(app, row)
    if guard is not None:
        raise HTTPException(*guard)
    info = await app.state.microvm.info(workspace_id)
    if info.status not in (VmStatus.ABSENT, VmStatus.STOPPED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace {workspace_id}'s VMM reports {info.status.value}; "
                f"stop it before moving its home volume"
            ),
        )
    return row


class HoldingStreamingResponse(StreamingResponse):
    """A streaming response that owns its fd and lock to its last
    send (#80 review).

    The release is bound to the response's own ``__call__`` — the
    send loop the server awaits — not the body iterator's fate: a
    client disconnect or a send failure unwinds this frame
    deterministically, where an abandoned generator's ``finally``
    would wait on garbage collection.
    """

    def __init__(self, body, *, teardown, **kwargs) -> None:
        super().__init__(body, **kwargs)
        self.teardown = teardown

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.teardown()


def volume_teardown(fd: int, lock: asyncio.Lock):
    """The response's teardown: close the fd, then hand the lock
    back (close first, so the waiter behind the lock never observes
    a live fd; raw-fd close stays EBADF-safe against a late
    threadpool read)."""

    def teardown() -> None:
        with contextlib.suppress(OSError):
            os.close(fd)
        lock.release()

    return teardown


async def locked_export(app, hub, workspace_id: str, home: Path) -> Response:
    """The export under the workspace's move-lock (#80).

    The lock spans the re-check and the open and rides the response
    through the stream: a boot that arrives mid-download waits it
    out instead of attaching a volume whose bytes are leaving. The
    size comes from the open fd, so the served length always
    matches the body it yields.
    """
    lock = await acquire_move_lock(app, workspace_id)
    try:
        await rechecked_row(app, workspace_id)
        try:
            fd, size = await asyncio.to_thread(persist.open_sized, home)
        except OSError:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"home volume file for workspace {workspace_id} "
                    f"is missing or unreadable under the state dir; "
                    "a start would rebuild it blank"
                ),
            ) from None
    except BaseException:
        lock.release()
        raise
    return HoldingStreamingResponse(
        export_body(hub, workspace_id, fd),
        teardown=volume_teardown(fd, lock),
        media_type="application/octet-stream",
        headers={
            "content-length": str(size),
            "content-disposition": (
                f'attachment; filename="{workspace_id}.ext4"'
            ),
        },
    )


async def export_body(hub, workspace_id: str, fd: int) -> AsyncIterator[bytes]:
    """The streamed half: the volume's windows. The completion event
    fires only on a clean end of file — a client that disconnects
    mid-download cancelled no export."""
    moved = 0
    async for window in persist.read_volume(fd):
        moved += len(window)
        yield window
    await hub.publish("home.exported", {"id": workspace_id, "bytes": moved})


async def installed_volume(
    state_dir: Path, workspace_id: str, request: Request
) -> int:
    """The upload's installed byte count, or its named HTTP failure.

    A body that is not ext4 and a body the client cut off are
    client errors; a disk-side failure is the daemon's — and all
    three leave the workspace's existing volume in place.
    """
    try:
        return await persist.import_home_volume(
            state_dir, workspace_id, request.stream()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (MicrovmError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ClientDisconnect as exc:
        raise HTTPException(
            status_code=400,
            detail="the upload ended before its body completed; "
            "the workspace kept its existing volume",
        ) from exc


async def locked_import(
    app, hub, workspace_id: str, request: Request
) -> Response:
    """The upload under the workspace's move-lock (#80).

    The row and seam re-read under the lock plus the lock itself
    close the boot race: a start either finished (the re-read
    refuses) or is waiting (the boot opens the installed volume).
    """
    await rechecked_row(app, workspace_id)
    state_dir = app.state.settings.vmm.state_dir
    total = await installed_volume(state_dir, workspace_id, request)
    await hub.publish("home.imported", {"id": workspace_id, "bytes": total})
    return Response(
        status_code=200,
        content=json.dumps({"id": workspace_id, "bytes": total}),
        media_type="application/json",
    )


async def serialize_create(app, key: str):
    """Serialize same-name creates end to end (#111, #246).

    The minted identity makes every create racer-specific — two
    concurrent creates of one name would each mint their own key,
    and the artifact installs are last-rename-wins, so the loser's
    seed could outlive its 409 under the winner's row: a workspace
    whose key never logs in. One lock per workspace name, held
    from the exists-check through the row insert, keeps the pair
    (row, seed) from one mint; the loser sees the winner's row and
    answers 409.
    """
    lock = app.state.create_locks.setdefault(key, asyncio.Lock())
    return lock


async def _workspace_or_404(app, workspace_id: str) -> dict:
    """The workspace a route's ref names — its immutable id or its
    unique name (#246) — or the 404."""
    row = await app.state.model.get_workspace(workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such workspace")
    return row


def placeholder_view(row: dict, sentinel: bool = True) -> dict:
    """The API-facing view of a placeholder row (#198).

    The sentinel appears only when *sentinel* is set — mint's 201
    carries it exactly once; every later view omits it.
    """
    view = {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "dests": row["dests"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }
    if sentinel:
        view["sentinel"] = row["sentinel"]
    return view


def build_api(app) -> FastAPI:
    """The FastAPI application bound to one msks App.

    The hub lives on ``app.state`` (#69) — the consent engine
    publishes verdict frames to it before the api exists.
    """
    hub = app.state.hub
    app.state.create_locks: dict[str, asyncio.Lock] = {}
    app.state.home_locks: dict[str, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        watcher: asyncio.Task | None = None
        try:
            app.state.model.migrate()
            await app.state.model.bootstrap_token()
            bootstrap_default_image(app)
            await app.state.net.start()
            await app.state.consent.start()
            watcher = asyncio.create_task(watch_loop(app, hub))
            api.state.watcher = watcher
            yield
        finally:
            # Close the model's engine so pooled sqlite connections
            # close deterministically — on shutdown and on a failed
            # startup step (migrate/bootstrap can have created the
            # engine before raising). The watcher teardown is its own
            # try so an unexpected watcher error cannot skip the close.
            try:
                if watcher is not None:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher
            finally:
                await app.state.consent.stop()
                await app.state.net.stop()
                await app.state.model.close()

    api = FastAPI(title="msksd", version=__version__, lifespan=lifespan)
    api.state.msks_app = app
    api.state.hub = hub

    @api.get("/api/v1/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "fastapi": fastapi_version,
            "image": cmdline_image(),
        }

    @api.post("/api/v1/tokens", dependencies=[Depends(require_token)])
    async def create_token(body: TokenCreate) -> Response:
        token_id, plaintext = await app.state.model.create_token(body.name)
        payload = {"id": token_id, "name": body.name, "token": plaintext}
        return Response(
            status_code=201,
            content=json.dumps(payload),
            media_type="application/json",
        )

    @api.get("/api/v1/tokens", dependencies=[Depends(require_token)])
    async def list_tokens() -> list[dict]:
        return await app.state.model.list_tokens()

    @api.delete(
        "/api/v1/tokens/{token_id}", dependencies=[Depends(require_token)]
    )
    async def revoke_token(token_id: int) -> dict:
        if not await app.state.model.revoke_token(token_id):
            raise HTTPException(status_code=404, detail="no such token")
        return {"revoked": token_id}

    # --- secrets (#198) -----------------------------------------------

    # One lock around every placeholder-mutating sequence (mint,
    # revoke, expiry sweep): the refs-query → manifest-write pair is
    # a TOCTOU otherwise — a stale snapshot landing last would strip
    # a live declaration, and a same-label mint race could leave an
    # uncertified value behind a 201-certified row. Store operations
    # are rare, so one global lock costs nothing.
    app.state.store_lock = asyncio.Lock()

    async def sync_store_manifest() -> None:
        """Re-render the store manifest off the live placeholder rows
        (off the event loop — it is file IO)."""
        refs = await app.state.model.placeholder_refs()
        await asyncio.to_thread(app.state.secrets.sync_manifest, refs)

    def validated_dests(dests: list[str]) -> list[str]:
        """Lowercased, de-duplicated, pattern-checked destinations."""
        seen = []
        for dest in dests:
            entry = dest.lower().rstrip(".")
            if not DEST_PATTERN.match(entry):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"dest {dest!r} must be an exact hostname or a "
                        "label-anchored suffix like .example.com"
                    ),
                )
            if entry not in seen:
                seen.append(entry)
        # An empty list never reaches here: pydantic's min_length=1
        # on the request model rejects it at validation.
        return seen

    @api.post("/api/v1/secrets", dependencies=[Depends(require_token)])
    async def mint_secret(body: SecretMint) -> Response:
        if not valid_name(body.name):
            raise HTTPException(
                status_code=422,
                detail=(
                    "name must be letters, numbers, or underscores "
                    "without a leading digit"
                ),
            )
        if not body.secret.strip():
            raise HTTPException(status_code=422, detail="the secret is empty")
        dests = validated_dests(body.dests)
        row = await app.state.model.get_workspace(body.workspace_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such workspace")
        # The ref (name or id, #246) resolved: the placeholder row
        # binds to the workspace's immutable id.
        workspace_id = row["id"]
        if (
            await app.state.model.placeholder_for(workspace_id, body.name)
            is not None
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"workspace {workspace_id} already has a "
                    f"placeholder named {body.name}"
                ),
            )
        ref = backend_ref(workspace_id, body.name)
        if await app.state.model.placeholder_by_ref(ref) is not None:
            # Distinct labels can sanitize to one ref; the collision
            # is answered before anything touches the shared store
            # entry (a 409 here keeps manifest and value intact).
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{workspace_id}/{body.name} collides with an"
                    f" existing placeholder on backend ref {ref};"
                    " pick another name"
                ),
            )
        expires = (
            datetime.now(UTC) + timedelta(seconds=body.ttl_s)
            if body.ttl_s is not None
            else None
        )
        sentinel = new_sentinel()
        async with app.state.store_lock:
            # Row before value, all under the store lock: an
            # uncertified byte can never land behind a ref a winning
            # row does not own — a losing same-label mint 409s on the
            # insert (or the pre-checks) and never touches the store,
            # and a failed write rolls its own row back.
            try:
                row = await app.state.model.create_placeholder(
                    workspace_id,
                    body.name,
                    sentinel,
                    dests,
                    ref,
                    expires,
                )
            except IntegrityError:
                raise HTTPException(
                    status_code=409, detail="placeholder name collision"
                ) from None
            # The manifest must declare the ref before the CLI can
            # write it; the row is in, so the live-rows sync carries
            # it.
            await sync_store_manifest()
            try:
                await app.state.secrets.write(ref, body.secret)
            except SecretStoreError as exc:
                # Roll the row back: a placeholder whose value never
                # landed would swap empty on the wire.
                await app.state.model.delete_placeholder(row["id"])
                await sync_store_manifest()
                raise HTTPException(status_code=503, detail=str(exc)) from None
            await app.state.model.record_audit("mint", row)
        # The sentinel appears in exactly one response: this one.
        return Response(
            status_code=201,
            content=json.dumps(placeholder_view(row)),
            media_type="application/json",
        )

    @api.get("/api/v1/secrets", dependencies=[Depends(require_token)])
    async def list_secrets() -> list[dict]:
        return [
            placeholder_view(row, sentinel=False)
            for row in await app.state.model.list_placeholders()
        ]

    @api.post("/api/v1/secrets/check", dependencies=[Depends(require_token)])
    async def check_secret_store() -> dict:
        try:
            return await app.state.secrets.check()
        except SecretStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @api.post(
        "/api/v1/secrets/{placeholder_id}/renew",
        dependencies=[Depends(require_token)],
    )
    async def renew_secret(placeholder_id: int, body: SecretRenew) -> dict:
        if await app.state.model.get_placeholder(placeholder_id) is None:
            raise HTTPException(status_code=404, detail="no such placeholder")
        expires = datetime.now(UTC) + timedelta(seconds=body.ttl_s)
        await app.state.model.renew_placeholder(placeholder_id, expires)
        row = await app.state.model.get_placeholder(placeholder_id)
        if row is None:
            # The expiry sweep can retire the row between the two
            # reads; a renew that lost its row answers 404, not 500.
            raise HTTPException(status_code=404, detail="no such placeholder")
        return placeholder_view(row, sentinel=False)

    @api.delete(
        "/api/v1/secrets/{placeholder_id}",
        dependencies=[Depends(require_token)],
    )
    async def revoke_secret(placeholder_id: int) -> dict:
        row = await app.state.model.get_placeholder(placeholder_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such placeholder")
        cleaned = True
        async with app.state.store_lock:
            # Row first: revocation takes effect on the next request,
            # whatever the store cleanup then does.
            await app.state.model.delete_placeholder(placeholder_id)
            try:
                await app.state.secrets.delete(row["backend_ref"])
            except SecretStoreError:
                # The row is gone, so the leftover value is inert; the
                # operator sees it in the response and can re-run
                # check.
                cleaned = False
            await app.state.model.record_audit("revoke", row)
            await sync_store_manifest()
        return {"revoked": placeholder_id, "store_cleaned": cleaned}

    @api.get("/api/v1/secrets/audit", dependencies=[Depends(require_token)])
    async def list_secret_audit() -> list[dict]:
        return await app.state.model.list_audit()

    @api.post("/api/v1/workspaces", dependencies=[Depends(require_token)])
    async def create_workspace(body: WorkspaceCreate) -> Response:
        name = create_name(body)
        async with await serialize_create(app, name or ""):
            return await create_workspace_locked(body, name)

    async def create_workspace_locked(
        body: WorkspaceCreate, name: str | None
    ) -> Response:
        if name is not None and await app.state.model.name_taken(name):
            raise HTTPException(status_code=409, detail="workspace exists")
        # The #246 instance id: minted by the daemon, immutable, and
        # never reused while its workspace lives — artifact paths,
        # caches, and every keyed surface derive from it, so a
        # workspace recreated under the same name is a different id
        # and cannot collide with the first instance anywhere.
        workspace_id = await mint_workspace_id(app.state.model)
        # The state-disk floor (#184): a create below it is the #180
        # failure mode in the making, so it answers a named 507 with
        # the reclaim path spelled out instead of wedging later.
        refusal = storage.create_refusal(app.state.settings.vmm)
        if refusal is not None:
            raise HTTPException(status_code=507, detail=refusal)
        boot = resolve_boot(app, body, workspace_id)
        # The consent posture (#69), fixed at create with the rest of
        # the egress facts: an unknown mode or an invalid spec is a
        # named 400 here, not a first-boot surprise.
        mode = (
            body.egress_mode
            if body.egress_mode is not None
            else app.state.settings.net.egress_mode
        )
        try:
            if mode not in EGRESS_MODES:
                raise ValueError(
                    f"egress_mode must be one of {list(EGRESS_MODES)}, "
                    f"got {mode!r}"
                )
            specs = parse_allowlist(body.egress_allowlist or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        boot["egress_mode"] = mode
        boot["egress_allowlist"] = specs
        # The identity (#111) mints before the artifacts: its public
        # half rides the seed (an artifact), its private half goes
        # straight into the row. A bad key type on a
        # directly-built Settings is a daemon fault, not a client
        # error. Keygen is CPU-bound (RSA 3072 especially): off the
        # loop, like every other tool call the routes make.
        #
        # The no-escrow mode (#121) replaces the mint: the client
        # minted the keypair and sent the public line, or the
        # operator supplied a key they already own (#132) — any
        # well-formed type, sshd the authority; the daemon validates
        # shape, re-annotates provenance, and stores the public half
        # only — the row's private half stays NULL and the key
        # endpoint answers private_key: null.
        private_key = None
        if body.ssh_pubkey is not None:
            try:
                algo, key_body = normalize_public_key(body.ssh_pubkey)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            comment = name or workspace_id
            boot["ssh_pubkey"] = f"{algo} {key_body} msks-client:{comment}"
        else:
            try:
                private_key, public_key = await asyncio.to_thread(
                    mint, app.state.settings.vmm.ssh_key_type
                )
            except ValueError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from None
            boot["ssh_pubkey"] = f"{public_key} msksd:{workspace_id}"
        # The persistent artifacts (#14) come before the row: a refused
        # create (a leftover artifact from a previous workspace of this
        # id) answers 503 with nothing written and nothing removed, and
        # a row in the table always has its artifacts underneath it.
        try:
            await app.state.microvm.prepare(spec_for(boot))
        except MicrovmError:
            # A racer may have won this name between the 404 check and
            # the strict prepare — its artifacts are the "leftover",
            # and the honest answer is the 409, not a removal plea.
            if name is not None and await app.state.model.name_taken(name):
                raise HTTPException(
                    status_code=409, detail="workspace exists"
                ) from None
            raise
        try:
            row = await app.state.model.create_workspace(
                spec_for(boot),
                image_hash=boot["image_hash"],
                host=owner_host(app),
                ssh_privkey=private_key,
                name=name,
            )
        except IntegrityError:
            # The insert lost the race. The winner's row owns whatever
            # blank artifacts sit at this id's paths now (ours and its
            # are indistinguishable), so nothing is cleaned up — the
            # row-exists-⇒-artifacts-exist invariant must not break.
            raise HTTPException(
                status_code=409, detail="workspace exists"
            ) from None
        return Response(
            status_code=201,
            content=json.dumps(row),
            media_type="application/json",
        )

    @api.get("/api/v1/storage", dependencies=[Depends(require_token)])
    async def get_storage() -> dict:
        """The capacity report (#184): the state-disk budget, each
        workspace's cost against its ceilings, and the catalog's.

        Computed on demand from one ``statvfs`` and a handful of
        ``lstat``s — the watcher's pressure probe, not this endpoint,
        is what watches the thresholds between requests.
        """
        vmm = app.state.settings.vmm
        rows = await app.state.model.list_workspaces()
        images = await asyncio.to_thread(imagestore.list_images, vmm.state_dir)
        return await asyncio.to_thread(
            storage.storage_report,
            vmm.state_dir,
            vmm.storage_warn_pct,
            vmm.storage_floor_mib,
            rows,
            images,
        )

    @api.get("/api/v1/images", dependencies=[Depends(require_token)])
    async def list_images() -> list[dict]:
        state_dir = app.state.settings.vmm.state_dir
        default = imagestore.default_image(state_dir)
        default_hash = default.hash if default is not None else None
        return [
            {
                "hash": image.hash,
                "name": image.name,
                "version": image.version,
                "cmdline": image.cmdline,
                "vsock_shell_port": image.vsock_shell_port,
                "console_protocol": image.console_protocol,
                "console_users": list(image.console_users),
                "kernel_version": image.kernel_version,
                "kernel_format": image.kernel_format,
                "provisioner": image.provisioner,
                "default": image.hash == default_hash,
            }
            for image in imagestore.list_images(state_dir)
        ]

    @api.post("/api/v1/images", dependencies=[Depends(require_token)])
    async def import_image(body: ImageImport) -> Response:
        # The floor (#184): an import retains the archive **and**
        # unpacks its boot cache — the incoming bytes are counted
        # twice.
        incoming_b = 0
        with contextlib.suppress(OSError):
            incoming_b = 2 * Path(body.source).stat().st_size
        refusal = storage.floor_refusal(
            app.state.settings.vmm, "importing images", incoming_b
        )
        if refusal is not None:
            raise HTTPException(status_code=507, detail=refusal)
        state_dir = app.state.settings.vmm.state_dir
        try:
            record = await asyncio.to_thread(
                imagestore.import_archive, Path(body.source), state_dir
            )
        except (ImageError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # The first imported image becomes the default: a fresh
        # daemon answers a bare workspace create immediately (the
        # sole-entry fallback would resolve it, but the pointer keeps
        # the designation explicit and stable across later imports).
        if len(imagestore.list_images(state_dir)) == 1:
            imagestore.set_default(record.hash, state_dir)
        return Response(
            status_code=201,
            content=json.dumps(
                {
                    "hash": record.hash,
                    "name": record.name,
                    "version": record.version,
                    "ref": record.ref,
                }
            ),
            media_type="application/json",
        )

    @api.delete(
        "/api/v1/images/{digest}", dependencies=[Depends(require_token)]
    )
    async def delete_image(digest: str) -> dict:
        state_dir = app.state.settings.vmm.state_dir
        record = next(
            (
                image
                for image in imagestore.list_images(state_dir)
                if image.hash == digest
            ),
            None,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="no such image")
        # An image a workspace still references cannot be removed:
        # its boot paths dangle, the workspace becomes unrestorable,
        # and its overlay would lose its backing file (#14).
        cache_prefix = str(record.kernel.parent) + "/"
        for row in await app.state.model.list_workspaces():
            if row.get("image_hash") == digest or str(
                row.get("kernel", "")
            ).startswith(cache_prefix):
                raise HTTPException(
                    status_code=409,
                    detail=f"workspace {row['id']} boots this image",
                )
        imagestore.remove(digest, state_dir)
        return {"removed": digest}

    @api.get("/api/v1/workspaces", dependencies=[Depends(require_token)])
    async def list_workspaces() -> list[dict]:
        return await app.state.model.list_workspaces()

    @api.get(
        "/api/v1/workspaces/{workspace_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_workspace(workspace_id: str) -> dict:
        return await _workspace_or_404(app, workspace_id)

    @api.get(
        "/api/v1/workspaces/{workspace_id}/ssh-key",
        dependencies=[Depends(require_token)],
    )
    async def workspace_ssh_key(workspace_id: str) -> dict:
        """The workspace identity (#111): both halves, token-gated.

        A token holder already owns the workspace's root console, so
        the private half grants nothing new; the response carries
        the type name (parsed off the public line) so a client never
        guesses the algorithm. A client-minted workspace (#121)
        answers ``private_key: null`` — the daemon never held that
        half; it lives on the client that created the workspace.
        ``created_at`` (#245) stamps the workspace *instance*, and
        ``id``/``name`` (#246) carry its immutable identity — the
        client keys its caches on the id, so a workspace recreated
        under the same name cannot collide with the first
        instance's cached host keys. ``user`` (#248) is the
        workspace's recorded login user — the default ``msks ssh``
        and ``msks console`` log in as — answered as the image's own
        account for a row created before per-workspace users, so
        every workspace serves one.
        """
        key = await app.state.model.get_ssh_key(workspace_id)
        if key is None:
            raise HTTPException(status_code=404, detail="no such workspace")
        if key["public_key"] is None:
            raise HTTPException(
                status_code=404,
                detail=f"workspace {workspace_id} has no minted identity",
            )
        return {
            "workspace": key["id"],
            "id": key["id"],
            "name": key["name"],
            "type": key["public_key"].split()[0],
            "public_key": key["public_key"],
            "private_key": key["private_key"],
            "created_at": key["created_at"],
            "user": key["login_user"] or LEGACY_LOGIN_USER,
        }

    # The #41 immutability contract, said out loud: the create-time
    # shape (user_data above all) never changes — a mutation attempt
    # gets a named error instead of a bare 405 from the router's
    # method table. Sizes are the one exception (#184): they move
    # through the resize route below.
    @api.put(
        "/api/v1/workspaces/{workspace_id}",
        dependencies=[Depends(require_token)],
    )
    @api.patch(
        "/api/v1/workspaces/{workspace_id}",
        dependencies=[Depends(require_token)],
    )
    async def mutate_workspace(workspace_id: str) -> dict:
        await _workspace_or_404(app, workspace_id)
        raise HTTPException(
            status_code=405,
            detail=(
                "workspaces cannot be modified after create (user_data is "
                "create-time; sizes move through "
                f"POST /api/v1/workspaces/{workspace_id}/resize); delete "
                "the workspace and recreate it to change anything else"
            ),
        )

    @api.post(
        "/api/v1/workspaces/{workspace_id}/resize",
        dependencies=[Depends(require_token)],
    )
    async def resize_workspace(
        workspace_id: str, body: WorkspaceResize
    ) -> dict:
        """Move a stopped workspace's sizes (#184): the home volume
        grows or shrinks, the overlay grows.

        The same guards a home-volume move carries: free lifecycle
        statuses, the placement check, the move-lock against a
        concurrent boot, and the live seam re-check. The floor never
        speaks here — a resize writes MiBs of filesystem metadata,
        and a shrink gives bytes back.
        """
        row = await _workspace_or_404(app, workspace_id)
        # The ref (name or id) resolved: everything keyed below uses
        # the row's immutable id (#246).
        workspace_id = row["id"]
        if mismatch := host_mismatch(app, row):
            raise HTTPException(status_code=409, detail=mismatch)
        if body.root_mib is None and body.home_mib is None:
            raise HTTPException(
                status_code=400,
                detail="nothing to resize: name root_mib, home_mib, or both",
            )
        async with move_lock(app, workspace_id):
            row = await rechecked_row(app, workspace_id)
            vmm = app.state.settings.vmm
            state_dir = vmm.state_dir
            home = persist.home_volume_path(state_dir, workspace_id)
            overlay = persist.overlay_path(state_dir, workspace_id)
            moved: list[str] = []
            if body.root_mib is not None:
                # The files are the truth, not the row: create clamps
                # the overlay to the base image's size, and a home
                # import can swap the volume in at any size. Root
                # grows only — its partition table and filesystem
                # belong to the guest. (#187.)
                if overlay.is_file():
                    virtual_b, image_format = await persist.base_info(
                        overlay, vmm.qemu_img, "the root overlay"
                    )
                    if image_format != "qcow2" or virtual_b == 0:
                        # qemu-img probes format, and a corrupt or
                        # truncated overlay answers "raw, 0 bytes" —
                        # a grow would silently truncate garbage. Name
                        # the corrupt file instead of moving it.
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"the root overlay for {workspace_id} is "
                                "not a readable qcow2 image (qemu-img "
                                f"reports {image_format}, {virtual_b} "
                                "bytes) — restore it with msks rm and a "
                                "fresh create, or a factory reset"
                            ),
                        )
                    ceiling_mib = virtual_b // (1024 * 1024)
                    if body.root_mib < ceiling_mib:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "the root overlay only grows; this one is "
                                f"{ceiling_mib} MiB and the request asked "
                                f"for {body.root_mib} MiB — msks rm and a "
                                "fresh create, or factory reset, reclaim a "
                                "root instead"
                            ),
                        )
                    if body.root_mib > ceiling_mib:
                        await persist.grow_overlay(overlay, body.root_mib, vmm)
                        moved.append(f"root grew to {body.root_mib} MiB")
                    # Equal to the file: only the row catches up.
                    await app.state.model.set_sizes(
                        workspace_id, body.root_mib, None
                    )
                elif body.root_mib != row["root_mib"]:
                    # The heal contract: no file, a new size — the row
                    # records it and the next start builds the blank
                    # overlay at it.
                    moved.append(f"root grew to {body.root_mib} MiB")
                    await app.state.model.set_sizes(
                        workspace_id, body.root_mib, None
                    )
            if body.home_mib is not None:
                if home.is_file():
                    if home.stat().st_size != body.home_mib * 1024 * 1024:
                        # e2fsck failures stay the daemon's 503; only
                        # the executed shrink's refusal is the
                        # client's to fix (data must move out of the
                        # tail).
                        await persist.volume_check(home, vmm)
                        executed_shrink = (
                            persist.volume_direction(home, body.home_mib)
                            == "shrank"
                        )
                        try:
                            direction = await persist.volume_move(
                                home, body.home_mib, vmm
                            )
                        except MicrovmError as exc:
                            if not executed_shrink:
                                raise
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"the shrink refused: {exc}; free data "
                                    "in the workspace's /home (or shrink "
                                    "less) and retry"
                                ),
                            ) from None
                        except OSError as exc:
                            # The volume vanished between the check
                            # and the move (an out-of-band rm): a
                            # named 503, not a bare 500.
                            raise HTTPException(
                                status_code=503,
                                detail=(
                                    f"the home volume for {workspace_id} "
                                    f"became unreachable mid-resize: {exc}"
                                ),
                            ) from None
                        moved.append(
                            f"home {direction} to {body.home_mib} MiB"
                        )
                    # The row follows the file, whichever moved.
                    await app.state.model.set_sizes(
                        workspace_id, None, body.home_mib
                    )
                elif body.home_mib != row["home_mib"]:
                    # The heal contract, the overlay's twin.
                    moved.append(f"home to {body.home_mib} MiB (fresh)")
                    await app.state.model.set_sizes(
                        workspace_id, None, body.home_mib
                    )
            updated = await app.state.model.get_workspace(workspace_id)
            if updated is None:
                # The row vanished under the move-lock (a concurrent
                # delete won it): answer 404, not a None crash.
                raise HTTPException(
                    status_code=404, detail="no such workspace"
                )
            # Nothing moved and nothing needs recording: idempotent,
            # with no event to announce.
            if not moved:
                return {**updated, "changes": []}
            await hub.publish(
                "workspace.resized",
                {
                    "id": workspace_id,
                    "root_mib": updated["root_mib"],
                    "home_mib": updated["home_mib"],
                    "changes": moved,
                },
            )
            return {**updated, "changes": moved}

    @api.post(
        "/api/v1/workspaces/{workspace_id}/start",
        dependencies=[Depends(require_token)],
    )
    async def start_workspace(workspace_id: str) -> dict:
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        mismatch = host_mismatch(app, row)
        if mismatch is not None:
            # Placement is a fact about the artifacts, not a
            # preference: booting elsewhere would present an empty
            # /home and a pristine root as if they were the data.
            raise HTTPException(status_code=409, detail=mismatch)
        # The home-volume move lock (#80): a volume import that
        # renames a new file over the boot's path mid-attach would
        # silently lose every guest write after the rename, so the
        # boot and any in-flight move serialize — the status write
        # stays inside the hold or a waiter would read a stale row.
        async with move_lock(app, workspace_id):
            await app.state.microvm.launch(spec_for(row))
            await app.state.model.set_status(workspace_id, "running")
        return {"id": workspace_id, "status": "running"}

    @api.exception_handler(MicrovmError)
    async def microvm_error(_request, exc: MicrovmError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @api.post(
        "/api/v1/workspaces/{workspace_id}/stop",
        dependencies=[Depends(require_token)],
    )
    async def stop_workspace(workspace_id: str) -> dict:
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        if mismatch := host_mismatch(app, row):
            # A stop from a non-owning host cannot reach the VMM; a
            # local no-op would mark a running VM stopped.
            raise HTTPException(status_code=409, detail=mismatch)
        await app.state.microvm.shutdown(workspace_id)
        await app.state.model.set_status(workspace_id, "stopped")
        return {"id": workspace_id, "status": "stopped"}

    @api.post(
        "/api/v1/workspaces/{workspace_id}/reset",
        dependencies=[Depends(require_token)],
    )
    async def reset_workspace(workspace_id: str) -> dict:
        """Factory reset: a pristine root, the same /home (#14)."""
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        if mismatch := host_mismatch(app, row):
            # The overlay lives on its owning host; resetting it from
            # here would no-op on this host's (absent) file and lie
            # about a pristine root.
            raise HTTPException(status_code=409, detail=mismatch)
        # The overlay is the running root device — stop the VM first,
        # with kill as the fallback for a wedged one (same contract
        # as delete).
        try:
            await app.state.microvm.shutdown(workspace_id)
        except MicrovmError:
            await app.state.microvm.kill(workspace_id)
        await app.state.microvm.reset(workspace_id)
        await app.state.model.set_status(workspace_id, "created")
        return {"id": workspace_id, "status": "created"}

    # The home-volume byte streams (#80): export for backup and
    # migration, import to restore or seed. Both refuse a workspace
    # the guard names, and both hold the workspace's move-lock for
    # their whole exchange — see home_volume_lock.
    @api.get(
        "/api/v1/workspaces/{workspace_id}/home",
        dependencies=[Depends(require_token)],
    )
    async def export_home_volume(workspace_id: str) -> Response:
        """Stream the workspace's /home volume out (#80): the volume
        file's bytes, verbatim."""
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        guard = home_volume_guard(app, row)
        if guard is not None:
            raise HTTPException(*guard)
        state_dir = app.state.settings.vmm.state_dir
        home = persist.home_volume_path(state_dir, workspace_id)
        return await locked_export(app, hub, workspace_id, home)

    @api.put(
        "/api/v1/workspaces/{workspace_id}/home",
        dependencies=[Depends(require_token)],
    )
    async def import_home_volume(
        workspace_id: str, request: Request
    ) -> Response:
        """Replace the workspace's /home volume with the request body
        (#80): the uploaded ext4 image lands atomically — a failed or
        refused upload leaves the old volume in place."""
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        guard = home_volume_guard(app, row)
        if guard is not None:
            raise HTTPException(*guard)
        # The floor (#184): an import streams a whole volume at the
        # state disk. The client's Content-Length sizes it when sent
        # (a chunked upload carries none and gets the floor alone);
        # the check runs before the body starts, so a refused import
        # installs nothing.
        incoming_b = 0
        length = request.headers.get("content-length", "")
        if length.isdigit():
            incoming_b = int(length)
        refusal = storage.create_refusal(
            app.state.settings.vmm, "importing a home volume", incoming_b
        )
        if refusal is not None:
            raise HTTPException(status_code=507, detail=refusal)
        async with move_lock(app, workspace_id):
            return await locked_import(app, hub, workspace_id, request)

    @api.delete(
        "/api/v1/workspaces/{workspace_id}",
        dependencies=[Depends(require_token)],
    )
    async def delete_workspace(workspace_id: str) -> dict:
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        if mismatch := host_mismatch(app, row):
            # Deleting the row from a non-owning host would orphan a
            # possibly-running VM: every route 404s without the row.
            raise HTTPException(status_code=409, detail=mismatch)
        # The home-volume move lock (#80 review): a delete that
        # races an import must not leave the row gone with the
        # import's rename landing after it (an orphaned volume the
        # next create would refuse on). Delete holds the same lock
        # the import does, so the pair is ordered either way.
        async with move_lock(app, workspace_id):
            # A wedged VM must still be deletable: a failed graceful
            # shutdown falls back to kill before cleanup.
            try:
                await app.state.microvm.shutdown(workspace_id)
            except MicrovmError:
                await app.state.microvm.kill(workspace_id)
            await app.state.microvm.cleanup(workspace_id)
            await app.state.model.delete_workspace(workspace_id)
        return {"deleted": workspace_id}

    @api.websocket("/api/v1/workspaces/{workspace_id}/console")
    async def console(socket: WebSocket, workspace_id: str) -> None:
        # Byte-stream bridge into a running workspace (#21): the
        # client gets an interactive shell over the same TLS + token
        # as the REST surface. Closing the websocket closes exactly
        # one guest shell session; the workspace keeps running.
        # Accept first, then close with a code: the client sees a
        # specific close reason (4401/4404/4501) instead of a generic
        # HTTP 403 rejection.
        await socket.accept()
        token = socket.query_params.get("token", "")
        if not await app.state.model.token_valid(token):
            await socket.close(code=4401)
            return
        row = await app.state.model.get_workspace(workspace_id)
        if row is None:
            await socket.close(code=4404)
            return
        # The ref (name or id, #246) resolved: the vsock dial and
        # every keyed surface below use the row's immutable id.
        workspace_id = row["id"]
        # Identity negotiation (#63): the daemon validates the request
        # against the image's served users before anything reaches the
        # guest, and only prelude images carry the user and window
        # size across the vsock link.
        user, rows, cols, term, problem = console_request(socket.query_params)
        if problem is not None:
            await socket.close(code=4400, reason=close_reason(problem))
            return
        protocol, served, unreadable = console_image_policy(app, row)
        if unreadable is not None:
            await socket.close(code=4501, reason=close_reason(unreadable))
            return
        # The workspace's recorded login user (#248) is served beside
        # the image's own console users: the first-boot seed
        # provisions the account, and the guest helper serves every
        # regular account passwd names — the manifest lists what the
        # IMAGE ships, the row adds what THIS workspace seeds.
        if user not in served and user != row.get("login_user"):
            refusal = f"console user {user!r} is not served"
            await socket.close(code=4400, reason=close_reason(refusal))
            return
        try:
            if protocol == CONSOLE_PROTOCOL_PRELUDE:
                reader, writer = await app.state.microvm.console(
                    workspace_id, user=user, rows=rows, cols=cols, term=term
                )
            else:
                reader, writer = await app.state.microvm.console(workspace_id)
        except MicrovmError as exc:
            # The client is token-authenticated by now: the cause is
            # not a secret, and the close reason is the only channel
            # an operator has for dead-VM vs refused vs deadline
            # (websocket close reasons cap at 123 bytes).
            await socket.close(code=4501, reason=close_reason(str(exc)))
            return
        try:
            await bridge_console(
                socket,
                reader,
                writer,
                app.state.settings.vmm.console_stall_timeout_s,
            )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @api.websocket("/api/v1/workspaces/{workspace_id}/forward/{port}")
    async def forward(socket: WebSocket, workspace_id: str, port: str) -> None:
        # Service-plane bridge (#109): raw bytes between the client
        # and a guest TCP port the caller names — the pipe ssh's
        # ProxyCommand rides. The token authenticates through the
        # Authorization header (REST's Bearer form): this endpoint's
        # callers are CLIs and tools, and a query string would put the
        # token in logs. Each websocket is one guest TCP connection.
        await socket.accept()
        token = bearer_token(socket)
        if token is None or not await app.state.model.token_valid(token):
            await socket.close(code=4401)
            return
        row = await app.state.model.get_workspace(workspace_id)
        if row is None:
            await socket.close(code=4404)
            return
        workspace_id = row["id"]
        target_port, problem = forward_port(port)
        if problem is not None:
            await socket.close(code=4400, reason=close_reason(problem))
            return
        refusal = forward_allowed(app, row, target_port)
        if refusal is not None:
            await socket.close(code=4403, reason=close_reason(refusal))
            return
        if not row.get("egress"):
            await socket.close(
                code=4501,
                reason=close_reason(
                    f"workspace {workspace_id} has no NIC "
                    "(created without egress)"
                ),
            )
            return
        try:
            reader, writer = await app.state.net.forward_stream(
                workspace_id, target_port
            )
        except MicrovmError as exc:
            await socket.close(code=4501, reason=close_reason(str(exc)))
            return
        try:
            # The opened publish lives inside the try so a cancellation
            # between dial and pump cannot skip the writer's cleanup.
            await hub.publish(
                "forward.opened", {"id": workspace_id, "port": target_port}
            )
            app.state.net.track_forward(workspace_id, writer)
            try:
                await pump_streams(socket, reader, writer)
            finally:
                app.state.net.untrack_forward(workspace_id, writer)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            # A detached task, not an await: teardown can cancel this
            # coroutine mid-finally (an await would raise CancelledError
            # and skip the event), and publish never blocks — it fans
            # out to subscriber queues synchronously.
            asyncio.create_task(
                hub.publish(
                    "forward.closed", {"id": workspace_id, "port": target_port}
                )
            )

    @api.websocket("/api/v1/events")
    async def events(socket: WebSocket) -> None:
        # Websockets cannot carry Authorization headers from browsers;
        # the token rides the query string instead (documented).
        token = socket.query_params.get("token", "")
        if not await app.state.model.token_valid(token):
            await socket.close(code=4401)
            return
        await socket.accept()
        queue = hub.subscribe()
        client_id = id(queue)
        try:
            receiver = asyncio.create_task(
                decider_loop(app, socket, client_id)
            )
            sender = asyncio.create_task(relay(queue, socket.send))
            done, pending = await asyncio.wait(
                {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            # The socket closing ends any decider authority this
            # client held (#69): interactivity follows the socket.
            app.state.deciders.deregister(client_id)
            hub.unsubscribe(queue)

    # --- egress consent (#69) ------------------------------------------------

    @api.get(
        "/api/v1/workspaces/{workspace_id}/egress",
        dependencies=[Depends(require_token)],
    )
    async def get_egress(workspace_id: str) -> dict:
        """The rule-management view: mode, static allowlist, and the
        in-effect verdicts."""
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        frame = await app.state.consent.rules_frame(workspace_id)
        return frame or {"workspace_id": workspace_id}

    @api.get(
        "/api/v1/workspaces/{workspace_id}/egress/requests",
        dependencies=[Depends(require_token)],
    )
    async def list_egress_requests(
        workspace_id: str,
        decision: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict]:
        """The consent rows (audit trail), newest first; the
        ``decision`` query filters one lifecycle state and
        ``limit`` bounds the page (1..1000, newest first)."""
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        if decision is not None and decision not in DECISIONS:
            raise HTTPException(
                status_code=400,
                detail=f"decision must be one of {list(DECISIONS)}",
            )
        return await app.state.model.egress_consent.list_requests(
            workspace_id, decision, limit
        )

    @api.post(
        "/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        dependencies=[Depends(require_token)],
    )
    async def decide_egress(
        workspace_id: str, request_id: str, body: EgressDecide
    ) -> dict:
        """A decider's verdict on a held request (#69): the held
        SYN releases on allow, refuses fast on deny; the duration
        sets how long enforcement honors the verdict."""
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        refusal = await verdict_refusal(app, workspace_id, request_id, body)
        if refusal is not None:
            raise HTTPException(*refusal)
        verdict = await app.state.consent.resolve(
            request_id,
            "allowed" if body.decision == "allow" else "denied",
            "token",
            body.duration,
        )
        if verdict is None:
            raise HTTPException(
                status_code=404,
                detail="no held request with that id",
            )
        return {"id": request_id, "verdict": verdict}

    @api.delete(
        "/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        dependencies=[Depends(require_token)],
    )
    async def revoke_egress(workspace_id: str, request_id: str) -> dict:
        """Undo an in-effect verdict (#69): the row flips to
        revoked, its flow rules and tracked connections clear, and
        the destination gates again at the next connection."""
        row = await _workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        row = await app.state.model.egress_consent.get_request(request_id)
        if row is None or row["workspace_id"] != workspace_id:
            raise HTTPException(
                status_code=404,
                detail="no consent request with that id for that workspace",
            )
        row = await app.state.consent.revoke(request_id, "token")
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="no in-effect verdict with that id",
            )
        return {"id": request_id, "revoked": True}

    return api


async def verdict_refusal(app, workspace_id, request_id, body) -> tuple | None:
    """(status, detail) when a decide request is malformed or names
    another workspace's hold — validated here so an invalid value
    answers a 400 (and a foreign hold a 404) instead of silently
    denying the held SYN and stranding its row."""
    if body.decision not in ("allow", "deny"):
        return 400, f"decision must be allow or deny, got {body.decision!r}"
    if body.duration not in DURATIONS:
        return 400, f"duration must be one of {list(DURATIONS)}"
    row = await app.state.model.egress_consent.get_request(request_id)
    if row is None or row["workspace_id"] != workspace_id:
        return 404, "no consent request with that id for that workspace"
    return None


def noop(*_observed) -> None:
    """The observer a plain forward passes: nothing to observe."""
    return None


async def pump_streams(
    socket: WebSocket, reader, writer, *, on_input=None, on_output=None
) -> str:
    """Pump raw bytes between a websocket and a byte stream.

    Two tasks, no queue: backpressure is websocket/TCP flow control
    (the byte stream must not lose or buffer unboundedly, #21).
    Whichever side finishes first (client disconnect or stream EOF)
    cancels the other — and an outer cancellation (the console's
    watchdog closing first, #103) cancels both inner tasks here, so
    nothing outlives the bridge writing into a closed stream.
    ``on_input`` and ``on_output`` observe the traffic in flight —
    the console's echo watchdog (#103) arms and disarms its deadline
    through them, and the console's refusal scan (#217) reads the
    guest bytes through ``on_output``; a plain forward passes none.
    Returns which side ended the session — ``"stream"`` for a guest
    EOF (or stream error), ``"socket"`` for a client disconnect —
    so the console bridge can close the websocket protocol-clean
    instead of returning out of the handler (#217).
    """
    to_guest = asyncio.create_task(
        _ws_to_stream(socket, writer, on_input or noop)
    )
    to_client = asyncio.create_task(_stream_to_ws(reader, socket, on_output))
    try:
        done, pending = await asyncio.wait(
            {to_guest, to_client}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        await cancel_tasks((to_guest, to_client))
        raise
    ended = "stream" if to_client in done else "socket"
    await settle(done, pending)
    return ended


async def cancel_tasks(tasks) -> None:
    """Cancel and drain the tasks, retrieving their outcomes — the
    outer-cancellation exit leaves no task and no unretrieved
    exception behind."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def settle(done, pending) -> None:
    """End the two-way race: cancel the loser, then read both
    outcomes quietly (the survivor's ending is the session's)."""
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    for task in done:
        with contextlib.suppress(Exception):
            task.result()


def scan_guest_protocol(
    tail: bytes, at_start: bool
) -> tuple[bool, str | None]:
    """(auth_ok, refusal_text) for the buffered guest output.

    The helper's protocol lines read at a line start; ``at_start``
    is true only while the buffer still holds the stream's first
    bytes (a slid tail makes every buffer start mid-stream). AUTH
    OK wins over a refusal in the same buffer: authenticated output
    is not a refusal even inside one read.
    """
    anchor = rb"(?:\A|\n)" if at_start else rb"\n"
    if re.search(anchor + rb"AUTH OK", tail):
        return True, None
    found = re.search(anchor + rb"(MSKS ERR[^\n]*)", tail)
    if found is not None:
        return False, found.group(1).decode(errors="replace").strip()
    return False, None


class _RefusalScan:
    """The #123 refusal line in guest output (``MSKS ERR ...``).

    A guest whose console helper rejects the session says so with
    one line and exits; without this scan the refusal reaches the
    client as transport death — the stream EOF ends the bridge
    with no websocket close at all (#217).

    The scan watches ALL guest output until the helper's ``AUTH
    OK`` line (the refusal can trail echoed input and split reads,
    so it cannot simply watch the first bytes) and then stands
    down — a logged or printed ``MSKS ERR`` line in post-auth
    shell output must not read as a refusal. The ``^`` anchor is
    trusted only while the stream's first bytes are still in the
    tail: once the tail slides, a mid-stream chunk boundary is
    indistinguishable from a line start. Matched, the bridge
    closes the websocket with
    :data:`CONSOLE_AUTH_REFUSED_CLOSE_CODE` and the guest's own
    text as the reason.
    """

    def __init__(self) -> None:
        self.text: str | None = None
        self.matched = asyncio.Event()
        self._tail = b""
        self._at_start = True
        self._armed = True

    def _append(self, data: bytes) -> None:
        """Grow the bounded tail; note when the stream's start slid
        out of it (the buffer start stops meaning a line start)."""
        self._tail = (self._tail + data)[-256:]
        if len(self._tail) < len(data):
            self._at_start = False

    def feed(self, data: bytes) -> None:
        """One relayed guest chunk; records the refusal once."""
        if self.text is not None or not self._armed:
            return
        self._append(data)
        auth_ok, refusal = scan_guest_protocol(self._tail, self._at_start)
        if auth_ok:
            # Authenticated: shell output from here on, not protocol.
            self._armed = False
        elif refusal is not None:
            self.text = refusal
            self.matched.set()


#: Close code for a console the guest refused (#123, #217): the
#: helper's ``MSKS ERR`` line names the reason in the close frame,
#: so a client can tell refusal apart from transport death. The
#: forward endpoint carries its own, unrelated 4403 ("not
#: permitted") in a separate table — the two must not merge.
CONSOLE_AUTH_REFUSED_CLOSE_CODE = 4403


async def bridge_console(
    socket: WebSocket, reader, writer, stall_timeout_s: float = 60.0
) -> None:
    """The console's pump: :func:`pump_streams` plus the echo
    watchdog.

    The guest pty echoes every input byte, so client input that draws
    zero guest bytes for ``stall_timeout_s`` names a wedged stream —
    the bridge closes the websocket with 4502 instead of hanging open
    and silent. An idle session (no input in flight) never trips it,
    and ``stall_timeout_s <= 0`` switches the watchdog off.

    Every ending is protocol-clean (#217): a refused session closes
    with 4403 and the guest's refusal text, a guest stream that ends
    (the helper exits, the shell logs out) closes with 1000, and only
    a client disconnect ends without a close frame — the client is
    gone; there is nothing to tell.
    """
    clock = _StallClock()
    refusal = _RefusalScan()

    def on_output(data: bytes) -> None:
        clock.disarm()
        refusal.feed(data)

    watchdog = asyncio.create_task(_echo_watchdog(socket, clock))
    pump = asyncio.create_task(
        pump_streams(
            socket,
            reader,
            writer,
            on_input=lambda: clock.arm(stall_timeout_s),
            on_output=on_output,
        )
    )
    refused = asyncio.create_task(refusal.matched.wait())
    done, pending = await asyncio.wait(
        {pump, watchdog, refused}, return_when=asyncio.FIRST_COMPLETED
    )
    ended = None
    if pump in done:
        with contextlib.suppress(Exception):
            ended = pump.result()
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    for task in done:
        with contextlib.suppress(Exception):
            task.result()
    await close_console_session(socket, ended, refusal)


async def close_console_session(socket, ended: str | None, refusal) -> None:
    """The bridge's protocol-clean ending (#217).

    Refusal first: a matched refusal closes with
    :data:`CONSOLE_AUTH_REFUSED_CLOSE_CODE` and the guest's text —
    even if the watchdog also fired. Then a guest stream that ended
    (shell exit, helper shutdown) closes with 1000. A client
    disconnect (``ended == "socket"``) and a watchdog close
    (``ended is None``, the watchdog having already closed 4502)
    attempt nothing. The suppress covers the races where the close
    already happened or the client vanished mid-close.
    """
    if refusal.text is not None:
        with contextlib.suppress(Exception):
            await socket.close(
                code=CONSOLE_AUTH_REFUSED_CLOSE_CODE,
                reason=close_reason(refusal.text),
            )
    elif ended == "stream":
        with contextlib.suppress(Exception):
            await socket.close(
                code=1000, reason=close_reason("console closed")
            )


class _StallClock:
    """The echo-deadline state (#103): armed by client input, cleared
    by any guest byte.

    A plain :class:`asyncio.Event` cannot carry the deadline: every
    input's ``set()`` resolves the watchdog's pending wait, and a
    later ``clear()`` from the guest-output task cannot cancel a
    timeout that ``wait_for`` already armed — the session would
    close one window after the last keystroke, echo or no echo.
    The clock holds the deadline itself; the watchdog sleeps until
    it and re-reads the state on every wake.
    """

    def __init__(self) -> None:
        self.deadline: float | None = None
        self.changed = asyncio.Event()

    def arm(self, timeout_s: float) -> None:
        """Input left the daemon: the guest has this long to answer.

        Armed only while no deadline is pending: the window runs from
        the FIRST unanswered input, not the last keystroke — a client
        that keeps sending into a wedged stream (the smoke probes'
        resend loop, a script pasting into a dead console) must not
        push its own close out of reach (#103).
        """
        if timeout_s > 0 and self.deadline is None:
            self.deadline = asyncio.get_running_loop().time() + timeout_s
            self.changed.set()

    def disarm(self) -> None:
        """Any guest byte: the stream is alive, no deadline pending."""
        self.deadline = None


#: Close code for a console stream that went silent with input in
#: flight (#103): input the guest never echoed means the stream (not
#: the workspace) is wedged; a reconnect gets a fresh session.
CONSOLE_STALLED_CLOSE_CODE = 4502


async def _echo_watchdog(socket: WebSocket, clock: _StallClock) -> None:
    """Close the session when the armed echo deadline expires."""
    loop = asyncio.get_running_loop()
    while True:
        # Clear before reading: an arm() racing this loop must leave
        # either a fresh deadline below or a set event to wake on —
        # clearing after the read could erase the wake and sleep
        # through an armed deadline.
        clock.changed.clear()
        deadline = clock.deadline
        if deadline is None:
            await clock.changed.wait()
            continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            await socket.close(
                code=CONSOLE_STALLED_CLOSE_CODE,
                reason=close_reason(
                    "console stalled: no guest bytes after input; "
                    "reconnect for a fresh session"
                ),
            )
            return
        try:
            await asyncio.wait_for(clock.changed.wait(), remaining)
            clock.changed.clear()
        except TimeoutError:
            # The sleep ran out: loop around, re-read the deadline
            # (input may have re-armed it, output cleared it).
            continue


async def _ws_to_stream(socket: WebSocket, writer, on_input=noop) -> None:
    """Client bytes to the stream; returns on disconnect."""
    while True:
        msg = await socket.receive()
        if msg["type"] != "websocket.receive":
            return
        data = msg.get("bytes")
        if data is None:
            data = msg.get("text", "").encode()
        if data:
            writer.write(data)
            # Input is now in flight: the console's echo deadline
            # starts if none is pending — one deadline per quiet
            # window, from the first unanswered input (#103).
            on_input()
            await writer.drain()


async def _stream_to_ws(reader, socket: WebSocket, on_output=None) -> None:
    """Stream bytes to the client; returns on stream EOF."""
    while True:
        data = await reader.read(4096)
        if not data:
            return
        # Any stream byte proves the stream alive (the console's
        # watchdog disarm, #103; the refusal scan reads the same
        # chunk, #217).
        if on_output is not None:
            on_output(data)
        await socket.send_bytes(data)


async def decider_loop(app, socket: WebSocket, client_id: int) -> None:
    """Read client control frames until disconnect (#69).

    A client announces itself as a consent decider with
    ``{"type": "egress.decider", "workspace": "<id>"}``; the
    registration lands it this workspace's pending holds and rules
    view directly (before the hub broadcast could), and its socket
    staying open is its liveness. Any other inbound frame is
    ignored — the relay task owns delivery.
    """
    while (message := await next_frame(socket)) is not False:
        if message is not None:
            await register_decider(app, socket, client_id, message)


async def next_frame(socket: WebSocket) -> dict | None | bool:
    """The next inbound frame: a decoded decider frame, None for
    an ignored message, or False when the socket closed (the
    loop's stop signal)."""
    try:
        raw = await socket.receive_text()
    except WebSocketDisconnect, RuntimeError:
        return False
    return decode_frame(raw)


async def register_decider(app, socket, client_id: int, message: dict):
    """One ``egress.decider`` frame: register the socket as this
    workspace's decider and land it the pending snapshot and rules
    view directly (before any hub broadcast could)."""
    workspace = message.get("workspace")
    if not isinstance(workspace, str):
        return
    row = await app.state.model.get_workspace(workspace)
    if row is None:
        # Say so: a decider pointed at a typo'd workspace would
        # otherwise wait on a silent, promptless connection.
        await socket.send_json(
            {
                "event": "egress.decider_rejected",
                "data": {"reason": "unknown workspace"},
            }
        )
        return
    # The frame names the workspace by id or name (#246); consent
    # state keys on the row's immutable id.
    workspace = row["id"]
    app.state.deciders.register(client_id, workspace)
    for pending in await app.state.consent.snapshot(workspace):
        await socket.send_json({"event": "egress.request", "data": pending})
    rules = await app.state.consent.rules_frame(workspace)
    if rules is not None:
        await socket.send_json({"event": "egress.rules", "data": rules})


def decode_frame(raw: str) -> dict | None:
    """A JSON object frame, or None for anything else."""
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("type") != (
        "egress.decider"
    ):
        return None
    return message
