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


@pytest.mark.parametrize(
    "key_name",
    [
        "ANTHROPIC_AUTH_" + "TOKEN",
        "LLM_API_" + "KEY",
    ],
)
@pytest.mark.parametrize(
    "assignment_template",
    [
        "{key}: live-yaml-secret",
        "{key} = live-env-secret",
        '"{key}" : "live-json-secret"',
        "'{lower_key}': 'live-mixed-secret'",
    ],
)
def test_scan_detects_both_credential_keys_with_supported_assignment_forms(
    tmp_path: Path,
    key_name: str,
    assignment_template: str,
) -> None:
    assignment_line = assignment_template.format(
        key=key_name,
        lower_key=key_name.lower(),
    )
    repository = _tracked_repository(tmp_path, {"settings.txt": assignment_line})

    findings = scan_repository(repository)

    assert [(finding.path, finding.line_number, finding.rule) for finding in findings] == [
        ("settings.txt", 1, "credential assignment")
    ]
    assert "live-" not in findings[0].render()


def test_scan_does_not_match_generic_key_as_identifier_substring(tmp_path: Path) -> None:
    repository = _tracked_repository(
        tmp_path,
        {"settings.env": "MY_LLM_API_" + "KEY_BACKUP=live-but-unrelated-value"},
    )

    assert scan_repository(repository) == []


def test_scan_does_not_match_generic_key_after_unicode_identifier_character(
    tmp_path: Path,
) -> None:
    repository = _tracked_repository(
        tmp_path,
        {"settings.env": "éLLM_API_" + "KEY=live-but-unrelated-value"},
    )

    assert scan_repository(repository) == []


def test_scan_does_not_treat_equality_comparison_as_assignment(tmp_path: Path) -> None:
    comparison = "ANTHROPIC_AUTH_" + 'TOKEN == "not-an-assignment"'
    repository = _tracked_repository(tmp_path, {"condition.py": comparison})

    assert scan_repository(repository) == []


def test_finding_render_escapes_control_characters_in_path(tmp_path: Path) -> None:
    assignment = "ANTHROPIC_AUTH_" + "TOKEN=live-control-path-secret"
    repository = _tracked_repository(tmp_path, {"unsafe\x1b[2J.env": assignment})

    findings = scan_repository(repository)

    assert len(findings) == 1
    rendered = findings[0].render()
    assert "\x1b" not in rendered
    assert "\\u001b" in rendered
    assert "live-control-path-secret" not in rendered


def test_scan_allows_references_but_rejects_shell_fallback_literals(tmp_path: Path) -> None:
    key = "LLM_API_" + "KEY"
    shell_reference = "LLM_API_" + "KEY"
    repository = _tracked_repository(
        tmp_path,
        {
            "workflow.env": "\n".join(
                [
                    f"{key} = ${{{{ secrets.LLM_API_KEY }}}}",
                    f"{key} = ${{{shell_reference}}}",
                    f"{key} = ${{{shell_reference}:-live-fallback-secret}}",
                    f"{key} = ${{{shell_reference}-live-fallback-secret}}",
                ]
            )
        },
    )

    findings = scan_repository(repository)

    assert [(finding.path, finding.line_number) for finding in findings] == [
        ("workflow.env", 3),
        ("workflow.env", 4),
    ]
    assert "live-fallback-secret" not in "\n".join(finding.render() for finding in findings)


@pytest.mark.parametrize(
    "placeholder",
    [
        "replace-with-a-repository-secret",
        "your-api-key",
        "test",
    ],
)
def test_scan_allows_generic_key_documentation_placeholders(
    tmp_path: Path,
    placeholder: str,
) -> None:
    repository = _tracked_repository(
        tmp_path,
        {".env.example": f'LLM_API_{"KEY"}="{placeholder}"'},
    )

    assert scan_repository(repository) == []


