#!/usr/bin/env python3
"""Fail when tracked text files contain credential-shaped values."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_STRUCTURED_VALUE_SEPARATOR = r"""
    (?:
        [ \t]*
        |
        [ \t]*(?:\#[^\r\n]*)?\r?\n
        (?:
            [ \t]*\r?\n
            |
            [ \t]*\#[^\r\n]*\r?\n
        )*
        (?:
            [ \t]+
            |
            (?=["'])
        )
    )
"""
_ASSIGNMENT_PATTERN = re.compile(
    r"""
    (?:
        (?P<assignment_quote>["'])ANTHROPIC_AUTH_TOKEN(?P=assignment_quote)
        |
        \bANTHROPIC_AUTH_TOKEN\b
    )
    [ \t]*(?:=(?!=)|:)
    """
    + _STRUCTURED_VALUE_SEPARATOR
    + r"""
    (?P<value>
        \$\{\{[^\r\n]*?\}\}
        |
        \$\{[^\r\n\s]*\}
        |
        "[^"\r\n]*"(?![ \t]*:)
        |
        '[^'\r\n]*'(?![ \t]*:)
        |
        [^\s\#;'":]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BEARER_PATTERN = re.compile(
    r"""
    (?:
        (?P<header_quote>["'])Authorization(?P=header_quote)
        |
        \bAuthorization\b
    )
    [ \t]*:
    """
    + _STRUCTURED_VALUE_SEPARATOR
    + r"""
    (?:
        (?P<container_quote>["'])Bearer[ \t]+
        (?P<contained_value>[^"'\r\n]+?)
        (?P=container_quote)(?![ \t]*:)
        |
        Bearer[ \t]+
        (?P<value>
            \$\{\{[^\r\n]*?\}\}
            |
            "[^"\r\n]*"(?![ \t]*:)
            |
            '[^'\r\n]*'(?![ \t]*:)
            |
            [^\s,;`]+
        )
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PATTERNS = (
    ("credential assignment", _ASSIGNMENT_PATTERN),
    ("bearer credential", _BEARER_PATTERN),
)
_SPECIFICATION_DIRECTORY = PurePosixPath("docs/superpowers/plans")
_SHELL_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_GITHUB_SECRET_REFERENCE_PATTERN = re.compile(
    r"^\$\{\{\s*secrets\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}$",
    re.IGNORECASE,
)


class SecretScanError(RuntimeError):
    """Raised when repository contents cannot be enumerated safely."""


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line_number: int
    rule: str

    def render(self) -> str:
        display_path = self.path
        if any(character.isspace() or not character.isprintable() for character in display_path):
            display_path = json.dumps(display_path, ensure_ascii=True)
        return f"{display_path}:{self.line_number}: potential {self.rule}"


def _tracked_paths(repository: Path) -> list[tuple[bytes, str]]:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repository), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SecretScanError("unable to enumerate tracked files") from error

    entries: list[tuple[bytes, str]] = []
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        decoded_path = os.fsdecode(raw_path)
        relative_path = PurePosixPath(decoded_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise SecretScanError("git returned an unsafe tracked path")
        entries.append((raw_path, decoded_path))
    return entries


def _read_regular_file(repository: Path, raw_path: bytes) -> bytes | None:
    repository_bytes = os.fsencode(repository)
    full_path = os.path.join(repository_bytes, raw_path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(full_path, flags)
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _unquote(value: str) -> str:
    stripped = value.strip().strip("`")
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        return stripped[1:-1].strip()
    return stripped


def _is_placeholder(path: str, value: str) -> bool:
    normalized = _unquote(value).casefold()
    if not normalized:
        return True
    if (
        _SHELL_REFERENCE_PATTERN.fullmatch(normalized)
        or _GITHUB_SECRET_REFERENCE_PATTERN.fullmatch(normalized)
        or (normalized.startswith("<") and normalized.endswith(">"))
        or normalized.startswith("replace-with-")
        or normalized.startswith("your-")
        or normalized
        in {
            "changeme",
            "client-default-token",
            "dummy",
            "example",
            "example-token",
            "placeholder",
            "test",
            "xxx",
        }
    ):
        return True

    # The committed implementation specification contains the literal values it
    # requires this scanner to reject. Keep this allowance limited to those
    # non-runtime design documents rather than exempting Markdown or source code.
    relative_path = PurePosixPath(path)
    return relative_path.is_relative_to(_SPECIFICATION_DIRECTORY) and normalized in {
        "real-value",
        "test-secret",
    }


def scan_repository(repository: Path) -> list[Finding]:
    """Return secret-shaped findings from Git-tracked, non-binary regular files."""

    root = Path(repository).resolve(strict=True)
    findings: list[Finding] = []
    for raw_path, display_path in _tracked_paths(root):
        content = _read_regular_file(root, raw_path)
        if content is None:
            continue
        # A NUL byte classifies the tracked entry as binary. The scanner's
        # contract is intentionally limited to tracked text files.
        if b"\0" in content:
            continue
        text = content.decode("utf-8", errors="replace")
        newline_positions = [
            position for position, character in enumerate(text) if character == "\n"
        ]
        for rule, pattern in _PATTERNS:
            for match in pattern.finditer(text):
                value = match.groupdict().get("value") or match.groupdict().get("contained_value")
                if value is not None and not _is_placeholder(display_path, value):
                    line_number = bisect_right(newline_positions, match.start()) + 1
                    findings.append(Finding(display_path, line_number, rule))
    return sorted(set(findings))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print("secret_scan_error: unexpected arguments", file=sys.stderr)
        return 2
    try:
        findings = scan_repository(Path.cwd())
    except (OSError, SecretScanError):
        print("secret_scan_error: repository unavailable", file=sys.stderr)
        return 2
    if not findings:
        return 0
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
