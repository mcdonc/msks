"""Stub tools the net suites share: recording `ip`/`nft` scripts.

Each stub appends its invocation to a log file (one line, arguments
space-joined) and exits 0 — unless told to fail a subcommand, or the
subcommand is pre-scripted to answer with specific stderr (the
absent-device and absent-table tolerances).
"""

from pathlib import Path

IP_FAIL_AT = "MSKS_TEST_IP_FAIL_AT"
IP_STDERR = "MSKS_TEST_IP_STDERR"
NFT_FAIL_AT = "MSKS_TEST_NFT_FAIL_AT"
NFT_STDERR = "MSKS_TEST_NFT_STDERR"

IP_STUB = """#!/bin/sh
printf '%s\n' "$*" >> {log}
fail_at="${{MSKS_TEST_IP_FAIL_AT:-}}"
if [ -n "$fail_at" ] && [ "$1 $2" = "$fail_at" ]; then
  printf '%s' "${{MSKS_TEST_IP_STDERR:-boom}}" >&2
  exit 1
fi
exit 0
"""

NFT_STUB = """#!/bin/sh
# Args and the ruleset text both land in the log (stdin appended
# after a marker line), so tests can pin which ruleset reached nft.
printf '%s\n' "$*" >> {log}
printf '%s\n' "--- $*" >> {log}.stdin
cat >> {log}.stdin
fail_at="${{MSKS_TEST_NFT_FAIL_AT:-}}"
if [ -n "$fail_at" ] && [ "$1 $2" = "$fail_at" ]; then
  printf '%s' "${{MSKS_TEST_NFT_STDERR:-boom}}" >&2
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
