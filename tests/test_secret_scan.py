from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts/check_no_secrets.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("check_no_secrets", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
check_no_secrets = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = check_no_secrets
SCRIPT_SPEC.loader.exec_module(check_no_secrets)
main = check_no_secrets.main
scan_repository = check_no_secrets.scan_repository


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
    )


def _tracked_repository(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    for name, content in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        _git(repository, "add", "--", name)
    return repository


def test_scan_reports_tracked_credentials_with_path_and_line_only(tmp_path: Path) -> None:
    environment_secret = "ANTHROPIC_AUTH_" + 'TOKEN = "real-value"'
    authorization_secret = "author" + "ization : bearer " + "'real-value'"
    repository = _tracked_repository(
        tmp_path,
        {"config.env": f"{environment_secret}\nbenign=true\n{authorization_secret}\n"},
    )

    findings = scan_repository(repository)

    assert [(finding.path, finding.line_number) for finding in findings] == [
        ("config.env", 1),
        ("config.env", 3),
    ]
    rendered = "\n".join(finding.render() for finding in findings)
    assert "config.env:1:" in rendered
    assert "config.env:3:" in rendered
    assert "real-value" not in rendered


def test_scan_ignores_placeholders_untracked_files_and_binary_data(tmp_path: Path) -> None:
    untracked_secret = "ANTHROPIC_AUTH_" + "TOKEN=real-value"
    binary_secret = b"\x00ANTHROPIC_AUTH_" + b"TOKEN=real-value"
    repository = _tracked_repository(
        tmp_path,
        {
            ".env.example": (
                "ANTHROPIC_AUTH_TOKEN=replace-with-a-repository-secret\n"
                "Author" + "ization: Bearer ${GITHUB_TOKEN}\n"
            ),
            "asset.bin": binary_secret,
            "odd name\twith\nnewline.txt": "safe tracked content\n",
        },
    )
    (repository / "untracked.env").write_text(untracked_secret, encoding="utf-8")

    assert scan_repository(repository) == []


def test_scan_ignores_documented_specification_placeholders_in_inline_code(
    tmp_path: Path,
) -> None:
    environment_reference = "`Author" + "ization: Bearer ${GITHUB_TOKEN}`"
    test_reference = "`Author" + "ization: Bearer test-secret`"
    repository = _tracked_repository(
        tmp_path,
        {"docs/superpowers/plans/example.md": (f"{environment_reference}\n{test_reference}.\n")},
    )

    assert scan_repository(repository) == []


def test_scan_does_not_exempt_source_files_with_secret_assignments(tmp_path: Path) -> None:
    source_secret = "ANTHROPIC_AUTH_" + "TOKEN='live-production-credential'"
    repository = _tracked_repository(tmp_path, {"src/settings.py": source_secret})

    findings = scan_repository(repository)

    assert [(finding.path, finding.line_number) for finding in findings] == [("src/settings.py", 1)]


def test_cli_exits_one_without_echoing_secret_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential = "Author" + "ization: " + "Bearer live-private-value"
    repository = _tracked_repository(tmp_path, {"headers.txt": credential})
    monkeypatch.chdir(repository)

    assert main([]) == 1

    captured = capsys.readouterr()
    assert "headers.txt:1:" in captured.err
    assert "live-private-value" not in captured.err
    assert captured.out == ""


def test_cli_exits_zero_for_clean_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _tracked_repository(tmp_path, {"README.md": "Nothing sensitive.\n"})
    monkeypatch.chdir(repository)

    assert main([]) == 0
    assert capsys.readouterr().err == ""