def test_scan_rejects_literal_placeholders_in_generated_runtime_files(tmp_path: Path) -> None:
    token_key = "LLM_API_" + "KEY"
    literal_values = (
        "your-prod-live-9f3d3f8e",
        "replace-with-real-looking-value",
        "<live-looking-value>",
    )
    repository = _tracked_repository(
        tmp_path,
        {
            path: "\n".join(f"{token_key}: {value}" for value in literal_values)
            for path in ("data/report.json", "content/report.md")
        },
    )

    findings = scan_repository(repository)

    assert [(finding.path, finding.line_number, finding.rule) for finding in findings] == [
        ("content/report.md", 1, "credential assignment"),
        ("content/report.md", 2, "credential assignment"),
        ("content/report.md", 3, "credential assignment"),
        ("data/report.json", 1, "credential assignment"),
        ("data/report.json", 2, "credential assignment"),
        ("data/report.json", 3, "credential assignment"),
    ]
    rendered = "\n".join(finding.render() for finding in findings)
    assert all(value not in rendered for value in literal_values)


def test_scan_allows_literal_placeholders_only_in_explicit_example_paths(
    tmp_path: Path,
) -> None:
    token_key = "LLM_API_" + "KEY"
    literal_values = (
        "your-prod-live-9f3d3f8e",
        "replace-with-real-looking-value",
        "<live-looking-value>",
    )
    content = "\n".join(f"{token_key}: {value}" for value in literal_values)
    repository = _tracked_repository(
        tmp_path,
        {
            ".env.example": content,
            "README.md": content,
            "docs/superpowers/plans/example.md": content,
            "docs/superpowers/specs/example.md": content,
        },
    )

    assert scan_repository(repository) == []


def test_scan_allows_nonliteral_references_in_runtime_files(tmp_path: Path) -> None:
    token_key = "LLM_API_" + "KEY"
    shell_reference = "LLM_API_" + "KEY"
    repository = _tracked_repository(
        tmp_path,
        {
            "data/report.json": "\n".join(
                [
                    f"{token_key}: ${{{shell_reference}}}",
                    f"{token_key}: ${{{{ secrets.LLM_API_KEY }}}}",
                ]
            )
        },
    )

    assert scan_repository(repository) == []


@pytest.mark.parametrize(
    "token_key",
    [
        "ANTHROPIC_AUTH_" + "TOKEN",
        "LLM_API_" + "KEY",
    ],
)
def test_scan_detects_indented_multiline_token_and_bearer_values_at_key_line(
    tmp_path: Path,
    token_key: str,
) -> None:
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "multiline.yaml": "\n".join(
                [
                    f"{token_key}:",
                    "  live-yaml-secret",
                    f'"{token_key}":',
                    ' "live-json-secret"',
                    f"{header_key}:",
                    "  Bearer live-yaml-bearer-secret",
                    f'"{header_key}":',
                    ' "Bearer live-json-bearer-secret"',
                ]
            )
        },
    )

    findings = scan_repository(repository)

    assert [(finding.line_number, finding.rule) for finding in findings] == [
        (1, "credential assignment"),
        (3, "credential assignment"),
        (5, "bearer credential"),
        (7, "bearer credential"),
    ]


def test_scan_detects_cr_only_json_assignment(tmp_path: Path) -> None:
    token_key = "LLM_API_" + "KEY"
    repository = _tracked_repository(
        tmp_path,
        {"settings.json": f'{{"{token_key}":\r"live-json-secret"}}'},
    )

    findings = scan_repository(repository)

    assert [(finding.path, finding.line_number, finding.rule) for finding in findings] == [
        ("settings.json", 1, "credential assignment")
    ]


def test_scan_detects_cr_only_multiline_values_with_key_line_numbers(tmp_path: Path) -> None:
    token_key = "LLM_API_" + "KEY"
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "settings.yaml": "\r".join(
                [
                    "safe: true",
                    f"{token_key}:",
                    "  live-assignment-secret",
                    f"{header_key}:",
                    "  Bearer live-bearer-secret",
                ]
            )
        },
    )

    findings = scan_repository(repository)

    assert [(finding.path, finding.line_number, finding.rule) for finding in findings] == [
        ("settings.yaml", 2, "credential assignment"),
        ("settings.yaml", 4, "bearer credential"),
    ]


