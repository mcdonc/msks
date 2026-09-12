"""Async client for cloud-hypervisor's REST API over an AF_UNIX socket.

No CLI scraping anywhere: every lifecycle call is an HTTP request
against the per-VM ``--api-socket`` (PUT /vm.create, PUT /vm.start,
GET /vm.info, PUT /vm.shutdown). Responses carry no body on success
except ``GET /vm.info`` (200 + JSON); errors map to
:class:`~msks.microvm.errors.MicrovmError` with the HTTP status
attached.
"""

import httpx

from .errors import MicrovmError


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
        """PUT /vm.create — install the full VM configuration."""
        await self._request("PUT", "/vm.create", json=config)

    async def start(self) -> None:
        """PUT /vm.start — begin execution."""
        await self._request("PUT", "/vm.start")

    async def info(self) -> dict:
        """GET /vm.info — the parsed VM status document."""
        result = await self._request("GET", "/vm.info")
        return result if isinstance(result, dict) else {}

    async def shutdown(self) -> None:
        """PUT /vm.shutdown — request a graceful power-off."""
        await self._request("PUT", "/vm.shutdown")

    async def _request(
        self, method: str, path: str, json: dict | None = None
    ) -> dict | None:
        response = await self._client.request(method, path, json=json)
        if response.status_code >= 400:
            detail = response.text.strip()
            raise MicrovmError(
                f"cloud-hypervisor {path} failed: {response.status_code} {detail}",
                status=response.status_code,
            )
        if response.status_code == 200:
            return response.json()
        return None
