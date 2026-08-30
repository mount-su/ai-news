# ruff: noqa: RUF001

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_news.models import DailyReport, NewsItem, RunRecord, RunStatus

_MARKDOWN_CONTROL_PATTERN = re.compile(r"([\\`*_[\]{}()#+.!|>\-])")
_REPORT_TARGET_PATTERN = re.compile(
    r"^data/(?P<year>[0-9]{4})/(?P<month>[0-9]{2})/"
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\.json$"
)
_RUN_RECORD_TARGET_PATTERN = re.compile(
    r"^data/runs/(?P<year>[0-9]{4})/(?P<month>[0-9]{2})/"
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\.json$"
)
_TRANSACTION_JOURNAL_NAME = ".ai-news-transaction.json"
_TRANSACTION_ID_FRAGMENT = r"(?P<transaction_id>[0-9a-f]{32})"
_ARTIFACT_SUFFIX_FRAGMENT = r"(?P<suffix>tmp|bak|recover\.tmp)"
_REPORT_ARTIFACT_PATTERN = re.compile(
    r"^\.(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\.json\."
    + _TRANSACTION_ID_FRAGMENT
    + r"\."
    + _ARTIFACT_SUFFIX_FRAGMENT
    + r"$"
)
_RUN_RECORD_ARTIFACT_PATTERN = re.compile(
    r"^\.(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\.json\."
    + _TRANSACTION_ID_FRAGMENT
    + r"\."
    + _ARTIFACT_SUFFIX_FRAGMENT
    + r"$"
)
_CONTENT_ARTIFACT_PATTERN = re.compile(
    r"^\.(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\.md\."
    + _TRANSACTION_ID_FRAGMENT
    + r"\."
    + _ARTIFACT_SUFFIX_FRAGMENT
    + r"$"
)
_INDEX_ARTIFACT_PATTERN = re.compile(
    r"^\.index\.json\." + _TRANSACTION_ID_FRAGMENT + r"\." + _ARTIFACT_SUFFIX_FRAGMENT + r"$"
)
_JOURNAL_TEMPORARY_PATTERN = re.compile(
    r"^\." + re.escape(_TRANSACTION_JOURNAL_NAME) + r"\." + _TRANSACTION_ID_FRAGMENT + r"\.tmp$"
)
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class _IndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    item_count: int = Field(ge=0)
    path: str = Field(min_length=1)


class _ReportIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reports: list[_IndexEntry]

    @model_validator(mode="after")
    def require_unique_dates(self) -> _ReportIndex:
        dates = [entry.date for entry in self.reports]
        if len(dates) != len(set(dates)):
            raise ValueError("index report dates must be unique")
        return self


class _JournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1)
    temporary: str = Field(min_length=1)
    backup: str | None
    existed: bool


class _TransactionJournal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1.0", "2.0"] = "1.0"
    transaction_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    entries: list[_JournalEntry] = Field(min_length=3, max_length=4)

    @model_validator(mode="after")
    def require_fixed_transaction_paths(self) -> _TransactionJournal:
        targets = [entry.target for entry in self.entries]
        if len(targets) != len(set(targets)):
            raise ValueError("transaction target paths must be unique")

        report_entries = [
            entry for entry in self.entries if _REPORT_TARGET_PATTERN.fullmatch(entry.target)
        ]
        if len(report_entries) != 1:
            raise ValueError("transaction must contain one canonical report target")
        report_match = _REPORT_TARGET_PATTERN.fullmatch(report_entries[0].target)
        if report_match is None:
            raise ValueError("transaction report target is invalid")
        try:
            report_date = date.fromisoformat(report_match.group("date"))
        except ValueError as error:
            raise ValueError("transaction report date is invalid") from error
        if (
            report_match.group("year") != f"{report_date:%Y}"
            or report_match.group("month") != f"{report_date:%m}"
        ):
            raise ValueError("transaction report path does not match its date")

        expected_targets = [
            _report_relative_path(report_date),
            f"content/{report_date.isoformat()}.md",
            "data/index.json",
        ]
        if self.version == "2.0":
            expected_targets.append(_run_record_relative_path(report_date))
        if targets != expected_targets:
            raise ValueError("transaction targets must use the fixed storage paths")

        for entry in self.entries:
            expected_temporary = _artifact_relative_path(
                entry.target,
                self.transaction_id,
                "tmp",
            )
            if entry.temporary != expected_temporary:
                raise ValueError("transaction temporary path is invalid")
            expected_backup = (
                _artifact_relative_path(entry.target, self.transaction_id, "bak")
                if entry.existed
                else None
            )
            if entry.backup != expected_backup:
                raise ValueError("transaction backup path is invalid")
        return self