def test_scan_detects_bounded_json_blank_line_and_yaml_comment_separators(
    tmp_path: Path,
) -> None:
    token_key = "ANTHROPIC_AUTH_" + "TOKEN"
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "structured-values.txt": "\n".join(
                [
                    f'{{"{token_key}":',
                    '"live-json-secret"}',
                    f"{token_key}:",
                    "",
                    "  live-after-blank-secret",
                    f"{token_key}:",
                    "  # credential below",
                    "  live-after-comment-secret",
                    f'{{"{header_key}":',
                    '"Bearer live-json-bearer-secret"}',
                    f"{header_key}:",
                    "",
                    "  Bearer live-blank-bearer-secret",
                    f"{header_key}:",
                    "  # credential below",
                    "  Bearer live-comment-bearer-secret",
                ]
            )
        },
    )

    findings = scan_repository(repository)

    assert [(finding.line_number, finding.rule) for finding in findings] == [
        (1, "credential assignment"),
        (3, "credential assignment"),
        (6, "credential assignment"),
        (9, "bearer credential"),
        (11, "bearer credential"),
        (14, "bearer credential"),
    ]


def test_scan_allows_placeholders_after_bounded_multiline_separators(tmp_path: Path) -> None:
    token_key = "ANTHROPIC_AUTH_" + "TOKEN"
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "placeholder-values.txt": "\n".join(
                [
                    f'{{"{token_key}":',
                    '"${{ secrets.ANTHROPIC_AUTH_TOKEN }}"}',
                    f"{token_key}:",
                    "",
                    "  # injected by the environment",
                    "  ${TOKEN}",
                    f"{header_key}:",
                    "",
                    "  # injected by the environment",
                    "  Bearer ${{ secrets.GITHUB_TOKEN }}",
                ]
            )
        },
    )

    assert scan_repository(repository) == []


def test_scan_does_not_allow_many_blank_and_comment_lines_to_bypass_detection(
    tmp_path: Path,
) -> None:
    token_key = "ANTHROPIC_AUTH_" + "TOKEN"
    repository = _tracked_repository(
        tmp_path,
        {
            "long-separator.yaml": "\n".join(
                [
                    f"{token_key}:",
                    "  # comment 1",
                    "",
                    "  # comment 2",
                    "",
                    "  # comment 3",
                    "",
                    "  live-after-many-comments-secret",
                ]
            )
        },
    )

    findings = scan_repository(repository)

    assert [(finding.line_number, finding.rule) for finding in findings] == [
        (1, "credential assignment")
    ]


@pytest.mark.parametrize(
    "token_key",
    [
        "ANTHROPIC_AUTH_" + "TOKEN",
        "LLM_API_" + "KEY",
    ],
)
def test_scan_detects_values_after_inline_yaml_comment_and_reports_key_line(
    tmp_path: Path,
    token_key: str,
) -> None:
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "inline-comments.yaml": "\n".join(
                [
                    f"{token_key}:  # supplied below",
                    "  live-token-secret",
                    f"{header_key}: # supplied below",
                    "  Bearer live-bearer-secret",
                ]
            )
        },
    )

    findings = scan_repository(repository)

    assert [(finding.line_number, finding.rule) for finding in findings] == [
        (1, "credential assignment"),
        (3, "bearer credential"),
    ]


def test_scan_allows_placeholder_after_inline_yaml_comment(tmp_path: Path) -> None:
    token_key = "ANTHROPIC_AUTH_" + "TOKEN"
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "inline-placeholders.yaml": "\n".join(
                [
                    f"{token_key}: # supplied below",
                    "  ${TOKEN}",
                    f"{header_key}: # supplied below",
                    "  Bearer ${{ secrets.GITHUB_TOKEN }}",
                ]
            )
        },
    )

    assert scan_repository(repository) == []


def test_scan_does_not_consume_next_key_after_inline_comment_only_value(
    tmp_path: Path,
) -> None:
    token_key = "ANTHROPIC_AUTH_" + "TOKEN"
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "inline-null.yaml": "\n".join(
                [
                    f"{token_key}: # intentionally empty",
                    "NEXT_KEY: harmless",
                    f"{header_key}: # intentionally empty",
                    "NEXT_HEADER: Bearer harmless",
                    f"{token_key}: # intentionally empty",
                    '"NEXT_QUOTED_KEY": harmless',
                ]
            )
        },
    )

    assert scan_repository(repository) == []


def test_scan_does_not_consume_unindented_next_key_as_multiline_value(tmp_path: Path) -> None:
    token_key = "ANTHROPIC_AUTH_" + "TOKEN"
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "null-values.yaml": "\n".join(
                [
                    f"{token_key}:",
                    "NEXT_KEY: harmless",
                    f"{header_key}:",
                    "NEXT_HEADER: Bearer harmless",
                    f"{token_key}:",
                    '"NEXT_QUOTED_KEY": harmless',
                    f"{header_key}:",
                    '"NEXT_QUOTED_HEADER": "Bearer harmless"',
                ]
            )
        },
    )

    assert scan_repository(repository) == []


