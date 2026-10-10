"""The workspace-lifecycle routes (#1, #111, #121, #246, #248):
create, list, read, the ssh identity row, and
start/stop/reset/delete."""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from ...identity import LEGACY_LOGIN_USER, normalize_public_key
from ...imagestore import list_images as list_catalog_images
from ...imagestore import resolve_hash
from ...microvm.errors import MicrovmError
from ...model.secrets import SECRET_COVERAGES
from ...spec.egress import EGRESS_MODES, parse_allowlist
from ...storage import create_refusal
from .boot import create_name, mint_workspace_id, resolve_boot
from .deps import require_token
from .rows import (
    host_mismatch,
    owner_host,
    serialize_create,
    spec_for,
    workspace_or_404,
)
from .schemas import WorkspaceCreate
from .volumes import move_lock


def image_ref(images, image_hash: str | None) -> str | None:
    """The catalog reference for a workspace's image hash (#470
    L3) — ``name:version``, the words a listing's IMAGE column
    reads — resolved against ONE catalog listing the caller
    holds (the listing endpoint is polled every second by an open
    page and every five by the standing list refresh, so a
    per-row catalog walk would rescan the directory N times a
    request). None when the catalog cannot resolve the digest (a
    pruned image, a row older than the hash, no image at all).
    The digest stays in the row's own field; this is the
    human-facing alias."""
    if not image_hash:
        return None
    record = resolve_hash(image_hash, images)
    if record is None:
        return None
    return f"{record.name}:{record.version}"