@dataclass(frozen=True)
class _PreparedWrite:
    target: Path
    temporary: Path
    backup: Path | None
    existed: bool


def _report_relative_path(run_date: date) -> str:
    return f"data/{run_date:%Y/%m}/{run_date.isoformat()}.json"


def _report_path(root: Path, run_date: date) -> Path:
    return root / _report_relative_path(run_date)


def _run_record_relative_path(run_date: date) -> str:
    return f"data/runs/{run_date:%Y/%m}/{run_date.isoformat()}.json"


def _run_record_path(root: Path, run_date: date) -> Path:
    return root / _run_record_relative_path(run_date)


def _markdown_path(root: Path, run_date: date) -> Path:
    return root / "content" / f"{run_date.isoformat()}.md"


def _index_path(root: Path) -> Path:
    return root / "data" / "index.json"


def _journal_path(root: Path) -> Path:
    return root / _TRANSACTION_JOURNAL_NAME


def _artifact_relative_path(target: str, transaction_id: str, suffix: str) -> str:
    target_path = PurePosixPath(target)
    artifact_name = f".{target_path.name}.{transaction_id}.{suffix}"
    return (target_path.parent / artifact_name).as_posix()


def _path_from_relative(root: Path, relative_path: str) -> Path:
    return root.joinpath(*PurePosixPath(relative_path).parts)


def _root_boundary(root: Path) -> Path:
    try:
        return root.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("storage root cannot be resolved safely") from error


def _repository_identity(root_boundary: Path) -> str:
    return hashlib.sha256(str(root_boundary).encode("utf-8")).hexdigest()


def _thread_lock(identity: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(identity, threading.RLock())


def _lock_directory() -> Path:
    path = Path(tempfile.gettempdir()) / f"ai-news-radar-{os.getuid()}-locks"
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        path_stat = path.lstat()
    except OSError as error:
        raise RuntimeError("repository lock directory is unavailable") from error
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
    ):
        raise RuntimeError("repository lock directory is unsafe")
    path.chmod(0o700)
    return path


@contextmanager
def _repository_lock(root_boundary: Path) -> Iterator[None]:
    identity = _repository_identity(root_boundary)
    thread_lock = _thread_lock(identity)
    with thread_lock:
        lock_path = _lock_directory() / f"{identity}.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise RuntimeError("repository lock file is unavailable") from error
        try:
            lock_stat = os.fstat(descriptor)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
                raise RuntimeError("repository lock file is unsafe")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _require_within_root(root_boundary: Path, path: Path) -> Path:
    try:
        resolved_path = path.resolve(strict=False)
        resolved_path.relative_to(root_boundary)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("storage path escapes resolved root") from error
    return path


def _require_storage_target(root_boundary: Path, path: Path) -> Path:
    _require_within_root(root_boundary, path)
    if path.is_symlink():
        raise ValueError("storage path must not be a leaf symlink")
    return path


def _json_text(value: object) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _safe_markdown_text(value: object) -> str:
    single_line = " ".join(str(value).split())
    html_escaped = html.escape(single_line, quote=False)
    return _MARKDOWN_CONTROL_PATTERN.sub(r"\\\1", html_escaped)


def _markdown_url(item: NewsItem) -> str:
    return (
        str(item.canonical_url)
        .replace("\\", "%5C")
        .replace("(", "%28")
        .replace(")", "%29")
        .replace("<", "%3C")
        .replace(">", "%3E")
        .replace(" ", "%20")
    )


