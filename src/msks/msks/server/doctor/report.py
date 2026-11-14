"""The doctor's report shapes and their rendering.

:class:`CheckResult` and :class:`DoctorReport` — the rows every
check answers with — beside the human rendering and the JSON
document the CLI prints.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hints import detect_package_manager


@dataclass
class CheckResult:
    """One check's outcome; ``is_warning`` grades the failure."""

    name: str
    ok: bool
    message: str
    is_warning: bool = False  # False = error (core path), True = warning
    hint: str = ""


@dataclass
class DoctorReport:
    """The checks doctor ran, with the pass/fail roll-up."""

    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def passed(self) -> bool:
        return all(r.ok or r.is_warning for r in self.results)

    @property
    def errors(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and not r.is_warning]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and r.is_warning]


# Ordered by specificity: dnf before yum (Fedora ships both),
# apt-get before apt (scripts prefer apt-get for non-interactive
# use). The name recorded is the manager's own — "apt", not
# "apt-get" — because the hint table keys on it.

# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def result_marker(result: CheckResult) -> str:
    """The ✓/⚠/✗ marker for one result."""
    if result.ok:
        return "✓"
    if result.is_warning:
        return "⚠"
    return "✗"


def hint_lines(hint: str) -> list[str]:
    """A hint as printed lines: the first carries the Run: label,
    the rest align beneath it — an upstream hint is a command
    sequence, one command a line, and the sequence reads as the
    operator pastes it."""
    parts = hint.split("\n")
    return [f"    Run:  {parts[0]}"] + [
        f"          {part}" for part in parts[1:]
    ]


def append_result(lines: list[str], result: CheckResult) -> None:
    """One result line with its marker; the hint follows on its own."""
    marker = result_marker(result)
    lines.append(f"  {marker} {result.name}: {result.message}")
    if result.hint:
        lines.extend(hint_lines(result.hint))


def append_failure_block(
    lines: list[str], results: list[CheckResult], marker: str, heading: str
) -> None:
    """A closing block that repeats each failure with its fix."""
    if not results:
        return
    lines.append(heading)
    lines.append("")
    for result in results:
        lines.append(f"  {marker} {result.name}: {result.message}")
        if result.hint:
            lines.extend(hint_lines(result.hint))
        lines.append("")


def summary_line(ok_count: int, errors: int, warnings: int) -> str:
    """The closing tally, naming only the nonzero grades."""
    parts = [f"{ok_count} passed"]
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    if warnings:
        parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    return ", ".join(parts)


def append_summary(lines: list[str], report: DoctorReport) -> None:
    """The tally plus, when anything failed, the repeated fix list."""
    errors = report.errors
    warnings = report.warnings
    ok_count = len(report.results) - len(errors) - len(warnings)
    if not errors and not warnings:
        lines.append(f"All {ok_count} checks passed.")
        return
    lines.append(summary_line(ok_count, len(errors), len(warnings)))
    lines.append("")
    append_failure_block(
        lines, errors, "✗", "Errors (the daemon needs these):"
    )
    append_failure_block(
        lines, warnings, "⚠", "Warnings (degraded paths, not core):"
    )


def format_report(report: DoctorReport) -> str:
    """Format a doctor report for the terminal."""
    lines = ["msksd doctor", "=" * 40]
    manager = detect_package_manager()
    lines.append(f"Package manager: {manager or '(none detected)'}")
    lines.append("")
    for result in report.results:
        append_result(lines, result)
    lines.append("")
    append_summary(lines, report)
    return "\n".join(lines)
