"""Shared AI knowledge — security vetting (spec Part 7).

- secret detection reports TYPE LABELS only
- hard secrets block saving entirely
- soft secrets are sanitized before anything leaves the machine
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.knowledge_security import (
    detect_secret_types,
    sanitize_text,
    vet_knowledge,
)


class TestSecretDetection:
    def test_api_key_detected(self):
        assert "API key" in detect_secret_types("use key sk-abc123456789 def")

    def test_github_token_detected(self):
        assert "API key" in detect_secret_types("token ghp_" + "a" * 20)

    def test_bearer_token_detected(self):
        assert "bearer token" in detect_secret_types(
            "send Authorization: Bearer abc.def.ghi"
        )

    def test_private_key_block_detected(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        assert "private key block" in detect_secret_types(text)

    def test_jwt_detected(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4f"
        assert "JWT" in detect_secret_types(f"token {jwt}")

    def test_session_cookie_detected(self):
        assert "session cookie" in detect_secret_types("sessionid=abc123def456")

    def test_connection_string_with_password_detected(self):
        assert "connection string with credentials" in detect_secret_types(
            "postgres://admin:hunter2@db.example.com/prod"
        )

    def test_clean_text_has_no_findings(self):
        assert detect_secret_types(
            "Retry subtasks once before failing over to another worker."
        ) == []

    def test_labels_never_contain_the_value(self):
        labels = detect_secret_types("key sk-supersecretvalue123456")
        for label in labels:
            assert "sk-" not in label


class TestSanitization:
    def test_soft_secret_value_redacted_key_name_kept(self):
        cleaned = sanitize_text("api_key = sk-live-value-1234567890")
        assert "sk-live-value" not in cleaned
        assert "api_key" in cleaned
        assert "[REDACTED]" in cleaned

    def test_bearer_redacted(self):
        cleaned = sanitize_text("Authorization: Bearer abc123def456ghi")
        assert "abc123def456" not in cleaned

    def test_private_key_removed(self):
        cleaned = sanitize_text(
            "before -----BEGIN PRIVATE KEY-----\nxyz\n-----END PRIVATE KEY----- after"
        )
        assert "PRIVATE KEY" not in cleaned.replace("[REDACTED]", "")

    def test_clean_text_unchanged(self):
        original = "Prefer idempotent subtask design with explicit verification."
        assert sanitize_text(original) == original


class TestVetting:
    def test_hard_secret_blocks_saving(self):
        verdict = vet_knowledge(
            "Deploy notes",
            "Rotate this key first: -----BEGIN EC PRIVATE KEY-----\nMHQ...",
        )
        assert verdict.ok is False
        assert any("private key" in r for r in verdict.blocked_reasons)

    def test_soft_secret_sanitized_but_allowed(self):
        verdict = vet_knowledge(
            "Retry lesson", "the client used api_key = sk-value-987654321"
        )
        assert verdict.ok is True
        assert verdict.was_sanitized is True
        assert "sk-value" not in verdict.sanitized_body

    def test_clean_content_passes_untouched(self):
        body = "Serialize worker writes to avoid the race we hit last sprint."
        verdict = vet_knowledge("Race lesson", body)
        assert verdict.ok is True
        assert verdict.was_sanitized is False
        assert verdict.sanitized_body == body

    def test_secret_in_title_is_caught_too(self):
        verdict = vet_knowledge("Key sk-abcdef1234567890 rotation", "rotate quarterly")
        assert verdict.was_sanitized is True
        assert "sk-abcdef" not in verdict.sanitized_title