def _render_item(item: NewsItem, position: int) -> list[str]:
    source = _safe_markdown_text(item.source)
    tags = "、".join(_safe_markdown_text(tag) for tag in item.tags)
    return [
        f"### {position}. {_safe_markdown_text(item.title)}",
        "",
        f"- 原始标题：{_safe_markdown_text(item.original_title)}",
        f"- 分类：{_safe_markdown_text(item.category.value)}",
        f"- 来源：[{source}]({_markdown_url(item)})",
        f"- 发布时间：{_safe_markdown_text(item.published_at.isoformat())}",
        f"- 摘要：{_safe_markdown_text(item.summary)}",
        f"- 重要性：{item.importance}/10",
        f"- 价值：{_safe_markdown_text(item.why_it_matters)}",
        f"- 标签：{tags}",
        f"- 官方信息：{'是' if item.is_official else '否'}",
        f"- 营销风险：{_safe_markdown_text(item.marketing_risk)}",
        f"- 跟踪信号：{_safe_markdown_text(item.tracking_signal)}",
        "",
    ]


def _render_markdown(report: DailyReport) -> str:
    lines = [
        f"# AI 资讯日报｜{report.date.isoformat()}",
        "",
        f"- 生成时间：{_safe_markdown_text(report.generated_at.isoformat())}",
        f"- 分析模型：{_safe_markdown_text(report.model)}",
        f"- 候选条目：{report.candidate_count}",
        f"- 降级模式：{'是' if report.degraded else '否'}",
        "",
        "## 今日最重要",
        "",
    ]
    if report.items:
        for position, item in enumerate(report.items[:3], start=1):
            lines.extend(_render_item(item, position))
    else:
        lines.extend(["暂无条目。", ""])

    lines.extend(["## 全部条目", ""])
    if report.items:
        for position, item in enumerate(report.items, start=1):
            lines.extend(_render_item(item, position))
    else:
        lines.extend(["暂无条目。", ""])
    return "\n".join(lines).rstrip() + "\n"


def _read_index(root: Path, root_boundary: Path) -> _ReportIndex:
    path = _require_storage_target(root_boundary, _index_path(root))
    if not path.exists():
        return _ReportIndex(reports=[])
    if not path.is_file():
        raise ValueError("index path must be a regular file")
    _require_storage_target(root_boundary, path)
    return _ReportIndex.model_validate_json(path.read_bytes())


def _validated_report_path(
    root: Path,
    root_boundary: Path,
    entry: _IndexEntry,
) -> Path:
    relative_path = PurePosixPath(entry.path)
    expected_path = _report_relative_path(entry.date)
    if relative_path.is_absolute() or relative_path.as_posix() != expected_path:
        raise ValueError(f"index path does not match report date for {entry.date.isoformat()}")

    target = _require_storage_target(
        root_boundary,
        root.joinpath(*relative_path.parts),
    )
    if not target.is_file():
        raise ValueError(f"indexed report path is not a file for {entry.date.isoformat()}")
    _require_storage_target(root_boundary, target)
    return target


def _load_entry_report(
    root: Path,
    root_boundary: Path,
    entry: _IndexEntry,
) -> DailyReport:
    path = _validated_report_path(root, root_boundary, entry)
    report = DailyReport.model_validate_json(path.read_bytes())
    if report.date != entry.date:
        raise ValueError(f"index date does not match report date for {entry.date.isoformat()}")
    if len(report.items) != entry.item_count:
        raise ValueError(f"index item_count does not match report for {entry.date.isoformat()}")
    return report


def _ensure_directory(root_boundary: Path, directory: Path) -> None:
    _require_within_root(root_boundary, directory)
    missing_directories: list[Path] = []
    current = directory
    while not current.exists():
        missing_directories.append(current)
        if current.resolve(strict=False) == root_boundary:
            break
        parent = current.parent
        if parent == current:
            raise ValueError("storage directory cannot be created within root")
        current = parent

    if current.exists() and not current.is_dir():
        raise ValueError("storage parent must be a directory")

    for path in reversed(missing_directories):
        path.mkdir()
        _require_within_root(root_boundary, path)
        try:
            _require_within_root(root_boundary, path.parent)
        except ValueError:
            _fsync_directories(root_boundary, {path})
        else:
            _fsync_directories(root_boundary, {path.parent})


