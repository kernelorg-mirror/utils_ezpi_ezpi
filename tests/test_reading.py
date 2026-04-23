"""Tests for ezpi public-inbox v2 format reading."""
from __future__ import annotations

import json
import os
import subprocess
from email.utils import formatdate
from pathlib import Path

import pytest

import ezpi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(i: int, date: str | None = None) -> bytes:
    """Build a deterministic RFC822 message for testing."""
    if date is None:
        date = "Mon, 01 Jan 2024 12:00:00 +0000"
    return (
        f"From: Sender {i} <sender{i}@example.com>\n"
        f"Subject: Test message {i}\n"
        f"Message-Id: <msg-{i}@example.com>\n"
        f"Date: {date}\n"
        f"\n"
        f"Body of message {i}.\n"
    ).encode()


def _add_with_committer_date(v2path: Path, msg_bytes: bytes, committer_date: str) -> None:
    """Add a message with a controlled committer date (needed for date-anchored tests)."""
    env = {
        "GIT_COMMITTER_NAME": ezpi.DEFAULT_NAME,
        "GIT_COMMITTER_EMAIL": ezpi.DEFAULT_ADDR,
        "GIT_COMMITTER_DATE": committer_date,
    }
    ezpi.add_rfc822_v2(str(v2path), msg_bytes, env=env)


def _rewrite_history(v2path: Path, epoch: int, messages: list[bytes]) -> None:
    """Replay a fresh history into an existing epoch, dropping the old tip.

    Uses a distinct committer identity so the replayed commits have fresh
    hashes (if the committer were identical, bit-identical commits would
    collide back to the original SHA-1s and the rebase would be invisible).
    This is how we simulate a public-inbox rebase (e.g. a message being
    removed from history).
    """
    epoch_path = str(v2path / "git" / f"{epoch}.git")
    ezpi.git_run_command(epoch_path, ["update-ref", "-d", ezpi.PI_HEAD])
    for mb in messages:
        env = {
            "GIT_COMMITTER_NAME": "Rewriter",
            "GIT_COMMITTER_EMAIL": "rewriter@example.com",
            "GIT_COMMITTER_DATE": formatdate(),
        }
        ezpi.add_rfc822(epoch_path, mb, env=env)


def _commit_count(v2path: Path, epoch: int) -> int:
    result = subprocess.run(
        ["git", "--git-dir", str(v2path / "git" / f"{epoch}.git"),
         "rev-list", "--count", ezpi.PI_HEAD],
        check=True, capture_output=True, text=True,
    )
    return int(result.stdout.strip())


# ---------------------------------------------------------------------------
# get_all_epochs
# ---------------------------------------------------------------------------

