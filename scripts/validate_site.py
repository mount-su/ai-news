#!/usr/bin/env python3
"""Validate internal URLs and structural invariants in a generated static site."""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import stat
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

_INVALID_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_URL_ATTRIBUTES = {"href", "src"}
_ALLOWED_EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel"}


@dataclass(frozen=True, order=True)
class ValidationIssue:
    path: str
    code: str
    message: str

    def render(self) -> str:
        display_path = self.path
        if any(character.isspace() or not character.isprintable() for character in display_path):
            display_path = json.dumps(display_path, ensure_ascii=True)
        return f"{display_path}: {self.code}: {self.message}"


@dataclass(frozen=True)
class _Reference:
    value: str


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.main_count = 0
        self.references: list[_Reference] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.casefold(): value for name, value in attrs}
        element_id = attributes.get("id")
        if element_id is not None:
            self.ids.append(element_id)
        if tag.casefold() == "main":
            self.main_count += 1
        for name in _URL_ATTRIBUTES:
            value = attributes.get(name)
            if value:
                self.references.append(_Reference(value=value))


def _validate_base_path(base_path: str) -> str:
    if not isinstance(base_path, str):
        raise ValueError("base_path must be a string")
    parsed = urlsplit(base_path)
    try:
        decoded = unquote(base_path, errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("base_path contains invalid encoding") from error
    if (
        not base_path
        or _INVALID_PERCENT.search(base_path)
        or "\\" in decoded
        or not base_path.startswith("/")
        or not base_path.endswith("/")
        or decoded.startswith("//")
        or "//" in decoded[1:]
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path != base_path
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ValueError("base_path must be one absolute site path with boundary slashes")
    return base_path


def _site_route(relative_path: str, base_path: str) -> str:
    pure_path = PurePosixPath(relative_path)
    if pure_path.name == "index.html":
        parent = pure_path.parent.as_posix()
        suffix = "" if parent == "." else f"{parent}/"
        return f"{base_path}{suffix}"
    return f"{base_path}{relative_path}"


def _decode_url_component(value: str) -> str:
    if _INVALID_PERCENT.search(value):
        raise ValueError("invalid percent escape")
    decoded = unquote(value, errors="strict")
    if "\x00" in decoded or "\\" in decoded:
        raise ValueError("invalid URL path")
    return decoded


def _validate_query(value: str) -> None:
    if _INVALID_PERCENT.search(value):
        raise ValueError("invalid percent escape")
    decoded = unquote(value, errors="strict")
    if "\x00" in decoded:
        raise ValueError("invalid URL query")


def _within_base_path(public_path: str, base_path: str) -> bool:
    if base_path == "/":
        return public_path.startswith("/")
    base_root = base_path.rstrip("/")
    return public_path == base_root or public_path.startswith(f"{base_root}/")


def _normalize_public_path(
    source_relative_path: str,
    raw_path: str,
    base_path: str,
) -> tuple[str, bool]:
    source_route = _site_route(source_relative_path, base_path)
    if not raw_path:
        return source_route, source_route.endswith("/")

    if raw_path.startswith("/"):
        joined = raw_path
    else:
        joined = urlsplit(urljoin(source_route, raw_path)).path
    decoded_path = _decode_url_component(joined)
    requested_directory = decoded_path.endswith("/")
    normalized = posixpath.normpath(decoded_path)
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if requested_directory and normalized != "/":
        normalized = f"{normalized}/"
    return normalized, requested_directory


def _target_relative_paths(
    public_path: str,
    requested_directory: bool,
    base_path: str,
) -> list[str]:
    if base_path == "/":
        relative = public_path.lstrip("/")
    else:
        base_root = base_path.rstrip("/")
        relative = public_path.removeprefix(base_root).lstrip("/")
    if not relative:
        return ["index.html"]
    clean_relative = relative.rstrip("/")
    if requested_directory:
        return [f"{clean_relative}/index.html"]
    return [clean_relative, f"{clean_relative}/index.html"]


def _inventory(dist: Path) -> tuple[set[str], dict[str, _Document], list[ValidationIssue]]:
    files: set[str] = set()
    documents: dict[str, _Document] = {}
    issues: list[ValidationIssue] = []
    for path in sorted(dist.rglob("*")):
        relative_path = path.relative_to(dist).as_posix()
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode):
            issues.append(
                ValidationIssue(relative_path, "unsafe-file", "site output contains a symlink")
            )
            continue
        if not stat.S_ISREG(path_stat.st_mode):
            continue
        files.add(relative_path)
        if path.suffix.casefold() != ".html":
            continue
        document = _Document()
        try:
            document.feed(path.read_text(encoding="utf-8"))
            document.close()
        except (OSError, UnicodeError) as error:
            raise ValueError(f"unable to parse HTML file: {relative_path}") from error
        documents[relative_path] = document
    return files, documents, issues


def _document_issues(path: str, document: _Document) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if document.main_count != 1:
        issues.append(
            ValidationIssue(path, "main-count", "HTML page must contain exactly one main")
        )
    for _element_id, count in Counter(document.ids).items():
        if count > 1:
            issues.append(
                ValidationIssue(path, "duplicate-id", "HTML page contains a duplicate id")
            )
    return issues


def _reference_issues(
    source_path: str,
    reference: _Reference,
    base_path: str,
    files: set[str],
    documents: dict[str, _Document],
) -> list[ValidationIssue]:
    raw_reference = reference.value.strip()
    if raw_reference.startswith("///"):
        return [ValidationIssue(source_path, "invalid-url", "page contains an invalid URL")]
    try:
        parsed = urlsplit(raw_reference)
    except ValueError:
        return [ValidationIssue(source_path, "invalid-url", "page contains an invalid URL")]
    try:
        if parsed.query:
            _validate_query(parsed.query)
    except (UnicodeDecodeError, ValueError):
        return [ValidationIssue(source_path, "invalid-url", "page contains an invalid URL")]
    if parsed.scheme:
        if parsed.scheme.casefold() in _ALLOWED_EXTERNAL_SCHEMES:
            return []
        return [ValidationIssue(source_path, "invalid-url", "page contains an invalid URL")]
    if parsed.netloc:
        return []

    try:
        public_path, requested_directory = _normalize_public_path(
            source_path,
            parsed.path,
            base_path,
        )
    except (UnicodeDecodeError, ValueError):
        return [ValidationIssue(source_path, "invalid-url", "page contains an invalid URL")]
    if not _within_base_path(public_path, base_path):
        return [
            ValidationIssue(
                source_path,
                "base-path-escape",
                "internal URL escapes the configured base path",
            )
        ]

    candidates = _target_relative_paths(public_path, requested_directory, base_path)
    target_path = next((candidate for candidate in candidates if candidate in files), None)
    if target_path is None:
        return [
            ValidationIssue(
                source_path,
                "missing-target",
                "internal URL points to a missing page or asset",
            )
        ]
    if parsed.fragment:
        try:
            fragment = _decode_url_component(parsed.fragment)
        except (UnicodeDecodeError, ValueError):
            return [ValidationIssue(source_path, "invalid-url", "page contains an invalid URL")]
        target_document = documents.get(target_path)
        if target_document is not None and fragment not in target_document.ids:
            return [
                ValidationIssue(
                    source_path,
                    "missing-fragment",
                    "internal fragment does not exist in its target page",
                )
            ]
    return []


def validate_site(dist: Path, base_path: str) -> list[ValidationIssue]:
    """Return deterministic validation findings for one static site directory."""

    base_path = _validate_base_path(base_path)
    root = Path(dist)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("dist must be an existing non-symlink directory")
    root = root.resolve(strict=True)
    files, documents, issues = _inventory(root)
    if not documents:
        issues.append(ValidationIssue(".", "missing-html", "site contains no HTML pages"))
    for path, document in documents.items():
        issues.extend(_document_issues(path, document))
        for reference in document.references:
            issues.extend(_reference_issues(path, reference, base_path, files, documents))
    return sorted(issues)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    parser.add_argument("--base-path", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        issues = validate_site(arguments.dist, arguments.base_path)
    except (OSError, ValueError):
        print("site_validation_error: invalid site input", file=sys.stderr)
        return 2
    if not issues:
        return 0
    for issue in issues:
        print(issue.render(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
