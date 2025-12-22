"""Tests for ezpi.add_plaintext function."""
from __future__ import annotations

from pathlib import Path

from conftest import get_commit_body, get_commit_message

import ezpi


class TestAddPlaintext:
    """Tests for add_plaintext function."""

    def test_basic_plaintext(self, bare_repo: Path) -> None:
        """Test adding basic plaintext content."""
        ezpi.add_plaintext(
            str(bare_repo),
            content="This is plain text content.",
            subject="Plaintext Test",
            authorname="Test Author",
            authoremail="author@example.com",
        )

        commit_msg = get_commit_message(bare_repo)
        assert "Plaintext Test" in commit_msg

        body = get_commit_body(bare_repo)
        assert b"This is plain text content." in body
        assert b"From: Test Author <author@example.com>" in body

    def test_plaintext_with_unicode_content(self, bare_repo: Path) -> None:
        """Test plaintext with Unicode in body."""
        ezpi.add_plaintext(
            str(bare_repo),
            content="Caf\u00e9 with \u00e9\u00e8\u00ea accents and \u2014 dash.",
            subject="Unicode Plaintext",
            authorname="Test Author",
            authoremail="author@example.com",
        )

        body = get_commit_body(bare_repo)
        assert b"accents" in body

    def test_plaintext_with_unicode_author(self, bare_repo: Path) -> None:
        """Test plaintext with Unicode in author name."""
        ezpi.add_plaintext(
            str(bare_repo),
            content="Body text.",
            subject="Unicode Author Test",
            authorname="Ren\u00e9 Fran\u00e7ois",
            authoremail="rene@example.com",
        )

        body = get_commit_body(bare_repo)
        assert b"rene@example.com" in body

    def test_plaintext_with_unicode_subject(self, bare_repo: Path) -> None:
        """Test plaintext with Unicode in subject."""
        ezpi.add_plaintext(
            str(bare_repo),
            content="Body text.",
            subject="Subject with \u00e9 and \u2014",
            authorname="Test Author",
            authoremail="author@example.com",
        )

        body = get_commit_body(bare_repo)
        assert b"Body text." in body

    def test_plaintext_with_domain(self, bare_repo: Path) -> None:
        """Test plaintext with custom domain for Message-Id."""
        ezpi.add_plaintext(
            str(bare_repo),
            content="Body text.",
            subject="Domain Test",
            authorname="Test Author",
            authoremail="author@example.com",
            domain="custom.example.com",
        )

        body = get_commit_body(bare_repo)
        assert b"Message-Id:" in body or b"Message-ID:" in body
        assert b"custom.example.com" in body

    def test_plaintext_multiline(self, bare_repo: Path) -> None:
        """Test plaintext with multiple lines."""
        content = "Line 1\nLine 2\nLine 3\n\nParagraph 2."
        ezpi.add_plaintext(
            str(bare_repo),
            content=content,
            subject="Multiline Test",
            authorname="Test Author",
            authoremail="author@example.com",
        )

        body = get_commit_body(bare_repo)
        assert b"Line 1" in body
        assert b"Line 2" in body
        assert b"Paragraph 2" in body

    def test_plaintext_empty_content(self, bare_repo: Path) -> None:
        """Test plaintext with empty content."""
        ezpi.add_plaintext(
            str(bare_repo),
            content="",
            subject="Empty Content Test",
            authorname="Test Author",
            authoremail="author@example.com",
        )

        commit_msg = get_commit_message(bare_repo)
        assert "Empty Content Test" in commit_msg
