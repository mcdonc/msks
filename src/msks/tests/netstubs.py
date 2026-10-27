"""Stub tools the net suites share: recording `ip`/`nft` scripts.

Each stub appends its invocation to a log file (one line, arguments
space-joined) and exits 0 — unless told to fail a subcommand, or the
subcommand is pre-scripted to answer with specific stderr (the
absent-device and absent-table tolerances).
"""

from pathlib import Path

IP_FAIL_AT = "IP_FAIL_AT"
IP_STDERR = "IP_STDERR"
NFT_FAIL_AT = "NFT_FAIL_AT"
NFT_STDERR = "NFT_STDERR"

IP_STUB = """#!/bin/sh
printf '%s\n' "$*" >> {log}
fail_at="${{IP_FAIL_AT:-}}"
if [ -n "$fail_at" ] && [ "$1 $2" = "$fail_at" ]; then
  printf '%s' "${{IP_STDERR:-boom}}" >&2
  exit 1
fi
exit 0
"""

NFT_STUB = """#!/bin/sh
# Args land in the log (one line, arguments space-joined); the
# ruleset text lands in {log}.stdin — catted from the file a
# ``-f <path>`` apply names, or from stdin when the invocation
# reads it — behind a marker line that names the apply's
# canonical shape, so tests split blocks without pinning the
# throwaway path. Each apply file's permission bits land in
# {log}.mode, one line per apply.
printf '%s\n' "$*" >> {log}
if [ "$1" = "-f" ] && [ "$2" != "-" ] && [ -f "$2" ]; then
  printf '%s\n' "--- -f -" >> {log}.stdin
  cat "$2" >> {log}.stdin
  stat -c '%a' "$2" >> {log}.mode
else
  printf '%s\n' "--- $*" >> {log}.stdin
  cat >> {log}.stdin
fi
fail_at="${{NFT_FAIL_AT:-}}"
word2="$2"
[ "$1" = "-f" ] && word2="-"
if [ -n "$fail_at" ] && [ "$1 $word2" = "$fail_at" ]; then
  printf '%s' "${{NFT_STDERR:-boom}}" >&2
  exit 1
fi
exit 0
"""


def stub_ip(directory: Path, log: Path) -> Path:
    """A recording `ip` stub."""
    script = directory / "ip"
    script.write_text(IP_STUB.format(log=log))
    script.chmod(0o755)
    return script


def stub_nft(directory: Path, log: Path) -> Path:
    """A recording `nft` stub (stdin consumed, arguments logged)."""
    script = directory / "nft"
    script.write_text(NFT_STUB.format(log=log))
    script.chmod(0o755)
    return script


def log_lines(log: Path) -> list[str]:
    """The recorded invocations, in order."""
    return log.read_text().splitlines()
