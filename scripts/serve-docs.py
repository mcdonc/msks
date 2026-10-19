#!/usr/bin/env python3
"""Serve the docs site with realtime re-rendering (#591).

A static server over the built ``site/`` plus a watcher over the
docs tree: when a chapter, the site config, or the renderings
change, the site rebuilds (the same strict build the deploy
runs) and every open browser tab reloads itself. The reload ride
is deliberately small: HTML responses gain a one-line poller on
the fly (the built files on disk stay untouched) that asks
``/__rev`` once a second and reloads the page when the build
revision moves.

Run by ``scripts/serve-docs.sh`` (which owns the pinned zensical
venv); the arguments are the site directory, the docs sources to
watch, the rebuild command as JSON, and the port.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: The poller injected into HTML answers: one ``/__rev`` fetch a
#: second, reload when the revision moves. The tag is one line so
#: the injection is a single replace before ``</body>``.
RELOAD_JS = (
    "var r; setInterval(function(){"
    "fetch('/__rev',{cache:'no-store'}).then(function(x){return x.text()})"
    ".then(function(v){ if(r===undefined){r=v} else if(r!==v)"
    "{location.reload()} })},1000)"
)
RELOAD_TAG = b"<script>" + RELOAD_JS.encode() + b"</script>"

#: The poll cadence over the docs tree (seconds).
WATCH_EVERY = 1.0


def fingerprint(paths: list[Path]) -> str:
    """One hash over every watched file's bytes — the revision."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        try:
            digest.update(path.read_bytes())
        except OSError:  # a vanished temp file: the next pass sees
            # its replacement (editors' atomic renames).
            continue
    return digest.hexdigest()


def with_poller(data: bytes) -> bytes:
    """The page with the reload poller before its last
    ``</body>``; a page without one passes through unchanged."""
    idx = data.rfind(b"</body>")
    if idx == -1:
        return data
    return data[:idx] + RELOAD_TAG + data[idx:]


def watched(sources: Path) -> list[Path]:
    """Every file under the docs sources, plus the site config."""
    files = [
        p for p in sources.rglob("*") if p.is_file() and "site" not in p.parts
    ]
    config = sources.parent / "zensical.toml"
    if config.is_file():
        files.append(config)
    return files


class ReloadHandler(SimpleHTTPRequestHandler):
    """The static server, with the poller injected into HTML."""

    def __init__(self, *args, revision: dict, **kwargs):
        self.revision = revision
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 — http.server's casing
        if self.path == "/__rev":
            self.serve_bytes(self.revision["value"].encode(), "text/plain")
            return
        target = self.html_target()
        if target is not None:
            self.serve_bytes(with_poller(target.read_bytes()), "text/html")
            return
        super().do_GET()

    def serve_bytes(self, body: bytes, ctype: str) -> None:
        """One whole answer from bytes this server owns."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def html_target(self) -> Path | None:
        """The HTML file a path answers with, or None: HTML is
        served whole (the injected poller changes the length, and
        the streaming base class would send the file's own
        Content-Length first — clients would cut the tag off the
        wire). translate_path stops at a directory; the index
        resolution it leaves to send_head is ours here."""
        target = Path(self.translate_path(self.path))
        if target.is_dir():
            target = target / "index.html"
        if target.is_file() and target.suffix in (".html", ".htm"):
            return target
        return None

    def end_headers(self) -> None:
        """Every answer stays no-cache, so a reload reads the
        rebuilt page, not a stale one."""
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


async def rebuild_loop(
    build_cmd: list[str], sources: Path, revision: dict
) -> None:
    """Watch the docs tree; on a change, rebuild and move the
    revision (every open tab reloads on its next poll)."""
    current = fingerprint(watched(sources))
    while True:
        await asyncio.sleep(WATCH_EVERY)
        now = fingerprint(watched(sources))
        if now == current:
            continue
        print("docs changed — rebuilding", file=sys.stderr)
        proc = subprocess.run(  # noqa: S603 — the fixed argv
            build_cmd, capture_output=True, text=True
        )
        if proc.returncode != 0:
            # The tab keeps the last good build; the error names
            # itself here for the operator fixing the docs.
            print(proc.stderr.strip(), file=sys.stderr)
        else:
            print(
                "rebuilt — tabs reload on their next poll",
                file=sys.stderr,
            )
        current = now
        revision["value"] = str(time.time())


def main() -> int:
    site = Path(sys.argv[1])
    sources = Path(sys.argv[2])
    build_cmd = json.loads(sys.argv[3])
    port = int(sys.argv[4])
    revision = {"value": str(time.time())}
    handler = partial(ReloadHandler, directory=str(site), revision=revision)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)

    async def run() -> None:
        await asyncio.gather(
            asyncio.to_thread(server.serve_forever),
            rebuild_loop(build_cmd, sources, revision),
        )

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