def router(app) -> APIRouter:
    """The workspace-lifecycle routes."""
    api = APIRouter()

    @api.post("/api/v1/workspaces", dependencies=[Depends(require_token)])
    async def create_workspace(body: WorkspaceCreate) -> Response:
        name = create_name(body)
        if name is None:
            # A nameless create shares nothing keyable with another
            # create (the id mint and the primary-key index are the
            # whole race story), so it takes no create lock.
            return await create_workspace_locked(body, name)
        async with await serialize_create(app, name):
            return await create_workspace_locked(body, name)

    async def create_workspace_locked(
        body: WorkspaceCreate, name: str | None
    ) -> Response:
        # The ref namespace is one namespace (#246): a name another
        # workspace already owns, OR another workspace's id, is
        # refused — ref resolution prefers the id, so a workspace
        # named like a live id would be silently shadowed by it
        # (every command with that name would aim at the other
        # workspace). get_workspace answers for both spellings.
        if name is not None and (
            await app.state.model.get_workspace(name) is not None
        ):
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
        refusal = create_refusal(app.state.settings.vmm)
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
        # The daemon-wide placeholder posture (#339), fixed at
        # create and changeable later: an unknown value is a named
        # 400 here, like the consent posture above.
        coverage = body.secret_coverage or "all"
        if coverage not in SECRET_COVERAGES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"secret_coverage must be one of "
                    f"{list(SECRET_COVERAGES)}, got {coverage!r}"
                ),
            )
        boot["secret_coverage"] = coverage
        # The operator's key is the identity (#486): the client sends
        # its public half with the create — from the operator's
        # configured key file (identity_file / MSKSC_IDENTITY_FILE)
        # or a one-off --pubkey — and the daemon validates shape,
        # re-annotates provenance, and stores the public half only;
        # the row's private half stays NULL and the key endpoint
        # answers private_key: null. msks never mints. A create
        # with no key at all (a deliberate API call, or a payload-
        # only workspace) seeds no key: the guest answers the
        # console alone (#481's root autologin), and rows minted
        # before #486 keep serving the halves they already hold.
        if body.ssh_pubkey is not None:
            try:
                algo, key_body = normalize_public_key(body.ssh_pubkey)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            comment = name or workspace_id
            boot["ssh_pubkey"] = f"{algo} {key_body} msks-client:{comment}"
        # The creation name rides the seed's meta-data as the
        # guest's hostname (#370); a nameless workspace seeds the
        # minted id there instead (seed_metadata's fallback).
        boot["name"] = name
        # The persistent artifacts (#14) come before the row: a refused
        # create (a leftover artifact from a previous workspace of this
        # id) answers 503 with nothing written and nothing removed, and
        # a row in the table always has its artifacts underneath it.
        try:
            await app.state.microvm.prepare(spec_for(boot))
        except MicrovmError:
            # A racer may have won this name between the pre-check and
            # the strict prepare — its artifacts are the "leftover",
            # and the honest answer is the 409, not a removal plea.
            if name is not None and (
                await app.state.model.get_workspace(name) is not None
            ):
                raise HTTPException(
                    status_code=409, detail="workspace exists"
                ) from None
            raise
        try:
            row = await app.state.model.create_workspace(
                spec_for(boot),
                image_hash=boot["image_hash"],
                host=owner_host(app),
                name=name,
            )
        except IntegrityError:
            # The insert lost the race. The winner's row owns whatever
            # blank artifacts sit at this id's paths now (ours and its
            # are indistinguishable), so nothing is cleaned up — the
            # row-exists-⇒-artifacts-exist invariant must not break.
            # The collision is the name index in practice; a same-id
            # mint race between two different names (~2⁻²⁰) answers
            # the same 409, and a retried create succeeds.
            raise HTTPException(
                status_code=409, detail="workspace exists"
            ) from None
        return Response(
            status_code=201,
            content=json.dumps(row),
            media_type="application/json",
        )

    @api.get("/api/v1/create-defaults", dependencies=[Depends(require_token)])
    async def create_defaults() -> dict:
        """The sizes a create lands on when its body leaves them
        unset: the settings' root/home defaults, MiB — the TUI
        create form's size placeholders hint them."""
        vmm = app.state.settings.vmm
        return {"root_mib": vmm.root_mib, "home_mib": vmm.home_mib}

    @api.get("/api/v1/workspaces", dependencies=[Depends(require_token)])
    async def list_workspaces() -> list[dict]:
        """The workspace rows with each image's catalog reference
        (#470 L3) — the listing's client prefers the reference and
        keeps the digest as its own fallback. One catalog walk
        serves every row."""
        rows = await app.state.model.list_workspaces()
        images = list_catalog_images(app.state.settings.vmm.state_dir)
        for row in rows:
            row["image_ref"] = image_ref(images, row.get("image_hash"))
        return rows

    @api.get(
        "/api/v1/workspaces/{workspace_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_workspace(workspace_id: str) -> dict:
        return await workspace_or_404(app, workspace_id)

    @api.get(
        "/api/v1/workspaces/{workspace_id}/ssh-key",
        dependencies=[Depends(require_token)],
    )
    async def workspace_ssh_key(workspace_id: str) -> dict:
        """The workspace's ssh identity row, token-gated (#486).

        A workspace created now answers the operator's public half
        with ``private_key: null`` — msks never holds a private half
        anymore. Rows minted before #486 keep serving the halves
        they already hold (a daemon-side mint, or a client-side one
        whose public half rode the same create): a token holder
        already owns the workspace's root console, so the private
        half grants nothing new. The response carries the type name
        (parsed off the public line) so a client never guesses the
        algorithm. ``created_at`` (#245) stamps the workspace
        *instance*, and ``id``/``name`` (#246) carry its immutable
        identity — the client keys its caches on the id, so a
        workspace recreated under the same name cannot collide with
        the first instance's cached host keys. ``user`` (#248) is
        the workspace's recorded login user — the default
        ``msks ssh`` and ``msks console`` log in as — answered as
        the image's own account for a row created before
        per-workspace users, so every workspace serves one.
        """
        key = await app.state.model.get_ssh_key(workspace_id)
        if key is None:
            raise HTTPException(status_code=404, detail="no such workspace")
        if key["public_key"] is None:
            raise HTTPException(
                status_code=404,
                detail=f"workspace {workspace_id} has no ssh identity",
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
        await workspace_or_404(app, workspace_id)
        raise HTTPException(
            status_code=405,
            detail=(
                "workspaces cannot be modified after create (user_data is "
                "create-time; sizes and topology move through "
                f"POST /api/v1/workspaces/{workspace_id}/resize); delete "
                "the workspace and recreate it to change anything else"
            ),
        )

    @api.post(
        "/api/v1/workspaces/{workspace_id}/start",
        dependencies=[Depends(require_token)],
    )
    async def start_workspace(workspace_id: str) -> dict:
        row = await workspace_or_404(app, workspace_id)
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
            # Re-read under the lock (#280 review): a mode switch
            # landing between the outer read and this hold must not
            # boot under the posture the outer row named — the
            # permissive direction would leave the row and every
            # rules frame claiming a lockdown the VM does not run.
            row = await app.state.model.get_workspace(workspace_id)
            if row is None:
                raise HTTPException(
                    status_code=404, detail="no such workspace"
                )
            await app.state.microvm.launch(spec_for(row))
            await app.state.model.set_status(workspace_id, "running")
        return {"id": workspace_id, "status": "running"}

    @api.post(
        "/api/v1/workspaces/{workspace_id}/stop",
        dependencies=[Depends(require_token)],
    )
    async def stop_workspace(workspace_id: str) -> dict:
        row = await workspace_or_404(app, workspace_id)
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
        row = await workspace_or_404(app, workspace_id)
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

    @api.delete(
        "/api/v1/workspaces/{workspace_id}",
        dependencies=[Depends(require_token)],
    )
    async def delete_workspace(workspace_id: str) -> dict:
        row = await workspace_or_404(app, workspace_id)
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

    return api
