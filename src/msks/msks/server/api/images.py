"""The image-catalog routes (#141, #258, #270, #340): list,
import, designate, rename, remove — and the state-disk capacity
report that prices them."""

import asyncio
import contextlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ...imagestore import (
    ImageCollision,
    ImageError,
    default_image,
    fetch_archive,
    import_archive,
    is_url,
    remove,
    resolve,
    set_default,
    sweep_crash_leftovers,
    unset_default,
    warm_import,
)
from ...imagestore import (
    list_images as list_catalog_images,
)
from ...imagestore import (
    rename_image as rename_catalog_image,
)
from ...storage import MIB, floor_refusal, state_usage, storage_report
from .deps import require_token
from .schemas import ImageDefault, ImageImport, ImageRename


def download_ceiling(vmm) -> int:
    """The URL-import ceiling, clamped to what the state disk can
    actually hold (#258): the download itself must not spend the
    bytes the storage floor protects (#184). The post-download
    floor check still runs with the archive's real size — the
    import counts it twice (retained copy plus boot cache)."""
    max_bytes = vmm.image_import_max_mib * 1024 * 1024
    usage = state_usage(vmm.state_dir)
    if usage is None:
        return max_bytes
    protected = vmm.storage_floor_mib * MIB
    headroom = max(usage["free"] - protected, 0)
    return min(max_bytes, headroom)


def bootstrap_default_image(app) -> None:
    """Import MSKSD_DEFAULT_IMAGE once, as the catalog default.

    Failure is loud but non-fatal: a bad pointer must not take the
    daemon down with it (the operator can still import by API).
    """
    source = app.state.settings.vmm.default_image
    if not source:
        return
    state_dir = app.state.settings.vmm.state_dir
    sweep_crash_leftovers(state_dir)
    try:
        warm = warm_import(Path(source), state_dir)
        if warm is not None:
            return
        record = import_archive(Path(source), state_dir)
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
    set_default(record.hash, state_dir)
    print(f"msksd: default image {record.ref} ({record.hash[:12]}) imported")