def _write_artifact(root_boundary: Path, path: Path, content: bytes) -> None:
    _require_within_root(root_boundary, path)
    _ensure_directory(root_boundary, path.parent)
    _require_within_root(root_boundary, path)
    if path.exists() or path.is_symlink():
        raise RuntimeError("transaction artifact path already exists")
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if path.is_file() and not path.is_symlink():
            path.unlink(missing_ok=True)
        raise


def _write_private_atomic(root_boundary: Path, target: Path, content: bytes) -> None:
    _require_storage_target(root_boundary, target)
    _ensure_directory(root_boundary, target.parent)
    _require_storage_target(root_boundary, target)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    _require_within_root(root_boundary, temporary)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _require_storage_target(root_boundary, target)
        temporary.replace(target)
        _fsync_directories(root_boundary, {target.parent})
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink(missing_ok=True)
            _fsync_directories(root_boundary, {temporary.parent})
        raise


def _fsync_directories(root_boundary: Path, paths: set[Path]) -> None:
    for path in sorted(paths, key=str):
        _require_within_root(root_boundary, path)
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _cleanup_files(root_boundary: Path, paths: list[Path | None]) -> None:
    for path in paths:
        if path is not None and (path.exists() or path.is_symlink()):
            _require_within_root(root_boundary, path)
            if path.is_symlink() or not path.is_file():
                raise ValueError("transaction artifact must be a regular file")
            path.unlink(missing_ok=True)


def _scan_directory(root_boundary: Path, directory: Path) -> list[Path]:
    if not directory.exists() and not directory.is_symlink():
        return []
    if directory.is_symlink():
        raise ValueError("orphan artifact directory under storage root must not be a symlink")
    _require_within_root(root_boundary, directory)
    if not directory.is_dir():
        raise ValueError("orphan artifact parent must be a directory")
    return list(directory.iterdir())


