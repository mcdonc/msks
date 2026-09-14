"""A minimal fake cloud-hypervisor REST server over AF_UNIX.

Speaks just enough HTTP/1.1 for the driver: parses the request line +
headers + Content-Length body, then answers from a per-route handler
table. Handlers may mutate ``FakeCH.state`` (the ``vm.info`` payload)
and fire ``on_shutdown`` hooks so tests can simulate process exit.
"""

import asyncio
import json


class FakeCH:
    """One fake VMM bound to one unix socket path."""

    def __init__(self, socket_path, responses: dict | None = None) -> None:
        self.socket_path = socket_path
        self.responses = responses or {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.state = {"state": "Running"}
        self.on_shutdown: list = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Bind the API socket."""
        self._server = await asyncio.start_unix_server(
            self._serve, str(self.socket_path)
        )

    async def stop(self) -> None:
        """Unbind the API socket."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request = await self._read_request(reader)
        if request is None:
            writer.close()
            return
        method, path, body = request
        self.requests.append((method, path, body))
        await self._respond(writer, method, path, body)

    async def _read_request(self, reader: asyncio.StreamReader):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        method, path, _version = lines[0].split(" ", 2)
        length = 0
        for line in lines[1:]:
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        body = None
        if length:
            raw = await reader.readexactly(length)
            body = json.loads(raw)
        return method, path, body

    async def _respond(self, writer, method: str, path: str, body: dict | None) -> None:
        if (method, path) in self.responses:
            status, payload = self.responses[(method, path)]
            data = payload.encode() if payload else b""
            writer.write(
                b"HTTP/1.1 %d\r\nContent-Length: %d\r\n\r\n%s"
                % (status, len(data), data)
            )
        elif path == "/api/v1/vm.info":
            payload = json.dumps(self.state).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s"
                % (len(payload), payload)
            )
        else:
            writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            if path in ("/api/v1/vm.shutdown", "/api/v1/vm.power-button"):
                for hook in self.on_shutdown:
                    hook()
        await writer.drain()
        writer.close()
