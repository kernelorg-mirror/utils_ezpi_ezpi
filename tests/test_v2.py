"""Tests for ezpi public-inbox v2 format support."""
from __future__ import annotations

import email.message
import subprocess
from pathlib import Path

import pytest
from conftest import get_commit_body

import ezpi


class TestInitV2Inbox:
    """Tests for v2 inbox initialization."""

    def test_init_v2_inbox(self, tmp_path: Path) -> None:
        """Test initializing a new v2 inbox."""
        v2path = tmp_path / "test-inbox"
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        # Verify structure
        assert (v2path / "inbox.lock").exists()
        assert (v2path / "git").is_dir()
        assert (v2path / "git" / "0.git").is_dir()
        assert (v2path / "all.git").is_dir()
        assert (v2path / "all.git" / "objects" / "info" / "alternates").exists()

        # Verify epoch path is correct
        assert epoch_path == str(v2path / "git" / "0.git")

        # Verify alternates content
        with open(v2path / "all.git" / "objects" / "info" / "alternates") as f:
            alternates = f.read()
        assert "../../git/0.git/objects" in alternates

    def test_init_v2_inbox_already_exists(self, tmp_path: Path) -> None:
        """Test that init_v2_inbox raises if path exists."""
        v2path = tmp_path / "existing"
        v2path.mkdir()

        with pytest.raises(FileExistsError):
            ezpi.init_v2_inbox(str(v2path))


class TestEpochManagement:
    """Tests for epoch creation and management."""

    def test_init_epoch(self, tmp_path: Path) -> None:
        """Test creating a new epoch."""
        v2path = tmp_path / "test-inbox"
        ezpi.init_v2_inbox(str(v2path))

        # Create second epoch
        epoch_path = ezpi.init_epoch(str(v2path), 1)
        assert epoch_path == str(v2path / "git" / "1.git")
        assert (v2path / "git" / "1.git").is_dir()

        # Verify alternates updated
        with open(v2path / "all.git" / "objects" / "info" / "alternates") as f:
            alternates = f.read()
        assert "../../git/0.git/objects" in alternates
        assert "../../git/1.git/objects" in alternates

    def test_get_latest_epoch(self, tmp_path: Path) -> None:
        """Test finding the latest epoch."""
        v2path = tmp_path / "test-inbox"
        ezpi.init_v2_inbox(str(v2path))

        epoch_num, epoch_path = ezpi.get_latest_epoch(str(v2path))
        assert epoch_num == 0
        assert epoch_path == str(v2path / "git" / "0.git")

        # Create more epochs
        ezpi.init_epoch(str(v2path), 1)
        ezpi.init_epoch(str(v2path), 2)

        epoch_num, epoch_path = ezpi.get_latest_epoch(str(v2path))
        assert epoch_num == 2
        assert epoch_path == str(v2path / "git" / "2.git")

    def test_get_latest_epoch_no_git_dir(self, tmp_path: Path) -> None:
        """Test get_latest_epoch with no git directory."""
        v2path = tmp_path / "empty"
        v2path.mkdir()

        with pytest.raises(FileNotFoundError, match="No git directory"):
            ezpi.get_latest_epoch(str(v2path))

    def test_get_epoch_size_empty(self, tmp_path: Path) -> None:
        """Test epoch size of empty repo."""
        v2path = tmp_path / "test-inbox"
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        size = ezpi.get_epoch_size(epoch_path)
        assert size == 0

    def test_get_epoch_size_with_objects(self, tmp_path: Path) -> None:
        """Test epoch size after adding objects."""
        v2path = tmp_path / "test-inbox"
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        # Add a message to create objects
        msg_bytes = (
            b"From: Test <test@example.com>\n"
            b"Subject: Size Test\n"
            b"Message-Id: <size-test@example.com>\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\n"
            b"\n"
            b"Body.\n"
        )
        ezpi.add_rfc822(epoch_path, msg_bytes)

        size = ezpi.get_epoch_size(epoch_path)
        assert size > 0


