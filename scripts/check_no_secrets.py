#!/usr/bin/env python3
"""Fail when tracked text files contain credential-shaped values."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_ASSIGNMENT_PATTERN = re.compile(
    r"""
    (?<!\w)
    (?P<assignment_quote>["'])?
    (?P<credential_key>LLM_API_KEY|ANTHROPIC_AUTH_TOKEN)
    (?(assignment_quote)(?P=assignment_quote)|(?!\w))
    [ \t]*(?:=(?!=)|:)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BEARER_PATTERN = re.compile(
    r"""
    (?<!\w)
    (?P<header_quote>["'])?
    Authorization
    (?(header_quote)(?P=header_quote)|(?!\w))
    [ \t]*:
    """,
    re.IGNORECASE | re.VERBOSE,
)
_STRUCTURED_KEY_PATTERN = re.compile(
    r"""
    ^[ \t]*
    (?:
        (?P<structured_quote>["'])[^"'\r\n]+(?P=structured_quote)
        |
        [^\s\#="'`:]+
    )
    [ \t]*(?:=(?!=)|:)
    """,
    re.VERBOSE,
)
_UNQUOTED_ASSIGNMENT_VALUE_PATTERN = re.compile(r"""[^\s\#;'"`:]+""")
_UNQUOTED_BEARER_VALUE_PATTERN = re.compile(r"[^\s,;`]+")
_BEARER_PREFIX_PATTERN = re.compile(r"Bearer[ \t]+", re.IGNORECASE)
_DOCUMENTATION_DIRECTORIES = (
    PurePosixPath("docs/superpowers/plans"),
    PurePosixPath("docs/superpowers/specs"),
)
_SHELL_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_GITHUB_SECRET_REFERENCE_PATTERN = re.compile(
    r"^\$\{\{\s*secrets\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}$",
    re.IGNORECASE,
)
_MAX_DELIMITED_VALUE_LENGTH = 4_096
_MISSING_VALUE = object()
_INVALID_VALUE = object()


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


@dataclass(frozen=True)
class _PendingValue:
    kind: str
    line_number: int


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


def _allows_literal_placeholder(path: str) -> bool:
    relative_path = PurePosixPath(path)
    return relative_path in {PurePosixPath(".env.example"), PurePosixPath("README.md")} or any(
        relative_path.is_relative_to(directory) for directory in _DOCUMENTATION_DIRECTORIES
    )


def _is_placeholder(path: str, value: str) -> bool:
    normalized = _unquote(value).casefold()
    if not normalized:
        return True
    if _SHELL_REFERENCE_PATTERN.fullmatch(normalized) or _GITHUB_SECRET_REFERENCE_PATTERN.fullmatch(
        normalized
    ):
        return True
    if not _allows_literal_placeholder(path):
        return False
    return (
        (normalized.startswith("<") and normalized.endswith(">"))
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
            "test-secret",
            "xxx",
            "real-value",
        }
    )


def _iter_logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield logical lines while treating LF, CRLF, and bare CR as newlines."""

    line_number = 1
    line_start = 0
    position = 0
    while position < len(text):
        character = text[position]
        if character not in "\r\n":
            position += 1
            continue
        yield line_number, text[line_start:position]
        if character == "\r" and position + 1 < len(text) and text[position + 1] == "\n":
            position += 1
        position += 1
        line_start = position
        line_number += 1
    if line_start < len(text):
        yield line_number, text[line_start:]


def _skip_horizontal_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t":
        position += 1
    return position


def _delimited_value(
    text: str,
    position: int,
    opening: str,
    closing: str,
) -> str | None:
    if not text.startswith(opening, position):
        return None
    scan_end = min(len(text), position + _MAX_DELIMITED_VALUE_LENGTH)
    closing_start = text.find(closing, position + len(opening), scan_end)
    if closing_start < 0:
        # An unclosed or exceptionally long value is still suspicious. Return
        # a bounded token so callers report it without rescanning the suffix.
        return text[position:scan_end]
    return text[position : closing_start + len(closing)]


def _quoted_value(text: str, position: int) -> str | object:
    quote = text[position]
    scan_end = min(len(text), position + _MAX_DELIMITED_VALUE_LENGTH)
    quote_end = text.find(quote, position + 1, scan_end)
    if quote_end < 0:
        return text[position:scan_end]
    after_quote = _skip_horizontal_whitespace(text, quote_end + 1)
    if after_quote < len(text) and text[after_quote] == ":":
        return _INVALID_VALUE
    return text[position : quote_end + 1]


def _assignment_value(text: str, position: int = 0) -> str | object:
    position = _skip_horizontal_whitespace(text, position)
    if position >= len(text) or text[position] == "#":
        return _MISSING_VALUE
    github_value = _delimited_value(text, position, "${{", "}}")
    if github_value is not None:
        return github_value
    shell_value = _delimited_value(text, position, "${", "}")
    if shell_value is not None:
        return shell_value
    if text[position] in "'\"":
        return _quoted_value(text, position)
    scan_end = min(len(text), position + _MAX_DELIMITED_VALUE_LENGTH)
    value = _UNQUOTED_ASSIGNMENT_VALUE_PATTERN.match(text, position, scan_end)
    return value.group(0) if value is not None else _INVALID_VALUE


def _bearer_credential(text: str, position: int) -> str | object:
    scan_end = min(len(text), position + _MAX_DELIMITED_VALUE_LENGTH)
    prefix = _BEARER_PREFIX_PATTERN.match(text, position, scan_end)
    if prefix is None:
        return _INVALID_VALUE
    position = prefix.end()
    github_value = _delimited_value(text, position, "${{", "}}")
    if github_value is not None:
        return github_value
    shell_value = _delimited_value(text, position, "${", "}")
    if shell_value is not None:
        return shell_value
    if position < len(text) and text[position] in "'\"":
        return _quoted_value(text, position)
    scan_end = min(len(text), position + _MAX_DELIMITED_VALUE_LENGTH)
    value = _UNQUOTED_BEARER_VALUE_PATTERN.match(text, position, scan_end)
    return value.group(0) if value is not None else _INVALID_VALUE


def _bearer_value(text: str, position: int = 0) -> str | object:
    position = _skip_horizontal_whitespace(text, position)
    if position >= len(text) or text[position] == "#":
        return _MISSING_VALUE
    if text[position] not in "'\"":
        return _bearer_credential(text, position)
    quote = text[position]
    scan_end = min(len(text), position + _MAX_DELIMITED_VALUE_LENGTH)
    quote_end = text.find(quote, position + 1, scan_end)
    if quote_end < 0:
        return text[position:scan_end]
    after_quote = _skip_horizontal_whitespace(text, quote_end + 1)
    if after_quote < len(text) and text[after_quote] == ":":
        return _INVALID_VALUE
    return _bearer_credential(text[position + 1 : quote_end], 0)


def _append_finding(
    findings: list[Finding],
    path: str,
    line_number: int,
    rule: str,
    value: str | object,
) -> None:
    if isinstance(value, str) and not _is_placeholder(path, value):
        findings.append(Finding(path, line_number, rule))


def _scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    pending: _PendingValue | None = None
    for line_number, line in _iter_logical_lines(text):
        stripped = line.lstrip(" \t")
        if pending is not None:
            if not stripped or stripped.startswith("#"):
                continue
            if _STRUCTURED_KEY_PATTERN.match(line) is not None:
                pending = None
            elif len(stripped) != len(line) or stripped.startswith(("'", '"')):
                if pending.kind == "assignment":
                    value = _assignment_value(stripped)
                    rule = "credential assignment"
                else:
                    value = _bearer_value(stripped)
                    rule = "bearer credential"
                _append_finding(findings, path, pending.line_number, rule, value)
                pending = None
                continue
            else:
                pending = None

        for match in _ASSIGNMENT_PATTERN.finditer(line):
            value = _assignment_value(line, match.end())
            if value is _MISSING_VALUE:
                pending = _PendingValue("assignment", line_number)
                break
            _append_finding(findings, path, line_number, "credential assignment", value)

        for match in _BEARER_PATTERN.finditer(line):
            value = _bearer_value(line, match.end())
            if value is _MISSING_VALUE:
                pending = _PendingValue("bearer", line_number)
                break
            _append_finding(findings, path, line_number, "bearer credential", value)
    return findings


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
        findings.extend(_scan_text(display_path, text))
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
