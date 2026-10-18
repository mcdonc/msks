"""Extract a version's release notes from ``docs/changes.md``
(#564).

The changelog's version headings escape the opening bracket
(``## \\[v1.2.3] - date``) so the docs build reads them as plain
text, not link references (#3142). The release workflow matches
the section for the tag being cut — prefix-matched on the
bracketed version, backslashes stripped first, exactly the
release-cut rule the changelog's header comment states — and the
extracted section rides the GitHub release as its human-authored
notes, prepended to the auto-generated ones.

Pure text in, text out: the workflow reads the file at the tag
and hands it here; the suite pins the matching (the escape, the
date suffix, the prefix rule, the missing-section refusal).
"""

import argparse
import sys
from pathlib import Path

TOOL = "release-notes"


def version_heading(version: str) -> str:
    """The heading prefix a section for *version* starts with —
    the bracketed form without the escape, matched against the
    escape-stripped text (a dated ``- YYYY-MM-DD`` suffix and
    anything after the bracket stay out of the match)."""
    return f"## [{version}]"


def extract(version: str, text: str) -> str:
    """The section for *version*: every line from its heading
    until the next ``## `` heading, stripped of the heading
    itself. The changelog escapes brackets in its source; the
    match strips backslashes first so both spellings find the
    section. Raises ValueError naming the miss when the section
    is absent — a release cut from a tag without its changelog
    entry stops the workflow rather than silently dropping the
    human-authored notes."""
    lines = unescaped(text).splitlines()
    start = section_start(version, lines)
    return section_body(lines[start + 1 :])


def unescaped(text: str) -> str:
    """The changelog with its bracket escapes (#3142) stripped, so
    both spellings of a version heading match."""
    return text.replace("\\[", "[").replace("\\]", "]")


def section_start(version: str, lines: list[str]) -> int:
    """The line the version's section heading sits on, or the
    refusal naming the miss (a release cut from a tag without its
    changelog entry stops the workflow rather than silently
    dropping the human-authored notes)."""
    prefix = version_heading(version)
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            return index
    raise ValueError(
        f"{TOOL}: no {prefix!r} section in docs/changes.md — "
        "cut the release in the changelog first "
        "(rename [Unreleased] per the changelog's release-cut "
        "rules), then re-tag"
    )


def section_body(lines: list[str]) -> str:
    """The section's lines: everything until the next heading."""
    body: list[str] = []
    for line in lines:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="Extract one version's release-notes section "
        "from docs/changes.md (#564).",
    )
    parser.add_argument("version", help="the tag's version, e.g. v1.2.3")
    parser.add_argument(
        "path",
        nargs="?",
        default="docs/changes.md",
        help="the changelog to read (default: docs/changes.md)",
    )
    args = parser.parse_args(argv)
    try:
        text = Path(args.path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return 1
    try:
        section = extract(args.version, text)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    sys.stdout.write(section)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