class TestEpochRotation:
    """Tests for epoch rotation logic."""

    def test_should_rotate_epoch_size_below_threshold(self, tmp_path: Path) -> None:
        """Test that small epoch doesn't trigger rotation."""
        v2path = tmp_path / "test-inbox"
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        # Add a small message
        msg_bytes = (
            b"From: Test <test@example.com>\n"
            b"Subject: Small Test\n"
            b"Message-Id: <small@example.com>\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\n"
            b"\n"
            b"Small body.\n"
        )
        ezpi.add_rfc822(epoch_path, msg_bytes)

        assert not ezpi.should_rotate_epoch(epoch_path, 'size')

    def test_should_rotate_epoch_annual_no_commits(self, tmp_path: Path) -> None:
        """Test annual rotation with no commits doesn't trigger rotation."""
        v2path = tmp_path / "test-inbox"
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        # No commits, should not rotate
        assert not ezpi.should_rotate_epoch(epoch_path, 'annual')


class TestAddRfc822V2:
    """Tests for add_rfc822_v2 function."""

    def test_add_rfc822_v2_new_inbox(self, tmp_path: Path) -> None:
        """Test adding message to new v2 inbox (auto-creates)."""
        v2path = tmp_path / "new-inbox"
        msg_bytes = (
            b"From: Test <test@example.com>\n"
            b"Subject: V2 Test\n"
            b"Message-Id: <v2-test@example.com>\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\n"
            b"\n"
            b"Body in v2 inbox.\n"
        )

        ezpi.add_rfc822_v2(str(v2path), msg_bytes)

        # Verify inbox was created
        assert (v2path / "inbox.lock").exists()
        assert (v2path / "git" / "0.git").is_dir()

        # Verify message was written
        body = get_commit_body(v2path / "git" / "0.git")
        assert b"Body in v2 inbox" in body

    def test_add_rfc822_v2_existing_inbox(self, tmp_path: Path) -> None:
        """Test adding message to existing v2 inbox."""
        v2path = tmp_path / "existing-inbox"
        ezpi.init_v2_inbox(str(v2path))

        msg_bytes = (
            b"From: Test <test@example.com>\n"
            b"Subject: Existing V2 Test\n"
            b"Message-Id: <existing-v2@example.com>\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\n"
            b"\n"
            b"Body in existing inbox.\n"
        )

        ezpi.add_rfc822_v2(str(v2path), msg_bytes)

        # Verify message was written to epoch 0
        body = get_commit_body(v2path / "git" / "0.git")
        assert b"Body in existing inbox" in body

    def test_add_rfc822_v2_multiple_messages(self, tmp_path: Path) -> None:
        """Test adding multiple messages to v2 inbox."""
        v2path = tmp_path / "multi-inbox"

        for i in range(3):
            msg_bytes = (
                f"From: Test <test@example.com>\n"
                f"Subject: Message {i}\n"
                f"Message-Id: <msg-{i}@example.com>\n"
                f"Date: Mon, 01 Jan 2024 12:00:00 +0000\n"
                f"\n"
                f"Body {i}.\n"
            ).encode()
            ezpi.add_rfc822_v2(str(v2path), msg_bytes)

        # Verify all written to same epoch
        result = subprocess.run(
            ["git", "--git-dir", str(v2path / "git" / "0.git"), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert int(result.stdout.strip()) == 3

    def test_add_rfc822_v2_with_message_object(self, tmp_path: Path) -> None:
        """Test adding Message object to v2 inbox."""
        v2path = tmp_path / "msg-obj-inbox"

        msg = email.message.EmailMessage()
        msg["From"] = "Test <test@example.com>"
        msg["Subject"] = "Message Object V2"
        msg["Message-Id"] = "<msgobj-v2@example.com>"
        msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
        msg.set_content("Message object body.")

        ezpi.add_rfc822_v2(str(v2path), msg)

        body = get_commit_body(v2path / "git" / "0.git")
        assert b"Message object body" in body
