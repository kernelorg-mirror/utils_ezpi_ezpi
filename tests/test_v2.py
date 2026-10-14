"""Tests for ezpi public-inbox v2 format support."""

from __future__ import annotations

import email.message
import fcntl
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import get_commit_body

import ezpi


class TestInitV2Inbox:
    """Tests for v2 inbox initialization."""

    def test_init_v2_inbox(self, tmp_path: Path) -> None:
        """Test initializing a new v2 inbox."""
        v2path = tmp_path / 'test-inbox'
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        # Verify structure
        assert (v2path / 'inbox.lock').exists()
        assert (v2path / 'git').is_dir()
        assert (v2path / 'git' / '0.git').is_dir()
        assert (v2path / 'all.git').is_dir()
        assert (v2path / 'all.git' / 'objects' / 'info' / 'alternates').exists()

        # Verify epoch path is correct
        assert epoch_path == str(v2path / 'git' / '0.git')

        # Verify alternates content
        with open(v2path / 'all.git' / 'objects' / 'info' / 'alternates') as f:
            alternates = f.read()
        assert '../../git/0.git/objects' in alternates

    def test_init_v2_inbox_already_exists(self, tmp_path: Path) -> None:
        """Test that init_v2_inbox raises if path exists."""
        v2path = tmp_path / 'existing'
        v2path.mkdir()

        with pytest.raises(FileExistsError):
            ezpi.init_v2_inbox(str(v2path))


class TestEpochManagement:
    """Tests for epoch creation and management."""

    def test_init_epoch(self, tmp_path: Path) -> None:
        """Test creating a new epoch."""
        v2path = tmp_path / 'test-inbox'
        ezpi.init_v2_inbox(str(v2path))

        # Create second epoch
        epoch_path = ezpi.init_epoch(str(v2path), 1)
        assert epoch_path == str(v2path / 'git' / '1.git')
        assert (v2path / 'git' / '1.git').is_dir()

        # Verify alternates updated
        with open(v2path / 'all.git' / 'objects' / 'info' / 'alternates') as f:
            alternates = f.read()
        assert '../../git/0.git/objects' in alternates
        assert '../../git/1.git/objects' in alternates

    def test_get_latest_epoch(self, tmp_path: Path) -> None:
        """Test finding the latest epoch."""
        v2path = tmp_path / 'test-inbox'
        ezpi.init_v2_inbox(str(v2path))

        epoch_num, epoch_path = ezpi.get_latest_epoch(str(v2path))
        assert epoch_num == 0
        assert epoch_path == str(v2path / 'git' / '0.git')

        # Create more epochs
        ezpi.init_epoch(str(v2path), 1)
        ezpi.init_epoch(str(v2path), 2)

        epoch_num, epoch_path = ezpi.get_latest_epoch(str(v2path))
        assert epoch_num == 2
        assert epoch_path == str(v2path / 'git' / '2.git')

    def test_get_latest_epoch_no_git_dir(self, tmp_path: Path) -> None:
        """Test get_latest_epoch with no git directory."""
        v2path = tmp_path / 'empty'
        v2path.mkdir()

        with pytest.raises(FileNotFoundError, match='No git directory'):
            ezpi.get_latest_epoch(str(v2path))

    def test_get_epoch_size_empty(self, tmp_path: Path) -> None:
        """Test epoch size of empty repo."""
        v2path = tmp_path / 'test-inbox'
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        size = ezpi.get_epoch_size(epoch_path)
        assert size == 0

    def test_get_epoch_size_with_objects(self, tmp_path: Path) -> None:
        """Test epoch size after adding objects."""
        v2path = tmp_path / 'test-inbox'
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        # Add a message to create objects
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Size Test\n'
            b'Message-Id: <size-test@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'Body.\n'
        )
        ezpi.add_rfc822(epoch_path, msg_bytes)

        size = ezpi.get_epoch_size(epoch_path)
        assert size > 0


