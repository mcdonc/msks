"""Async client for cloud-hypervisor's REST API over an AF_UNIX socket.

No CLI scraping anywhere: every lifecycle call is an HTTP request
against the per-VM ``--api-socket``. Routes carry the ``/api/v1``
prefix (the VMM's ``HTTP_ROOT``), boot is ``PUT /vm.boot`` (there is
no ``vm.start`` route), and errors map to
:class:`~msks.microvm.errors.MicrovmError` — including transport
failures (a stale socket left behind by a SIGKILLed VMM surfaces as a
connection error, which must not escape the seam raw).
"""

import httpx

from .errors import MicrovmError

API_ROOT = "/api/v1"


class CloudHypervisorApi:
    """One request-scoped client bound to one VM's API socket."""

    def __init__(self, socket_path, timeout_s: float) -> None:
        transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url="http://cloud-hypervisor",
            timeout=timeout_s,
        )

    async def aclose(self) -> None:
        """Release the underlying connection."""
        await self._client.aclose()

    async def create(self, config: dict) -> None:
        """PUT /api/v1/vm.create — install the full VM configuration."""
        await self._request("PUT", f"{API_ROOT}/vm.create", json=config)

    async def boot(self) -> None:
        """PUT /api/v1/vm.boot — begin execution."""
        await self._request("PUT", f"{API_ROOT}/vm.boot")

    async def info(self) -> dict:
        """GET /api/v1/vm.info — the parsed VM status document."""
        result = await self._request("GET", f"{API_ROOT}/vm.info")
        return result if isinstance(result, dict) else {}

    async def power_button(self) -> None:
        """PUT /api/v1/vm.power-button — press the ACPI power button.

        The guest's own handler (systemd-logind, or an acpid rule) runs
        the clean shutdown — unmounts, syncs — which is what a workspace
        with persistent disks needs. ``vm.shutdown`` is the *hard* stop
        in v52: the guest is never notified, page-cache writes are lost.
        """
        await self._request("PUT", f"{API_ROOT}/vm.power-button")

    async def _request(
        self, method: str, path: str, json: dict | None = None
    ) -> dict | None:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise MicrovmError(
                f"cloud-hypervisor API unreachable at {path}: {exc}"
            ) from exc
        if response.status_code >= 400:
            detail = response.text.strip()
            raise MicrovmError(
                f"cloud-hypervisor {path} failed: "
                f"{response.status_code} {detail}",
                status=response.status_code,
            )
        if response.status_code == 200:
            return response.json()
        return None