def test_bearer_github_references_pass_but_literal_fallback_is_reported(
    tmp_path: Path,
) -> None:
    header_key = "Author" + "ization"
    repository = _tracked_repository(
        tmp_path,
        {
            "headers.yaml": "\n".join(
                [
                    f"{header_key}: Bearer ${{{{ secrets.GITHUB_TOKEN }}}}",
                    f"{header_key}:",
                    "  Bearer ${{ secrets.GITHUB_TOKEN }}",
                    f"{header_key}: Bearer ${{TOKEN}}",
                    f"{header_key}: Bearer ${{TOKEN:-live-bearer-fallback}}",
                ]
            )
        },
    )

    findings = scan_repository(repository)

    assert [(finding.line_number, finding.rule) for finding in findings] == [
        (5, "bearer credential")
    ]
    assert "live-bearer-fallback" not in findings[0].render()


@pytest.mark.parametrize(
    "authorization_line",
    [
        '"Author' + 'ization": "Bearer live-json-secret"',
        "Author" + 'ization: "Bearer live-yaml-secret"',
        "'Author" + "ization': 'Bearer live-python-secret'",
        "Author" + "ization: Bearer live-plain-secret",
        "Author" + "ization: Bearer 'live-quoted-secret'",
        "aUtHoR" + 'iZaTiOn : "bEaReR live-mixed-secret"',
    ],
)
def test_scan_detects_quoted_and_unquoted_bearer_header_forms(
    tmp_path: Path,
    authorization_line: str,
) -> None:
    repository = _tracked_repository(tmp_path, {"headers.txt": authorization_line})

    findings = scan_repository(repository)

    assert [(finding.path, finding.line_number, finding.rule) for finding in findings] == [
        ("headers.txt", 1, "bearer credential")
    ]
    rendered = findings[0].render()
    assert "headers.txt:1:" in rendered
    assert "live-" not in rendered


@pytest.mark.parametrize(
    "authorization_line",
    [
        "Author" + "ization: Bearer ${GITHUB_TOKEN}",
        '"Author' + 'ization": "Bearer ${GITHUB_TOKEN}"',
        "'Author" + "ization': 'Bearer ${GITHUB_TOKEN}'",
        '"Author' + 'ization": "Bearer client-default-token"',
    ],
)
def test_scan_accepts_bearer_placeholders_in_all_supported_header_forms(
    tmp_path: Path,
    authorization_line: str,
) -> None:
    repository = _tracked_repository(tmp_path, {".env.example": authorization_line})

    assert scan_repository(repository) == []


def test_scan_ignores_placeholders_untracked_files_and_binary_data(tmp_path: Path) -> None:
    untracked_secret = "ANTHROPIC_AUTH_" + "TOKEN=real-value"
    binary_secret = b"\x00ANTHROPIC_AUTH_" + b"TOKEN=real-value"
    repository = _tracked_repository(
        tmp_path,
        {
            ".env.example": (
                "ANTHROPIC_AUTH_" + "TOKEN=replace-with-a-repository-secret\n"
                "Author" + "ization: Bearer ${GITHUB_TOKEN}\n"
            ),
            "asset.bin": binary_secret,
            "odd name\twith\nnewline.txt": "safe tracked content\n",
        },
    )
    (repository / "untracked.env").write_text(untracked_secret, encoding="utf-8")

    assert scan_repository(repository) == []


def test_scan_large_comment_chain_completes_within_timeout(tmp_path: Path) -> None:
    repeated_line = "# LLM_API_" + "KEY: # intentionally empty\n"
    repository = _tracked_repository(
        tmp_path,
        {"large-comments.txt": repeated_line * 8_000},
    )

    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


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


def test_main_none_reads_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_PATH), "unexpected"])

    assert main() == 2

    captured = capsys.readouterr()
    assert "unexpected arguments" in captured.err
    assert captured.out == ""


def test_script_rejects_real_unexpected_cli_argument() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "unexpected"],
        cwd=SCRIPT_PATH.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "unexpected arguments" in completed.stderr
    assert completed.stdout == ""