class TestGetAllEpochs:
    def test_missing_git_dir(self, tmp_path: Path) -> None:
        v2path = tmp_path / "nogit"
        v2path.mkdir()
        with pytest.raises(FileNotFoundError):
            ezpi.get_all_epochs(str(v2path))

    def test_single_epoch(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.init_v2_inbox(str(v2path))
        epochs = ezpi.get_all_epochs(str(v2path))
        assert epochs == [(0, str(v2path / "git" / "0.git"))]

    def test_sorted_ascending_ignores_garbage(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.init_v2_inbox(str(v2path))
        ezpi.init_epoch(str(v2path), 3)
        ezpi.init_epoch(str(v2path), 1)
        # Drop a non-matching directory to make sure it is ignored.
        (v2path / "git" / "notanepoch").mkdir()
        (v2path / "git" / "trash.git").mkdir()  # has .git suffix but not a number
        epochs = ezpi.get_all_epochs(str(v2path))
        nums = [e for e, _ in epochs]
        assert nums == [0, 1, 3]


# ---------------------------------------------------------------------------
# iter_messages (low-level)
# ---------------------------------------------------------------------------

class TestIterMessages:
    def test_empty_inbox_yields_nothing(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.init_v2_inbox(str(v2path))
        assert list(ezpi.iter_messages(str(v2path))) == []

    def test_full_walk_chronological(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        for i in range(3):
            ezpi.add_rfc822_v2(str(v2path), _make_msg(i))
        msgs = list(ezpi.iter_messages(str(v2path)))
        assert len(msgs) == 3
        # Chronological: msg 0, 1, 2
        for idx, (epoch, commit, body) in enumerate(msgs):
            assert epoch == 0
            assert f"msg-{idx}@example.com".encode() in body
            assert isinstance(commit, str) and len(commit) == 40

    def test_since_skips_earlier_commits(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        for i in range(4):
            ezpi.add_rfc822_v2(str(v2path), _make_msg(i))
        all_msgs = list(ezpi.iter_messages(str(v2path)))
        # Resume after the 2nd commit
        second_commit = all_msgs[1][1]
        resumed = list(ezpi.iter_messages(str(v2path), since={0: second_commit}))
        assert len(resumed) == 2
        assert b"msg-2@example.com" in resumed[0][2]
        assert b"msg-3@example.com" in resumed[1][2]

    def test_stale_since_raises(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.add_rfc822_v2(str(v2path), _make_msg(0))
        bogus = "0" * 40
        with pytest.raises(ezpi.StaleCommitError) as excinfo:
            list(ezpi.iter_messages(str(v2path), since={0: bogus}))
        assert excinfo.value.epoch == 0
        assert excinfo.value.commit == bogus

    def test_noop_commit_is_skipped(self, tmp_path: Path) -> None:
        """Commits without an ``m`` blob (purge/rm) are skipped silently."""
        v2path = tmp_path / "inbox"
        for i in range(2):
            ezpi.add_rfc822_v2(str(v2path), _make_msg(i))
        # Fabricate a noop commit: empty tree, parent = current HEAD
        epoch_path = str(v2path / "git" / "0.git")
        parent = subprocess.run(
            ["git", "--git-dir", epoch_path, "rev-parse", ezpi.PI_HEAD],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        empty_tree = subprocess.run(
            ["git", "--git-dir", epoch_path, "mktree"],
            input="", check=True, capture_output=True, text=True,
        ).stdout.strip()
        noop = subprocess.run(
            ["git", "--git-dir", epoch_path, "commit-tree", "-p", parent,
             "-m", "purge", empty_tree],
            check=True, capture_output=True, text=True,
            env={**os.environ,
                 "GIT_AUTHOR_NAME": "p", "GIT_AUTHOR_EMAIL": "p@e",
                 "GIT_COMMITTER_NAME": "p", "GIT_COMMITTER_EMAIL": "p@e"},
        ).stdout.strip()
        subprocess.run(
            ["git", "--git-dir", epoch_path, "update-ref", ezpi.PI_HEAD, noop],
            check=True,
        )
        # Add another real message after the noop.
        ezpi.add_rfc822(epoch_path, _make_msg(2))
        msgs = list(ezpi.iter_messages(str(v2path)))
        # We get the two originals plus msg-2, but not the noop.
        msgids = [m[2] for m in msgs]
        assert len(msgids) == 3
        assert all(b"msg-" in m for m in msgids)
        assert any(b"msg-2@example.com" in m for m in msgids)

    def test_walks_all_epochs_in_order(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.add_rfc822_v2(str(v2path), _make_msg(0))
        # Create a new epoch manually and add to it
        ezpi.init_epoch(str(v2path), 1)
        ezpi.add_rfc822(str(v2path / "git" / "1.git"), _make_msg(1))
        msgs = list(ezpi.iter_messages(str(v2path)))
        assert [m[0] for m in msgs] == [0, 1]
        assert b"msg-0" in msgs[0][2]
        assert b"msg-1" in msgs[1][2]


# ---------------------------------------------------------------------------
# Cursor state (load / save / reset)
# ---------------------------------------------------------------------------

class TestCursorState:
    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.init_v2_inbox(str(v2path))
        assert ezpi.load_cursor(str(v2path), "nope") is None

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.add_rfc822_v2(str(v2path), _make_msg(0))
        # Grab the commit we just wrote
        epoch, commit, body = next(iter(ezpi.iter_messages(str(v2path))))
        ezpi.save_cursor(str(v2path), "foo", epoch, commit, body)
        state = ezpi.load_cursor(str(v2path), "foo")
        assert state is not None
        assert state["cursor"] == "foo"
        entry = state["epochs"]["0"]
        assert entry["commit"] == commit
        assert entry["msgid"] == "<msg-0@example.com>"
        assert entry["subject"] == "Test message 0"
        # commit_date follows git %ci format: "YYYY-MM-DD HH:MM:SS +TZ"
        assert len(entry["commit_date"].split()) == 3

    def test_save_preserves_other_epochs(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.add_rfc822_v2(str(v2path), _make_msg(0))
        ezpi.init_epoch(str(v2path), 1)
        ezpi.add_rfc822(str(v2path / "git" / "1.git"), _make_msg(1))
        all_msgs = list(ezpi.iter_messages(str(v2path)))
        ezpi.save_cursor(str(v2path), "foo", *all_msgs[0])
        ezpi.save_cursor(str(v2path), "foo", *all_msgs[1])
        state = ezpi.load_cursor(str(v2path), "foo")
        assert state is not None
        assert set(state["epochs"].keys()) == {"0", "1"}

    def test_reset_cursor_removes_file(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.add_rfc822_v2(str(v2path), _make_msg(0))
        ep, commit, body = next(iter(ezpi.iter_messages(str(v2path))))
        ezpi.save_cursor(str(v2path), "foo", ep, commit, body)
        assert (v2path / "ezpi-cursor.foo.json").exists()
        ezpi.reset_cursor(str(v2path), "foo")
        assert not (v2path / "ezpi-cursor.foo.json").exists()
        # Idempotent
        ezpi.reset_cursor(str(v2path), "foo")

    def test_load_corrupt_raises(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.init_v2_inbox(str(v2path))
        (v2path / "ezpi-cursor.bad.json").write_text("{not json")
        with pytest.raises(ezpi.CursorStateError):
            ezpi.load_cursor(str(v2path), "bad")


# ---------------------------------------------------------------------------
# iter_new_messages (primary API)
# ---------------------------------------------------------------------------

class TestIterNewMessagesFirstUse:
    def test_first_use_head_yields_nothing_and_creates_state(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        for i in range(3):
            ezpi.add_rfc822_v2(str(v2path), _make_msg(i))
        got = list(ezpi.iter_new_messages(str(v2path), "foo"))
        assert got == []
        state = ezpi.load_cursor(str(v2path), "foo")
        assert state is not None
        assert "0" in state["epochs"]
        # State should point at current HEAD
        head = subprocess.run(
            ["git", "--git-dir", str(v2path / "git" / "0.git"), "rev-parse", ezpi.PI_HEAD],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert state["epochs"]["0"]["commit"] == head

    def test_first_use_head_then_subsequent_yields_only_new(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.add_rfc822_v2(str(v2path), _make_msg(0))
        # First use: start at HEAD, see nothing
        assert list(ezpi.iter_new_messages(str(v2path), "foo", auto_advance=True)) == []
        # Add more messages, then read again
        ezpi.add_rfc822_v2(str(v2path), _make_msg(1))
        ezpi.add_rfc822_v2(str(v2path), _make_msg(2))
        got = list(ezpi.iter_new_messages(str(v2path), "foo", auto_advance=True))
        assert len(got) == 2
        assert b"msg-1" in got[0][2]
        assert b"msg-2" in got[1][2]
        # Running again yields nothing (auto_advance persisted)
        assert list(ezpi.iter_new_messages(str(v2path), "foo", auto_advance=True)) == []

    def test_first_use_beginning_yields_everything(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        for i in range(3):
            ezpi.add_rfc822_v2(str(v2path), _make_msg(i))
        got = list(ezpi.iter_new_messages(str(v2path), "foo", start="beginning"))
        assert len(got) == 3

    def test_invalid_start_raises(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.init_v2_inbox(str(v2path))
        with pytest.raises(ValueError):
            list(ezpi.iter_new_messages(str(v2path), "foo", start="garbage"))


class TestIterNewMessagesManualAdvance:
    def test_manual_save_required(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        for i in range(3):
            ezpi.add_rfc822_v2(str(v2path), _make_msg(i))
        # Initialize at beginning, consume everything WITHOUT auto_advance
        seen = list(ezpi.iter_new_messages(str(v2path), "foo", start="beginning"))
        assert len(seen) == 3
        # No save_cursor calls -> state was never written -> running again still yields all 3
        # EXCEPT: iter_new_messages on first-use-beginning with existing state doesn't
        # seed the state file. We need to pretend the cursor already existed. Reset not needed.
        # But in this path, state wasn't written; so a second call is still first-use.
        seen2 = list(ezpi.iter_new_messages(str(v2path), "foo", start="beginning"))
        assert len(seen2) == 3

    def test_manual_save_persists_position(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        for i in range(3):
            ezpi.add_rfc822_v2(str(v2path), _make_msg(i))
        # Consume all, manually save after each
        seen = []
        for epoch, commit, body in ezpi.iter_new_messages(str(v2path), "foo", start="beginning"):
            ezpi.save_cursor(str(v2path), "foo", epoch, commit, body)
            seen.append((epoch, commit, body))
        assert len(seen) == 3
        # Now running again should yield nothing
        assert list(ezpi.iter_new_messages(str(v2path), "foo")) == []


class TestNewEpochRollover:
    def test_new_epoch_after_cursor_saved(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.add_rfc822_v2(str(v2path), _make_msg(0))
        # Advance cursor past everything in epoch 0
        assert list(ezpi.iter_new_messages(str(v2path), "foo", auto_advance=True)) == []
        # Add a new epoch and a message in it
        ezpi.init_epoch(str(v2path), 1)
        ezpi.add_rfc822(str(v2path / "git" / "1.git"), _make_msg(1))
        got = list(ezpi.iter_new_messages(str(v2path), "foo", auto_advance=True))
        assert len(got) == 1
        assert got[0][0] == 1
        assert b"msg-1" in got[0][2]


# ---------------------------------------------------------------------------
# Rebase recovery
# ---------------------------------------------------------------------------

class TestRebaseRecovery:
    def test_exact_match_after_rebase(self, tmp_path: Path) -> None:
        """Rewriting history with the same messages (new hashes) recovers
        silently by matching on (subject, msgid) and resumes without
        duplicating or skipping."""
        v2path = tmp_path / "inbox"
        messages = [_make_msg(i) for i in range(4)]
        for m in messages:
            ezpi.add_rfc822_v2(str(v2path), m)
        # Save cursor at message 1
        first_two = list(ezpi.iter_messages(str(v2path)))[:2]
        ezpi.save_cursor(str(v2path), "foo", *first_two[1])
        old_commit = first_two[1][1]
        # Rewrite history with the SAME messages -> new hashes, same headers
        _rewrite_history(v2path, 0, messages)
        # Call iter_new_messages -> detects stale, recovers via headers,
        # resumes yielding msg 2 and 3.
        got = list(ezpi.iter_new_messages(str(v2path), "foo", auto_advance=True))
        assert [int(b.split(b"msg-")[1].split(b"@")[0]) for _, _, b in got] == [2, 3]
        # Cursor state reflects recovery (new hash, not old)
        state = ezpi.load_cursor(str(v2path), "foo")
        assert state is not None
        new_commit = state["epochs"]["0"]["commit"]
        assert new_commit != old_commit
        # Last yielded was msg 3 (because auto_advance persisted state at each step)
        expected_msgid_at_cursor = "<msg-3@example.com>"
        assert state["epochs"]["0"]["msgid"] == expected_msgid_at_cursor

    def test_fallback_first_candidate_when_no_match(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """If the stored msgid has been scrubbed from history, recovery
        falls back to the first candidate at/after the stored commit_date
        and logs a warning."""
        v2path = tmp_path / "inbox"
        original = [_make_msg(i) for i in range(4)]
        for m in original:
            ezpi.add_rfc822_v2(str(v2path), m)
        # Cursor at message 1
        all_msgs = list(ezpi.iter_messages(str(v2path)))
        ezpi.save_cursor(str(v2path), "foo", *all_msgs[1])
        # Rewrite dropping message 1 entirely -> msgid gone from history
        survivors = [original[0], original[2], original[3]]
        _rewrite_history(v2path, 0, survivors)
        with caplog.at_level("WARNING", logger="ezpi"):
            got = list(ezpi.iter_new_messages(str(v2path), "foo", auto_advance=True))
        # We expect to resume at "first candidate at/after stored date" -- the earliest
        # rewritten commit whose date is >= the stored commit_date. The surviving set
        # is [0, 2, 3]; since cursor stored commit_date was near-real-time of the
        # original msg-1 commit, all three survivors qualify. Recovery picks the
        # earliest -> msg-0's replay -> yield from after it onward.
        # Concretely: recovered position points at survivors[0], so the iterator
        # yields survivors[1] and survivors[2] (msgs 2 and 3).
        assert any("no exact match after rebase" in rec.message for rec in caplog.records)
        msgids = [b.split(b"Message-Id: ")[1].split(b"\n")[0] for _, _, b in got]
        assert msgids == [b"<msg-2@example.com>", b"<msg-3@example.com>"]

    def test_empty_revlist_fast_forwards(self, tmp_path: Path) -> None:
        """If the stored commit_date is in the future of all commits in the
        rewritten repo, recovery fast-forwards to top."""
        v2path = tmp_path / "inbox"
        # Add one message with committer-date far in the future
        future_date = "Mon, 01 Jan 2099 12:00:00 +0000"
        _add_with_committer_date(v2path, _make_msg(0), future_date)
        all_msgs = list(ezpi.iter_messages(str(v2path)))
        # Save cursor at that future commit
        ezpi.save_cursor(str(v2path), "foo", *all_msgs[0])
        # Rewrite history with messages having committer-date in the past
        past_date = "Mon, 01 Jan 2000 12:00:00 +0000"
        epoch_path = str(v2path / "git" / "0.git")
        ezpi.git_run_command(epoch_path, ["update-ref", "-d", ezpi.PI_HEAD])
        for i in range(3):
            env = {
                "GIT_COMMITTER_NAME": ezpi.DEFAULT_NAME,
                "GIT_COMMITTER_EMAIL": ezpi.DEFAULT_ADDR,
                "GIT_COMMITTER_DATE": past_date,
            }
            ezpi.add_rfc822(epoch_path, _make_msg(100 + i), env=env)
        # Now rev-list --since-as-filter <future_date> returns empty.
        got = list(ezpi.iter_new_messages(str(v2path), "foo", auto_advance=True))
        # Fast-forward to top yields nothing (cursor is now at HEAD).
        assert got == []
        # Cursor state advanced to the new top
        state = ezpi.load_cursor(str(v2path), "foo")
        assert state is not None
        new_top = subprocess.run(
            ["git", "--git-dir", epoch_path, "rev-parse", ezpi.PI_HEAD],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert state["epochs"]["0"]["commit"] == new_top

    def test_recover_cursor_explicit_api(self, tmp_path: Path) -> None:
        """The recover_cursor public helper can be invoked directly."""
        v2path = tmp_path / "inbox"
        messages = [_make_msg(i) for i in range(3)]
        for m in messages:
            ezpi.add_rfc822_v2(str(v2path), m)
        all_msgs = list(ezpi.iter_messages(str(v2path)))
        ezpi.save_cursor(str(v2path), "foo", *all_msgs[1])
        old_commit = all_msgs[1][1]
        _rewrite_history(v2path, 0, messages)
        new_commit = ezpi.recover_cursor(str(v2path), "foo", 0)
        assert new_commit != old_commit
        # State updated in place
        state = ezpi.load_cursor(str(v2path), "foo")
        assert state is not None
        assert state["epochs"]["0"]["commit"] == new_commit

    def test_recover_cursor_missing_state_raises(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.init_v2_inbox(str(v2path))
        with pytest.raises(ezpi.CursorStateError):
            ezpi.recover_cursor(str(v2path), "foo", 0)


# ---------------------------------------------------------------------------
# _peek_cursor_headers helper
# ---------------------------------------------------------------------------

class TestPeekHeaders:
    def test_populated(self) -> None:
        headers = ezpi._peek_cursor_headers(_make_msg(7))
        assert headers["msgid"] == "<msg-7@example.com>"
        assert headers["subject"] == "Test message 7"

    def test_missing_fields_use_sentinels(self) -> None:
        raw = b"From: a@b\n\nbody\n"
        headers = ezpi._peek_cursor_headers(raw)
        assert headers["msgid"] == "(no message-id)"
        assert headers["subject"] == "(no subject)"


# ---------------------------------------------------------------------------
# Cursor file shape sanity
# ---------------------------------------------------------------------------

class TestCursorFileShape:
    def test_json_shape(self, tmp_path: Path) -> None:
        v2path = tmp_path / "inbox"
        ezpi.add_rfc822_v2(str(v2path), _make_msg(0))
        ep, commit, body = next(iter(ezpi.iter_messages(str(v2path))))
        ezpi.save_cursor(str(v2path), "myapp", ep, commit, body)
        path = v2path / "ezpi-cursor.myapp.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["cursor"] == "myapp"
        assert set(data.keys()) == {"cursor", "updated", "epochs"}
        assert set(data["epochs"]["0"].keys()) == {"commit", "msgid", "subject", "commit_date"}
