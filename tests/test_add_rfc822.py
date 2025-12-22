"""Tests for ezpi.add_rfc822 function."""
from __future__ import annotations

import email
import subprocess
from pathlib import Path

import pytest

import ezpi


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """Create a bare git repository for testing."""
    repo_path = tmp_path / "test.git"
    subprocess.run(["git", "init", "--bare", str(repo_path)], check=True, capture_output=True)
    return repo_path


def get_commit_message(repo: Path) -> str:
    """Get the commit message from the latest commit."""
    result = subprocess.run(
        ["git", "--git-dir", str(repo), "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def get_commit_body(repo: Path) -> bytes:
    """Get the blob content from the latest commit."""
    # Get the tree from the latest commit
    tree_result = subprocess.run(
        ["git", "--git-dir", str(repo), "log", "-1", "--format=%T"],
        check=True,
        capture_output=True,
        text=True,
    )
    tree = tree_result.stdout.strip()

    # Get the blob hash from the tree
    ls_result = subprocess.run(
        ["git", "--git-dir", str(repo), "ls-tree", tree],
        check=True,
        capture_output=True,
        text=True,
    )
    # Format: "100644 blob <hash>\t<filename>"
    blob_hash = ls_result.stdout.split()[2]

    # Get the blob content
    blob_result = subprocess.run(
        ["git", "--git-dir", str(repo), "cat-file", "blob", blob_hash],
        check=True,
        capture_output=True,
    )
    return blob_result.stdout


class TestAddRfc822WithBytes:
    """Tests for add_rfc822 with bytes input."""

    def test_basic_message(self, bare_repo: Path) -> None:
        """Test adding a basic RFC822 message as bytes."""
        msg_bytes = b"""From: Test User <test@example.com>
Subject: Test Subject
Message-Id: <test123@example.com>
Date: Mon, 01 Jan 2024 12:00:00 +0000

This is a test message body.
"""
        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        # Verify commit was created with correct subject
        assert get_commit_message(bare_repo) == "Test Subject"

        # Verify the message body was stored
        body = get_commit_body(bare_repo)
        assert b"This is a test message body." in body
        assert b"From: Test User <test@example.com>" in body

    def test_message_without_message_id(self, bare_repo: Path) -> None:
        """Test that a Message-Id is generated if not present."""
        msg_bytes = b"""From: Test User <test@example.com>
Subject: No Message-Id Test
Date: Mon, 01 Jan 2024 12:00:00 +0000

Body without message-id.
"""
        ezpi.add_rfc822(str(bare_repo), msg_bytes, domain="test.example.com")

        body = get_commit_body(bare_repo)
        assert b"Message-Id:" in body or b"Message-ID:" in body

    def test_message_without_date(self, bare_repo: Path) -> None:
        """Test that a Date is generated if not present."""
        msg_bytes = b"""From: Test User <test@example.com>
Subject: No Date Test
Message-Id: <nodate@example.com>

Body without date.
"""
        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        body = get_commit_body(bare_repo)
        assert b"Date:" in body


class TestAddRfc822WithMessage:
    """Tests for add_rfc822 with Message object input."""

    def test_basic_message_object(self, bare_repo: Path) -> None:
        """Test adding an email.message.Message object."""
        msg = email.message.EmailMessage()
        msg["From"] = "Test User <test@example.com>"
        msg["Subject"] = "Message Object Test"
        msg["Message-Id"] = "<msgobj@example.com>"
        msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
        msg.set_content("This is a message from a Message object.")

        ezpi.add_rfc822(str(bare_repo), msg)

        # Verify commit was created with correct subject
        assert get_commit_message(bare_repo) == "Message Object Test"

        # Verify the message body was stored
        body = get_commit_body(bare_repo)
        assert b"This is a message from a Message object." in body

    def test_message_object_with_custom_env(self, bare_repo: Path) -> None:
        """Test adding a Message with custom git environment."""
        msg = email.message.EmailMessage()
        msg["From"] = "Author <author@example.com>"
        msg["Subject"] = "Custom Env Test"
        msg["Message-Id"] = "<customenv@example.com>"
        msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
        msg.set_content("Testing custom environment.")

        custom_env = {
            "GIT_COMMITTER_NAME": "Custom Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.com",
            "GIT_COMMITTER_DATE": "Mon, 01 Jan 2024 12:00:00 +0000",
        }

        ezpi.add_rfc822(str(bare_repo), msg, env=custom_env)

        # Verify commit was created
        assert get_commit_message(bare_repo) == "Custom Env Test"

        # Verify committer info
        result = subprocess.run(
            ["git", "--git-dir", str(bare_repo), "log", "-1", "--format=%cn <%ce>"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "Custom Committer <committer@example.com>"


class TestAddRfc822Validation:
    """Tests for add_rfc822 input validation."""

    def test_missing_subject_raises(self, bare_repo: Path) -> None:
        """Test that missing Subject header raises ValueError."""
        msg_bytes = b"""From: Test User <test@example.com>
Message-Id: <nosubject@example.com>

Body without subject.
"""
        with pytest.raises(ValueError, match="Subject"):
            ezpi.add_rfc822(str(bare_repo), msg_bytes)

    def test_missing_from_raises(self, bare_repo: Path) -> None:
        """Test that missing From header raises ValueError."""
        msg_bytes = b"""Subject: No From Test
Message-Id: <nofrom@example.com>

Body without from.
"""
        with pytest.raises(ValueError, match="From"):
            ezpi.add_rfc822(str(bare_repo), msg_bytes)