class TestEpochRotation:
    """Tests for epoch rotation logic."""

    def test_should_rotate_epoch_size_below_threshold(self, tmp_path: Path) -> None:
        """Test that small epoch doesn't trigger rotation."""
        v2path = tmp_path / 'test-inbox'
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        # Add a small message
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Small Test\n'
            b'Message-Id: <small@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'Small body.\n'
        )
        ezpi.add_rfc822(epoch_path, msg_bytes)

        assert not ezpi.should_rotate_epoch(epoch_path, 'size')

    def test_should_rotate_epoch_annual_no_commits(self, tmp_path: Path) -> None:
        """Test annual rotation with no commits doesn't trigger rotation."""
        v2path = tmp_path / 'test-inbox'
        epoch_path = ezpi.init_v2_inbox(str(v2path))

        # No commits, should not rotate
        assert not ezpi.should_rotate_epoch(epoch_path, 'annual')


class TestAddRfc822V2:
    """Tests for add_rfc822_v2 function."""

    def test_add_rfc822_v2_new_inbox(self, tmp_path: Path) -> None:
        """Test adding message to new v2 inbox (auto-creates)."""
        v2path = tmp_path / 'new-inbox'
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: V2 Test\n'
            b'Message-Id: <v2-test@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'Body in v2 inbox.\n'
        )

        ezpi.add_rfc822_v2(str(v2path), msg_bytes)

        # Verify inbox was created
        assert (v2path / 'inbox.lock').exists()
        assert (v2path / 'git' / '0.git').is_dir()

        # Verify message was written
        body = get_commit_body(v2path / 'git' / '0.git')
        assert b'Body in v2 inbox' in body

    def test_add_rfc822_v2_existing_inbox(self, tmp_path: Path) -> None:
        """Test adding message to existing v2 inbox."""
        v2path = tmp_path / 'existing-inbox'
        ezpi.init_v2_inbox(str(v2path))

        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Existing V2 Test\n'
            b'Message-Id: <existing-v2@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'Body in existing inbox.\n'
        )

        ezpi.add_rfc822_v2(str(v2path), msg_bytes)

        # Verify message was written to epoch 0
        body = get_commit_body(v2path / 'git' / '0.git')
        assert b'Body in existing inbox' in body

    def test_add_rfc822_v2_multiple_messages(self, tmp_path: Path) -> None:
        """Test adding multiple messages to v2 inbox."""
        v2path = tmp_path / 'multi-inbox'

        for i in range(3):
            msg_bytes = (
                f'From: Test <test@example.com>\n'
                f'Subject: Message {i}\n'
                f'Message-Id: <msg-{i}@example.com>\n'
                f'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
                f'\n'
                f'Body {i}.\n'
            ).encode()
            ezpi.add_rfc822_v2(str(v2path), msg_bytes)

        # Verify all written to same epoch
        result = subprocess.run(
            ['git', '--git-dir', str(v2path / 'git' / '0.git'), 'rev-list', '--count', 'HEAD'],
            check=True,
            capture_output=True,
            text=True,
        )
        assert int(result.stdout.strip()) == 3

    def test_add_rfc822_v2_with_message_object(self, tmp_path: Path) -> None:
        """Test adding Message object to v2 inbox."""
        v2path = tmp_path / 'msg-obj-inbox'

        msg = email.message.EmailMessage()
        msg['From'] = 'Test <test@example.com>'
        msg['Subject'] = 'Message Object V2'
        msg['Message-Id'] = '<msgobj-v2@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        msg.set_content('Message object body.')

        ezpi.add_rfc822_v2(str(v2path), msg)

        body = get_commit_body(v2path / 'git' / '0.git')
        assert b'Message object body' in body


