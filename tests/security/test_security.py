"""Security hardening tests: path traversal, URLs, scrubbing, audit."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.security.audit import SecurityAudit
from magnetoclip.security.safe_names import safe_join, sanitize_filename
from magnetoclip.security.validation import InvalidUrlError, validate_url


class TestSanitizeFilename:
    def test_removes_paths(self) -> None:
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("C:\\Windows\\evil.exe") == "evil.exe"

    def test_replaces_invalid_chars(self) -> None:
        assert sanitize_filename("a<b>c|d?e*f") == "a_b_c_d_e_f"

    def test_strips_control_chars(self) -> None:
        assert "\x00" not in sanitize_filename("bad\x00name")

    def test_handles_reserved_names(self) -> None:
        assert sanitize_filename("CON") == "_CON"
        assert sanitize_filename("LPT1.txt").startswith("_")

    def test_empty_falls_back(self) -> None:
        assert sanitize_filename("  .  ") == "download"


class TestSafeJoin:
    def test_normal_join(self, tmp_path: Path) -> None:
        assert safe_join(tmp_path, "report.pdf") == (tmp_path / "report.pdf")

    def test_traversal_is_neutralized(self, tmp_path: Path) -> None:
        # Traversal prefixes are stripped during sanitization, never applied.
        assert safe_join(tmp_path, "../../escape.txt") == (tmp_path / "escape.txt")
        assert safe_join(tmp_path, "..\\..\\escape.txt") == (tmp_path / "escape.txt")

    def test_never_escapes_base(self, tmp_path: Path) -> None:
        for attempt in ("../x", "..", "/etc/passwd", "sub/../../y"):
            target = safe_join(tmp_path, attempt)
            assert target == tmp_path or tmp_path in target.parents


class TestValidateUrl:
    def test_accepts_http_https(self) -> None:
        assert validate_url("https://example.com/file.bin") == "https://example.com/file.bin"

    def test_rejects_unsafe_schemes(self) -> None:
        for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"):
            with pytest.raises(InvalidUrlError):
                validate_url(url)

    def test_rejects_control_chars(self) -> None:
        with pytest.raises(InvalidUrlError):
            validate_url("https://example.com/\r\n.inject")

    def test_rejects_empty(self) -> None:
        with pytest.raises(InvalidUrlError):
            validate_url("   ")


class TestDiagnosticScrubbing:
    def test_settings_secrets_redacted(self, tmp_path: Path) -> None:
        build_context(
            config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", log_dir=tmp_path / "log"
        )
        from magnetoclip.services.diagnostics.report import DiagnosticReport

        scrubbed = DiagnosticReport._scrub_settings(
            {"network.password": "hunter2", "auth.token": "abc123", "appearance.theme": "dark"}
        )
        assert scrubbed["network.password"] == "***REDACTED***"
        assert scrubbed["auth.token"] == "***REDACTED***"
        assert scrubbed["appearance.theme"] == "dark"

    def test_export_report_contains_no_credentials(self, tmp_path: Path) -> None:
        context = build_context(
            config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", log_dir=tmp_path / "log"
        )
        from magnetoclip.services.diagnostics.report import DiagnosticReport

        report = DiagnosticReport(context)
        destination = report.export(tmp_path / "diagnostic.json")
        text = destination.read_text(encoding="utf-8")
        assert "Basic " not in text
        assert "Bearer " not in text
        assert re.search(r"(?i)password.?=.?\S+", text) is None
        report.assert_no_secrets()


class TestSecurityAudit:
    def test_findings_clean_by_default(self, tmp_path: Path) -> None:
        context = build_context(
            config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", log_dir=tmp_path / "log"
        )
        audit = SecurityAudit(context)
        assert audit.findings() == []

    def test_detects_plaintext_secret(self, tmp_path: Path) -> None:
        context = build_context(
            config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", log_dir=tmp_path / "log"
        )
        # Settings.set ignores unknown keys, so store via the underlying map.
        context.settings._values["network.proxy_password"] = "topsecret"  # type: ignore[attr-defined]
        findings = SecurityAudit(context).findings()
        assert any("network.proxy_password" in f["message"] for f in findings)

    def test_traversal_check_helper(self, tmp_path: Path) -> None:
        assert SecurityAudit.check_directory_traversal(tmp_path, "ok.pdf") is True
        assert SecurityAudit.check_directory_traversal(tmp_path, "ok/../../evil") is True
