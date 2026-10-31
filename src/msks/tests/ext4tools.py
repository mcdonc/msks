"""The ext4 resize tooling gate the volume suites share.

The home-volume resize pair (`resize2fs`/`e2fsck`) and its
formatting peers (`mkfs.ext4`, `dumpe2fs`) are Linux host tools:
a stock macOS dev host carries none of them, so tests whose
subject is a genuinely formatted filesystem skip there with the
reason named — the same named-skip posture the NFQUEUE binding's
suite takes. Tests that only stub the tools keep running on every
host; the always-clean `e2fsck` stand-in is the shared piece.
"""

import shutil
from pathlib import Path

import pytest

#: The four binaries a genuinely formatted test volume needs on
#: PATH. e2fsprogs ships them together, so one `which` pass answers
#: for the set.
REAL_EXT4_TOOLS = all(
    shutil.which(tool) is not None
    for tool in ("mkfs.ext4", "e2fsck", "resize2fs", "dumpe2fs")
)

#: The skip marker for tests that plant a genuinely formatted
#: volume and move its filesystem with the real tools.
realtools = pytest.mark.skipif(
    not REAL_EXT4_TOOLS,
    reason="mkfs.ext4/e2fsck/resize2fs/dumpe2fs not on PATH",
)


def write_e2fsck_stub(directory: Path) -> Path:
    """An always-clean e2fsck stand-in (records nothing): exit 0,
    the answer a checked volume's journal-quiet pass gives."""
    stub = directory / "e2fsck"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return stub