class TestV2Locking:
    """Tests for the locks taken by add_rfc822_v2."""

    @pytest.mark.parametrize('lockfile', ['inbox.lock', 'git/0.git/ezpi.lock'])
    def test_waits_for_flock_holder(self, tmp_path: Path, lockfile: str) -> None:
        """A write must wait for a flock(2) holder of either lock.

        public-inbox locks inbox.lock with flock(2), so a lockf(3) lock
        would not exclude it. The holder is a separate open() in this same
        process, which also proves that two threads exclude each other.
        """
        v2path = tmp_path / 'locked-inbox'
        ezpi.init_v2_inbox(str(v2path))
        msg_bytes = b'From: Test <test@example.com>\nSubject: Locked\n\nBody.\n'
        writer = threading.Thread(target=ezpi.add_rfc822_v2, args=(str(v2path), msg_bytes))

        with open(v2path / lockfile, 'w') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            writer.start()
            writer.join(timeout=0.5)
            assert writer.is_alive(), 'add_rfc822_v2 did not wait for the lock'
        writer.join(timeout=10)
        assert not writer.is_alive()
        assert b'Body.' in get_commit_body(v2path / 'git' / '0.git')

    def test_writer_arrives_during_creation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A writer that arrives while another is creating the inbox must succeed.

        The second writer starts from inside the first one's init_epoch(),
        after the inbox directory exists but before its first epoch does:
        the window that multi-worker services used to hit. A real race
        hits that window too rarely to make a reliable test. The parent
        directory does not exist yet either.
        """
        v2path = tmp_path / 'lists' / 'new-inbox'
        real_init_epoch = ezpi.init_epoch
        racer_errors: list[Exception] = []

        def racer() -> None:
            try:
                ezpi.add_rfc822_v2(str(v2path), b'From: a@example.org\nSubject: racer\n\nhi\n')
            except Exception as exc:
                racer_errors.append(exc)

        def init_epoch_with_racer(path: str, epoch: int) -> str:
            monkeypatch.setattr(ezpi, 'init_epoch', real_init_epoch)
            # A thread rather than a direct call, so a lock wrongly held
            # during creation fails the test instead of hanging it.
            thread = threading.Thread(target=racer)
            thread.start()
            thread.join(timeout=30)
            assert not thread.is_alive(), 'racer blocked while the inbox was being created'
            return real_init_epoch(path, epoch)

        monkeypatch.setattr(ezpi, 'init_epoch', init_epoch_with_racer)
        ezpi.add_rfc822_v2(str(v2path), b'From: a@example.org\nSubject: first\n\nhi\n')

        assert racer_errors == []
        result = subprocess.run(
            ['git', '--git-dir', str(v2path / 'git' / '0.git'), 'log', '--format=%s'],
            check=True,
            capture_output=True,
            text=True,
        )
        assert sorted(result.stdout.split()) == ['first', 'racer']
        assert os.listdir(tmp_path / 'lists') == ['new-inbox'], 'temporary inbox left behind'


class TestV2Cli:
    """Tests for the ezpi command with --v2-path."""

    def test_runs_inbox_post_commit_hook(self, tmp_path: Path) -> None:
        """--run-post-commit-hook runs <inbox>/hooks/post-commit in the inbox.

        The hook lives in the inbox, not in an epoch, so that rotation
        (which creates a fresh epoch with git init) does not lose it.
        """
        v2path = tmp_path / 'hook-inbox'
        ezpi.init_v2_inbox(str(v2path))
        marker = tmp_path / 'hook-ran'
        hook = v2path / 'hooks' / 'post-commit'
        hook.parent.mkdir()
        hook.write_text(f'#!/bin/sh\npwd > {marker}\n')
        hook.chmod(0o755)

        subprocess.run(
            [sys.executable, '-c', 'import ezpi; ezpi.command()', '--v2-path', str(v2path), '-p', '--rfc822'],
            input=b'From: Test <test@example.com>\nSubject: Hook\n\nBody.\n',
            check=True,
            capture_output=True,
        )

        assert marker.read_text().strip() == str(v2path)