def _require_regular_orphan(root_boundary: Path, path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("orphan transaction artifact must not be a symlink")
    _require_within_root(root_boundary, path)
    if not path.is_file():
        raise ValueError("orphan transaction artifact must be a regular file")
    return path


def _valid_artifact_date(
    value: str,
    *,
    year: str | None = None,
    month: str | None = None,
) -> bool:
    try:
        artifact_date = date.fromisoformat(value)
    except ValueError:
        return False
    return (
        artifact_date.isoformat() == value
        and (year is None or f"{artifact_date:%Y}" == year)
        and (month is None or f"{artifact_date:%m}" == month)
    )


def _collect_orphan_artifacts(root_boundary: Path) -> list[Path]:
    candidates: list[Path] = []
    root_entries = _scan_directory(root_boundary, root_boundary)
    for entry in root_entries:
        if _JOURNAL_TEMPORARY_PATTERN.fullmatch(entry.name):
            candidates.append(_require_regular_orphan(root_boundary, entry))

    data_directory = root_boundary / "data"
    data_entries = _scan_directory(root_boundary, data_directory)
    for entry in data_entries:
        if _INDEX_ARTIFACT_PATTERN.fullmatch(entry.name):
            candidates.append(_require_regular_orphan(root_boundary, entry))

    content_directory = root_boundary / "content"
    for entry in _scan_directory(root_boundary, content_directory):
        match = _CONTENT_ARTIFACT_PATTERN.fullmatch(entry.name)
        if match and _valid_artifact_date(match.group("date")):
            candidates.append(_require_regular_orphan(root_boundary, entry))

    for year_entry in data_entries:
        if re.fullmatch(r"[0-9]{4}", year_entry.name) is None:
            continue
        for month_entry in _scan_directory(root_boundary, year_entry):
            if re.fullmatch(r"[0-9]{2}", month_entry.name) is None:
                continue
            for entry in _scan_directory(root_boundary, month_entry):
                match = _REPORT_ARTIFACT_PATTERN.fullmatch(entry.name)
                if match and _valid_artifact_date(
                    match.group("date"),
                    year=year_entry.name,
                    month=month_entry.name,
                ):
                    candidates.append(_require_regular_orphan(root_boundary, entry))

    run_records_directory = data_directory / "runs"
    for year_entry in _scan_directory(root_boundary, run_records_directory):
        if re.fullmatch(r"[0-9]{4}", year_entry.name) is None:
            continue
        for month_entry in _scan_directory(root_boundary, year_entry):
            if re.fullmatch(r"[0-9]{2}", month_entry.name) is None:
                continue
            for entry in _scan_directory(root_boundary, month_entry):
                match = _RUN_RECORD_ARTIFACT_PATTERN.fullmatch(entry.name)
                if match and _valid_artifact_date(
                    match.group("date"),
                    year=year_entry.name,
                    month=month_entry.name,
                ):
                    candidates.append(_require_regular_orphan(root_boundary, entry))
    return candidates


def _sweep_orphan_artifacts(root_boundary: Path) -> None:
    if not root_boundary.exists():
        return
    candidates = _collect_orphan_artifacts(root_boundary)
    if not candidates:
        return
    _cleanup_files(root_boundary, list(candidates))
    _fsync_directories(
        root_boundary,
        {candidate.parent for candidate in candidates},
    )


def _read_journal(root: Path, root_boundary: Path) -> _TransactionJournal | None:
    path = _require_storage_target(root_boundary, _journal_path(root))
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("transaction journal must be a regular file")
    _require_storage_target(root_boundary, path)
    return _TransactionJournal.model_validate_json(path.read_bytes())


def _publish_journal(
    root: Path,
    root_boundary: Path,
    journal: _TransactionJournal,
) -> None:
    path = _require_storage_target(root_boundary, _journal_path(root))
    if path.exists():
        raise RuntimeError("a report transaction is already pending")
    temporary = root / f".{_TRANSACTION_JOURNAL_NAME}.{journal.transaction_id}.tmp"
    _write_artifact(
        root_boundary,
        temporary,
        _json_text(journal.model_dump(mode="json")).encode("utf-8"),
    )
    try:
        temporary.replace(path)
    except BaseException:
        _cleanup_files(root_boundary, [temporary])
        _fsync_directories(root_boundary, {temporary.parent})
        raise
    _fsync_directories(root_boundary, {path.parent})


def _journal_prepared_writes(
    root: Path,
    root_boundary: Path,
    journal: _TransactionJournal,
) -> list[_PreparedWrite]:
    prepared: list[_PreparedWrite] = []
    for entry in journal.entries:
        target = _require_storage_target(
            root_boundary,
            _path_from_relative(root, entry.target),
        )
        temporary = _require_within_root(
            root_boundary,
            _path_from_relative(root, entry.temporary),
        )
        backup = (
            _require_within_root(
                root_boundary,
                _path_from_relative(root, entry.backup),
            )
            if entry.backup is not None
            else None
        )
        prepared.append(
            _PreparedWrite(
                target=target,
                temporary=temporary,
                backup=backup,
                existed=entry.existed,
            )
        )
    return prepared


def _recovery_temporary(
    root: Path,
    root_boundary: Path,
    journal: _TransactionJournal,
    entry: _JournalEntry,
) -> Path:
    relative_path = _artifact_relative_path(
        entry.target,
        journal.transaction_id,
        "recover.tmp",
    )
    return _require_within_root(
        root_boundary,
        _path_from_relative(root, relative_path),
    )


def _cleanup_transaction_artifacts(
    root: Path,
    root_boundary: Path,
    journal: _TransactionJournal,
    prepared_writes: list[_PreparedWrite],
) -> None:
    recovery_temporaries = [
        _recovery_temporary(root, root_boundary, journal, entry) for entry in journal.entries
    ]
    artifact_paths = [
        *(prepared.temporary for prepared in prepared_writes),
        *(prepared.backup for prepared in prepared_writes),
        *recovery_temporaries,
    ]
    _cleanup_files(root_boundary, artifact_paths)
    artifact_directories = {path.parent for path in artifact_paths if path is not None}
    if artifact_directories:
        _fsync_directories(root_boundary, artifact_directories)


def _recover_pending_transaction(root: Path, root_boundary: Path) -> None:
    journal = _read_journal(root, root_boundary)
    if journal is None:
        _sweep_orphan_artifacts(root_boundary)
        return

    prepared_writes = _journal_prepared_writes(root, root_boundary, journal)
    target_directories = {prepared.target.parent for prepared in prepared_writes}
    for entry, prepared in zip(journal.entries, prepared_writes, strict=True):
        _require_storage_target(root_boundary, prepared.target)
        recovery_temporary = _recovery_temporary(
            root,
            root_boundary,
            journal,
            entry,
        )
        if recovery_temporary.exists() or recovery_temporary.is_symlink():
            _cleanup_files(root_boundary, [recovery_temporary])
            _fsync_directories(root_boundary, {recovery_temporary.parent})

        if prepared.existed:
            if prepared.backup is None or not prepared.backup.is_file():
                raise ValueError("transaction backup is missing")
            if prepared.backup.is_symlink():
                raise ValueError("transaction backup must not be a symlink")
            _require_within_root(root_boundary, prepared.backup)
            _write_artifact(
                root_boundary,
                recovery_temporary,
                prepared.backup.read_bytes(),
            )
            recovery_temporary.replace(prepared.target)
        elif prepared.target.exists():
            prepared.target.unlink()

    _fsync_directories(root_boundary, target_directories)
    journal_path = _require_storage_target(root_boundary, _journal_path(root))
    journal_path.unlink()
    _fsync_directories(root_boundary, {journal_path.parent})
    _cleanup_transaction_artifacts(
        root,
        root_boundary,
        journal,
        prepared_writes,
    )
    _sweep_orphan_artifacts(root_boundary)


def _commit_files(
    root: Path,
    root_boundary: Path,
    target_contents: list[tuple[Path, bytes]],
    *,
    journal_version: Literal["1.0", "2.0"] = "1.0",
) -> None:
    transaction_id = uuid4().hex
    prepared_writes: list[_PreparedWrite] = []
    created_artifacts: list[Path | None] = []
    directories = {target.parent for target, _ in target_contents}
    try:
        for target, _ in target_contents:
            _require_storage_target(root_boundary, target)

        for target, content in target_contents:
            target_relative = target.relative_to(root).as_posix()
            temporary = _path_from_relative(
                root,
                _artifact_relative_path(target_relative, transaction_id, "tmp"),
            )
            _write_artifact(root_boundary, temporary, content)
            created_artifacts.append(temporary)
            _require_storage_target(root_boundary, target)
            existed = target.exists()
            backup: Path | None = None
            if existed:
                backup = _path_from_relative(
                    root,
                    _artifact_relative_path(target_relative, transaction_id, "bak"),
                )
                _write_artifact(root_boundary, backup, target.read_bytes())
                created_artifacts.append(backup)
            prepared_writes.append(
                _PreparedWrite(
                    target=target,
                    temporary=temporary,
                    backup=backup,
                    existed=existed,
                )
            )

        _fsync_directories(root_boundary, directories)
        journal = _TransactionJournal(
            version=journal_version,
            transaction_id=transaction_id,
            entries=[
                _JournalEntry(
                    target=prepared.target.relative_to(root).as_posix(),
                    temporary=prepared.temporary.relative_to(root).as_posix(),
                    backup=(
                        prepared.backup.relative_to(root).as_posix()
                        if prepared.backup is not None
                        else None
                    ),
                    existed=prepared.existed,
                )
                for prepared in prepared_writes
            ],
        )
        _publish_journal(root, root_boundary, journal)

        try:
            for prepared in prepared_writes:
                _require_storage_target(root_boundary, prepared.target)
                _require_within_root(root_boundary, prepared.temporary)
                prepared.temporary.replace(prepared.target)
            _fsync_directories(root_boundary, directories)
        except BaseException as commit_error:
            try:
                _recover_pending_transaction(root, root_boundary)
            except BaseException as recovery_error:
                raise RuntimeError("report transaction rollback failed") from recovery_error
            raise commit_error

        journal_path = _require_storage_target(root_boundary, _journal_path(root))
        journal_path.unlink()
        _fsync_directories(root_boundary, {journal_path.parent})
        _cleanup_transaction_artifacts(
            root,
            root_boundary,
            journal,
            prepared_writes,
        )
    except BaseException:
        if not _journal_path(root).exists():
            _cleanup_files(root_boundary, created_artifacts)
            existing_directories = {
                path.parent
                for path in created_artifacts
                if path is not None and path.parent.exists()
            }
            if existing_directories:
                _fsync_directories(root_boundary, existing_directories)
        raise


def _save_report_locked(
    root: Path,
    root_boundary: Path,
    report: DailyReport,
    published_record: RunRecord | None = None,
) -> None:
    report_target = _report_path(root, report.date)
    markdown_target = _markdown_path(root, report.date)
    index_target = _index_path(root)
    run_record_target = (
        _run_record_path(root, report.date) if published_record is not None else None
    )
    for target in (report_target, markdown_target, index_target, run_record_target):
        if target is None:
            continue
        _require_storage_target(root_boundary, target)

    _recover_pending_transaction(root, root_boundary)
    existing_index = _read_index(root, root_boundary)
    for entry in existing_index.reports:
        _load_entry_report(root, root_boundary, entry)

    replacement = _IndexEntry(
        date=report.date,
        item_count=len(report.items),
        path=_report_relative_path(report.date),
    )
    retained_entries = [entry for entry in existing_index.reports if entry.date != report.date]
    index = _ReportIndex(
        reports=sorted(
            [*retained_entries, replacement],
            key=lambda entry: entry.date,
            reverse=True,
        )
    )

    report_content = _json_text(report.model_dump(mode="json")).encode("utf-8")
    markdown_content = _render_markdown(report).encode("utf-8")
    index_content = _json_text(index.model_dump(mode="json")).encode("utf-8")
    target_contents = [
        (report_target, report_content),
        (markdown_target, markdown_content),
        (index_target, index_content),
    ]
    if published_record is not None and run_record_target is not None:
        target_contents.append(
            (
                run_record_target,
                _json_text(published_record.model_dump(mode="json")).encode("utf-8"),
            )
        )
    _commit_files(
        root,
        root_boundary,
        target_contents,
        journal_version="2.0" if published_record is not None else "1.0",
    )


def save_report(root: Path, report: DailyReport) -> None:
    root = Path(root)
    report = DailyReport.model_validate_json(report.model_dump_json())
    root_boundary = _root_boundary(root)
    with _repository_lock(root_boundary):
        if _root_boundary(root) != root_boundary:
            raise ValueError("storage root changed while acquiring its lock")
        _save_report_locked(root, root_boundary, report)


def save_publication(root: Path, report: DailyReport, record: RunRecord) -> None:
    root = Path(root)
    report = DailyReport.model_validate_json(report.model_dump_json())
    record = RunRecord.model_validate_json(record.model_dump_json())
    if record.date != report.date:
        raise ValueError("published run record date must match report date")
    if record.status.value != "published":
        raise ValueError("publication requires a published run record")
    if record.source_runs != report.source_runs:
        raise ValueError("published run record source_runs must match report source_runs")

    root_boundary = _root_boundary(root)
    with _repository_lock(root_boundary):
        if _root_boundary(root) != root_boundary:
            raise ValueError("storage root changed while acquiring its lock")
        _save_report_locked(root, root_boundary, report, published_record=record)


def save_run_record(root: Path, record: RunRecord) -> None:
    root = Path(root)
    record = RunRecord.model_validate_json(record.model_dump_json())
    root_boundary = _root_boundary(root)
    with _repository_lock(root_boundary):
        if _root_boundary(root) != root_boundary:
            raise ValueError("storage root changed while acquiring its lock")
        _recover_pending_transaction(root, root_boundary)
        target = _run_record_path(root, record.date)
        _write_private_atomic(
            root_boundary,
            target,
            _json_text(record.model_dump(mode="json")).encode("utf-8"),
        )


def _load_run_records_locked(root: Path, root_boundary: Path) -> list[RunRecord]:
    records_root = _require_storage_target(root_boundary, root / "data/runs")
    if not records_root.exists():
        return []
    if not records_root.is_dir():
        raise ValueError("run records path must be a directory")

    records: list[RunRecord] = []
    for year_directory in sorted(records_root.iterdir()):
        if year_directory.is_symlink() or not year_directory.is_dir():
            raise ValueError("run record year path must be a regular directory")
        if re.fullmatch(r"[0-9]{4}", year_directory.name) is None:
            raise ValueError("run record year path is invalid")
        _require_within_root(root_boundary, year_directory)
        for month_directory in sorted(year_directory.iterdir()):
            if month_directory.is_symlink() or not month_directory.is_dir():
                raise ValueError("run record month path must be a regular directory")
            if re.fullmatch(r"[0-9]{2}", month_directory.name) is None:
                raise ValueError("run record month path is invalid")
            _require_within_root(root_boundary, month_directory)
            for path in sorted(month_directory.iterdir()):
                relative_path = path.relative_to(root).as_posix()
                match = _RUN_RECORD_TARGET_PATTERN.fullmatch(relative_path)
                if match is None or path.is_symlink() or not path.is_file():
                    raise ValueError("run record path is invalid")
                _require_within_root(root_boundary, path)
                try:
                    path_date = date.fromisoformat(match.group("date"))
                except ValueError as error:
                    raise ValueError("run record path date is invalid") from error
                if (
                    match.group("year") != f"{path_date:%Y}"
                    or match.group("month") != f"{path_date:%m}"
                ):
                    raise ValueError("run record path does not match its date")
                record = RunRecord.model_validate_json(path.read_bytes())
                if record.date != path_date:
                    raise ValueError("run record path does not match record date")
                records.append(record)
    return sorted(records, key=lambda record: record.date, reverse=True)


def load_run_records(root: Path) -> list[RunRecord]:
    root = Path(root)
    root_boundary = _root_boundary(root)
    with _repository_lock(root_boundary):
        if _root_boundary(root) != root_boundary:
            raise ValueError("storage root changed while acquiring its lock")
        _recover_pending_transaction(root, root_boundary)
        return _load_run_records_locked(root, root_boundary)


def active_reports(
    reports: Sequence[DailyReport],
    run_records: Sequence[RunRecord],
) -> list[DailyReport]:
    report_dates = [report.date for report in reports]
    record_by_date = {record.date: record for record in run_records}
    if len(report_dates) != len(set(report_dates)) or len(record_by_date) != len(run_records):
        raise ValueError("active report inputs must contain unique dates")
    return [
        report
        for report in reports
        if record_by_date.get(report.date) is None
        or record_by_date[report.date].status is not RunStatus.NO_ELIGIBLE_CONTENT
    ]


def load_reports(root: Path) -> list[DailyReport]:
    root = Path(root)
    root_boundary = _root_boundary(root)
    with _repository_lock(root_boundary):
        if _root_boundary(root) != root_boundary:
            raise ValueError("storage root changed while acquiring its lock")
        _recover_pending_transaction(root, root_boundary)
        index = _read_index(root, root_boundary)
        entries = sorted(index.reports, key=lambda entry: entry.date, reverse=True)
        return [_load_entry_report(root, root_boundary, entry) for entry in entries]


def load_history_urls(
    root: Path,
    run_date: date,
    days: int = 30,
) -> set[str]:
    if isinstance(days, bool) or days < 1:
        raise ValueError("days must be at least 1")

    root = Path(root)
    root_boundary = _root_boundary(root)
    with _repository_lock(root_boundary):
        if _root_boundary(root) != root_boundary:
            raise ValueError("storage root changed while acquiring its lock")
        _recover_pending_transaction(root, root_boundary)
        index = _read_index(root, root_boundary)
        window_start = run_date - timedelta(days=days)
        entries = [entry for entry in index.reports if window_start <= entry.date < run_date]
        reports = [_load_entry_report(root, root_boundary, entry) for entry in entries]
        reports = active_reports(reports, _load_run_records_locked(root, root_boundary))
        return {str(item.canonical_url) for report in reports for item in report.items}