def router(app, catalog_lock) -> APIRouter:
    """The catalog routes; imports and renames serialize on the
    daemon-wide catalog lock (#258, #340)."""
    api = APIRouter()

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
        images = await asyncio.to_thread(list_catalog_images, vmm.state_dir)
        return await asyncio.to_thread(
            storage_report,
            vmm.state_dir,
            vmm.storage_warn_pct,
            vmm.storage_floor_mib,
            rows,
            images,
        )

    @api.get("/api/v1/images", dependencies=[Depends(require_token)])
    async def list_images() -> list[dict]:
        state_dir = app.state.settings.vmm.state_dir
        default = default_image(state_dir)
        default_hash = default.hash if default is not None else None
        return [
            {
                "hash": image.hash,
                "name": image.name,
                "version": image.version,
                # The archive's own pair (#340): equal to name/version
                # for an unrenamed row, the origin beside a differing
                # registered pair for a renamed one.
                "origin_name": image.origin_name,
                "origin_version": image.origin_version,
                "cmdline": image.cmdline,
                "vsock_shell_port": image.vsock_shell_port,
                "console_protocol": image.console_protocol,
                "console_users": list(image.console_users),
                "kernel_version": image.kernel_version,
                "kernel_format": image.kernel_format,
                "provisioner": image.provisioner,
                "default": image.hash == default_hash,
                # ISO 8601, UTC-aware: the record's stamp (#186) or the
                # cache-mtime fallback. None only in the rename race.
                "imported": (
                    image.imported.isoformat()
                    if image.imported is not None
                    else None
                ),
            }
            for image in list_catalog_images(state_dir)
        ]

    @api.post("/api/v1/images", dependencies=[Depends(require_token)])
    async def import_image(body: ImageImport) -> Response:
        state_dir = app.state.settings.vmm.state_dir
        vmm = app.state.settings.vmm
        # A URL source is staged first (#258): the download lands in
        # the catalog's own dot-prefixed staging area (crash
        # leftovers sweep at startup), and the import below works on
        # the downloaded copy — the recorded hash always reflects
        # the fetched bytes.
        staged: Path | None = None
        try:
            async with catalog_lock:
                if is_url(body.source):
                    max_bytes = download_ceiling(vmm)
                    if max_bytes <= 0:
                        # The disk sits at or below the floor: the
                        # same named 507 a path import answers,
                        # before any bytes are fetched.
                        refusal = floor_refusal(vmm, "importing images")
                        raise HTTPException(
                            status_code=507,
                            detail=refusal
                            or "the state disk sits at the storage floor",
                        )
                    staged = await asyncio.to_thread(
                        fetch_archive,
                        body.source,
                        state_dir,
                        timeout_s=vmm.image_import_timeout_s,
                        max_bytes=max_bytes,
                    )
                    source = staged
                else:
                    source = Path(body.source)
                # The floor (#184): an import retains the archive **and**
                # unpacks its boot cache — the incoming bytes are counted
                # twice. A URL source is sized after its download, so a
                # floor refusal lands before the unpack with the real
                # size named.
                incoming_b = 0
                with contextlib.suppress(OSError):
                    incoming_b = 2 * source.stat().st_size
                refusal = floor_refusal(vmm, "importing images", incoming_b)
                if refusal is not None:
                    raise HTTPException(status_code=507, detail=refusal)
                record = await asyncio.to_thread(
                    import_archive,
                    source,
                    state_dir,
                    name=body.name,
                    version=body.version,
                )
                # The first imported image becomes the default: a fresh
                # daemon answers a bare workspace create immediately (the
                # sole-entry fallback would resolve it, but the pointer
                # keeps the designation explicit and stable across later
                # imports). Inside the lock: two imports into an empty
                # catalog otherwise both observe the other's row and
                # leave no default designated at all.
                if len(list_catalog_images(state_dir)) == 1:
                    set_default(record.hash, state_dir)
        except ImageCollision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except (ImageError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
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

    # Both default routes are declared before the {digest} route:
    # Starlette matches in registration order, and "default" would
    # otherwise land in the digest parameter's seat.
    @api.post("/api/v1/images/default", dependencies=[Depends(require_token)])
    async def set_default_image(body: ImageDefault) -> dict:
        """Designate the image a bare create boots (#270).

        The reference resolves against the catalog — the same forms
        a create's ``image`` field takes — and writing the pointer
        is the whole designation: it survives restarts (the pointer
        file), and a fresh ``MSKSD_DEFAULT_IMAGE`` import at startup
        still reclaims the slot. A miss is a named 404.
        """
        state_dir = app.state.settings.vmm.state_dir
        try:
            record = resolve(body.ref, state_dir)
        except ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if record is None:
            raise HTTPException(
                status_code=404, detail=f"no such image: {body.ref}"
            )
        set_default(record.hash, state_dir)
        return {
            "hash": record.hash,
            "name": record.name,
            "version": record.version,
            "ref": record.ref,
        }

    @api.delete(
        "/api/v1/images/default", dependencies=[Depends(require_token)]
    )
    async def unset_default_image() -> dict:
        """Clear the designation (#270).

        The answer reports the fallback a bare create now takes:
        the sole catalog entry, or none (a multi-image catalog
        without a designation refuses a bare create by name).
        """
        state_dir = app.state.settings.vmm.state_dir
        unset_default(state_dir)
        fallback = default_image(state_dir)
        return {
            "fallback": None
            if fallback is None
            else {
                "hash": fallback.hash,
                "name": fallback.name,
                "version": fallback.version,
                "ref": fallback.ref,
            }
        }

    @api.patch(
        "/api/v1/images/{digest}", dependencies=[Depends(require_token)]
    )
    async def rename_image(digest: str, body: ImageRename) -> dict:
        """Rename a cataloged image (#340).

        The registered ``name``/``version`` pair changes — the
        bytes, the hash, the origin pair, and workspaces already
        booting the image stay put, so ``name@hash`` and bare-hash
        references keep resolving. The rename serializes against
        imports (the catalog lock): an import swaps the per-hash
        cache aside, and an unlocked rename in that window would
        commit into the discarded cache. A composed pair another
        row already holds answers 409 naming it; a miss is a named
        404; a pair that fails the override checks is a named 400.
        """
        state_dir = app.state.settings.vmm.state_dir
        try:
            async with catalog_lock:
                record = await asyncio.to_thread(
                    rename_catalog_image,
                    digest,
                    state_dir,
                    name=body.name,
                    version=body.version,
                )
        except ImageCollision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except (ImageError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if record is None:
            raise HTTPException(
                status_code=404, detail=f"no such image: {digest}"
            )
        return {
            "hash": record.hash,
            "name": record.name,
            "version": record.version,
            "ref": record.ref,
            "origin": record.origin_ref,
        }

    @api.delete(
        "/api/v1/images/{digest}", dependencies=[Depends(require_token)]
    )
    async def delete_image(digest: str) -> dict:
        state_dir = app.state.settings.vmm.state_dir
        record = next(
            (
                image
                for image in list_catalog_images(state_dir)
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
        remove(digest, state_dir)
        return {"removed": digest}

    return api
