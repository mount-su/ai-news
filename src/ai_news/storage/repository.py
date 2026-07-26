# ruff: noqa: RUF001

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_news.models import DailyReport, NewsItem

_MARKDOWN_CONTROL_PATTERN = re.compile(r"([\\`*_[\]{}()#+.!|>\-])")


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


def _markdown_path(root: Path, run_date: date) -> Path:
    return root / "content" / f"{run_date.isoformat()}.md"


def _index_path(root: Path) -> Path:
    return root / "data" / "index.json"


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


def _read_index(root: Path) -> _ReportIndex:
    path = _index_path(root)
    if not path.exists():
        return _ReportIndex(reports=[])
    if not path.is_file():
        raise ValueError("index path must be a regular file")
    return _ReportIndex.model_validate_json(path.read_bytes())


def _validated_report_path(root: Path, entry: _IndexEntry) -> Path:
    relative_path = PurePosixPath(entry.path)
    expected_path = _report_relative_path(entry.date)
    if relative_path.is_absolute() or relative_path.as_posix() != expected_path:
        raise ValueError(f"index path does not match report date for {entry.date.isoformat()}")

    target = root.joinpath(*relative_path.parts)
    data_root = (root / "data").resolve()
    resolved_target = target.resolve(strict=False)
    try:
        resolved_target.relative_to(data_root)
    except ValueError as error:
        raise ValueError(f"report path escapes data root for {entry.date.isoformat()}") from error
    if not target.is_file():
        raise ValueError(f"indexed report path is not a file for {entry.date.isoformat()}")
    return target


def _load_entry_report(root: Path, entry: _IndexEntry) -> DailyReport:
    path = _validated_report_path(root, entry)
    report = DailyReport.model_validate_json(path.read_bytes())
    if report.date != entry.date:
        raise ValueError(f"index date does not match report date for {entry.date.isoformat()}")
    if len(report.items) != entry.item_count:
        raise ValueError(f"index item_count does not match report for {entry.date.isoformat()}")
    return report


def _prepare_file(target: Path, content: bytes, suffix: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=suffix,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary_path
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _fsync_directories(paths: set[Path]) -> None:
    for path in sorted(paths, key=str):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _cleanup_files(paths: list[Path | None]) -> None:
    for path in paths:
        if path is not None:
            path.unlink(missing_ok=True)


def _commit_files(target_contents: list[tuple[Path, bytes]]) -> None:
    temporary_files: list[Path] = []
    prepared_writes: list[_PreparedWrite] = []
    replaced_writes: list[_PreparedWrite] = []
    directories = {target.parent for target, _ in target_contents}
    try:
        for target, content in target_contents:
            temporary_files.append(_prepare_file(target, content, ".tmp"))

        for (target, _), temporary in zip(
            target_contents,
            temporary_files,
            strict=True,
        ):
            existed = target.exists()
            backup = _prepare_file(target, target.read_bytes(), ".bak") if existed else None
            prepared_writes.append(
                _PreparedWrite(
                    target=target,
                    temporary=temporary,
                    backup=backup,
                    existed=existed,
                )
            )

        try:
            for prepared in prepared_writes:
                prepared.temporary.replace(prepared.target)
                replaced_writes.append(prepared)
            _fsync_directories(directories)
        except BaseException as commit_error:
            rollback_error: BaseException | None = None
            for prepared in reversed(replaced_writes):
                try:
                    if prepared.existed:
                        if prepared.backup is None:
                            raise RuntimeError("transaction backup is missing")
                        os.replace(prepared.backup, prepared.target)
                    else:
                        prepared.target.unlink(missing_ok=True)
                except BaseException as error:
                    rollback_error = rollback_error or error
            try:
                _fsync_directories(directories)
            except BaseException as error:
                rollback_error = rollback_error or error
            if rollback_error is not None:
                raise RuntimeError("report transaction rollback failed") from rollback_error
            raise commit_error
    finally:
        _cleanup_files([*temporary_files, *(prepared.backup for prepared in prepared_writes)])


def save_report(root: Path, report: DailyReport) -> None:
    root = Path(root)
    existing_index = _read_index(root)
    for entry in existing_index.reports:
        _load_entry_report(root, entry)

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
    _commit_files(
        [
            (_report_path(root, report.date), report_content),
            (_markdown_path(root, report.date), markdown_content),
            (_index_path(root), index_content),
        ]
    )


def load_reports(root: Path) -> list[DailyReport]:
    root = Path(root)
    index = _read_index(root)
    entries = sorted(index.reports, key=lambda entry: entry.date, reverse=True)
    return [_load_entry_report(root, entry) for entry in entries]


def load_history_urls(
    root: Path,
    run_date: date,
    days: int = 30,
) -> set[str]:
    if isinstance(days, bool) or days < 1:
        raise ValueError("days must be at least 1")

    root = Path(root)
    index = _read_index(root)
    window_start = run_date - timedelta(days=days)
    entries = [entry for entry in index.reports if window_start <= entry.date < run_date]
    reports = [_load_entry_report(root, entry) for entry in entries]
    return {str(item.canonical_url) for report in reports for item in report.items}
