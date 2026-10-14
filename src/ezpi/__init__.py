#!/usr/bin/env python3
#
# Copyright (C) 2020 by The Linux Foundation
# SPDX-License-Identifier: MIT-0
#
"""EZPI - A library for reading and writing RFC2822 messages in public-inbox repositories.

This module provides functions to add email messages to public-inbox v2
format repositories and to consume them back out again. For writing,
messages can be provided as raw bytes or email.message.Message objects;
the v2 inbox structure is created automatically and epochs are rotated
based on size or annual boundaries. For reading, named cursors track
per-reader position across epochs with automatic new-epoch detection
and rebase recovery. EZPI itself never fetches from remotes -- refresh
the local checkout with git, lei, rsync, or whatever tool fits your
workflow.

Example usage::

    import ezpi

    # Add an RFC822 message from bytes (creates inbox if needed)
    with open('message.eml', 'rb') as f:
        ezpi.add_rfc822_v2('/path/to/inbox', f.read())

    # Add an RFC822 message from a Message object
    import email.message
    msg = email.message.EmailMessage()
    msg['From'] = 'sender@example.com'
    msg['Subject'] = 'Test'
    msg.set_content('Hello world')
    ezpi.add_rfc822_v2('/path/to/inbox', msg)

    # Use annual epoch rotation (new epoch each January)
    ezpi.add_rfc822_v2('/path/to/inbox', msg, auto_epoch='annual')

    # Read new messages since the last call for a named cursor
    for epoch, commit, msg_bytes in ezpi.iter_new_messages(
        '/path/to/inbox', 'myapp', auto_advance=True,
    ):
        process(msg_bytes)
"""

from __future__ import annotations

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import contextlib
import datetime
import email
import email.header
import email.message
import email.policy
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Iterator, Sequence
from email import charset
from email.utils import formatdate, make_msgid, mktime_tz, parseaddr, parsedate_tz
from fcntl import LOCK_EX, flock
from typing import Any

# Optional pygit2 support
try:
    import pygit2

    HAS_PYGIT2 = True
except ImportError:
    pygit2 = None  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    HAS_PYGIT2 = False

charset.add_charset('utf-8', charset.SHORTEST)

logger = logging.getLogger(__name__)

DEFAULT_NAME = 'EZ PI'
DEFAULT_ADDR = 'ezpi@localhost'
DEFAULT_SUBJ = 'EZPI commit'

# Set our own policy
EMLPOLICY = email.policy.EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None)

# This shouldn't change
PI_HEAD = 'refs/heads/master'

# My version
__VERSION__ = '0.7.0'


class CursorError(RuntimeError):
    """Base class for cursor-related errors."""


class CursorStateError(CursorError):
    """Cursor state file is missing a required field or cannot be parsed."""


class StaleCommitError(CursorError):
    """A ``since`` commit hash given to :func:`iter_messages` is not present
    in the repo (history was rewritten). Carries the epoch and stored commit
    so the cursor-aware layer can trigger recovery.
    """

    def __init__(self, epoch: int, commit: str) -> None:
        super().__init__(f'Commit {commit} is not in epoch {epoch}')
        self.epoch = epoch
        self.commit = commit


def _use_pygit2() -> bool:
    """Check if pygit2 should be used for git operations.

    Returns True if pygit2 is available and not disabled via environment
    variable EZPI_USE_GIT_SUBPROCESS=1.
    """
    if not HAS_PYGIT2:
        return False
    return os.environ.get('EZPI_USE_GIT_SUBPROCESS', '') != '1'


def git_run_command(
    gitdir: str,
    args: list[str],
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a git command and return its output.

    Args:
        gitdir: Path to the git repository (sets GIT_DIR environment variable).
        args: List of arguments to pass to git (without 'git' itself).
        stdin: Optional bytes to send to the command's stdin.
        env: Optional environment variables to set for the command.

    Returns:
        A tuple of (return_code, stdout_bytes, stderr_bytes).
    """
    if not env:
        env = {}
    if gitdir:
        env['GIT_DIR'] = gitdir
    full_args = ['git', '--no-pager', *args]
    logger.debug('Running %s', ' '.join(full_args))
    pp = subprocess.Popen(full_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    (output, error) = pp.communicate(input=stdin)

    return pp.returncode, output, error


def _pygit2_init_repo(path: str) -> None:
    """Initialize a bare git repository using pygit2.

    Args:
        path: Path where the bare repository should be created.
    """
    pygit2.init_repository(path, bare=True)
    logger.debug('Initialized bare repo at %s (pygit2)', path)


def _parse_git_env_date(date_str: str | None) -> tuple[int, int] | None:
    """Parse a ``GIT_*_DATE`` env-format date into ``(unix_ts, tz_minutes)``.

    Accepts the two formats git itself accepts for these env vars: RFC 2822
    (as produced by :func:`email.utils.formatdate`) and git's ``%ci`` ISO
    form (``YYYY-MM-DD HH:MM:SS +ZZZZ``). Returns None if unparseable, in
    which case callers should fall back to the current wall-clock time.
    """
    if not date_str:
        return None
    parsed = parsedate_tz(date_str)
    if parsed is not None and parsed[9] is not None:
        try:
            return int(mktime_tz(parsed)), (parsed[9] or 0) // 60
        except (OverflowError, ValueError):
            pass
    for fmt in ('%Y-%m-%d %H:%M:%S %z', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            dt = datetime.datetime.strptime(date_str, fmt)  # noqa: DTZ007  (both formats include %z)
        except ValueError:
            continue
        offset = dt.utcoffset()
        offset_min = int(offset.total_seconds() // 60) if offset is not None else 0
        return int(dt.timestamp()), offset_min
    return None


def _pygit2_signature(name: str, email_addr: str, date_str: str | None) -> Any:
    """Build a pygit2.Signature honoring ``GIT_*_DATE`` env format if given."""
    parsed = _parse_git_env_date(date_str)
    if parsed is None:
        return pygit2.Signature(name, email_addr)
    ts, offset = parsed
    return pygit2.Signature(name, email_addr, time=ts, offset=offset)


def _pygit2_write_commit(repo: str, env: dict[str, str], c_msg: str, body: bytes, dest: str = 'm') -> None:
    """Create a git commit using pygit2.

    Args:
        repo: Path to the bare git repository.
        env: Environment variables for the git commit (GIT_AUTHOR_*, GIT_COMMITTER_*).
        c_msg: The commit message.
        body: The file content to store.
        dest: Filename for the blob in the tree.
    """
    repository = pygit2.Repository(repo)

    # Create blob
    blob_id = repository.create_blob(body)

    # Build tree
    tb = repository.TreeBuilder()
    tb.insert(dest, blob_id, 0o100644)  # GIT_FILEMODE_BLOB
    tree_id = tb.write()

    # Create signatures, honoring GIT_*_DATE env vars (matches subprocess path).
    author = _pygit2_signature(
        env.get('GIT_AUTHOR_NAME', DEFAULT_NAME),
        env.get('GIT_AUTHOR_EMAIL', DEFAULT_ADDR),
        env.get('GIT_AUTHOR_DATE'),
    )
    committer = _pygit2_signature(
        env.get('GIT_COMMITTER_NAME', DEFAULT_NAME),
        env.get('GIT_COMMITTER_EMAIL', DEFAULT_ADDR),
        env.get('GIT_COMMITTER_DATE'),
    )

    # Get parent commit if exists
    parents: Sequence[str] = []
    try:
        ref = repository.references.get(PI_HEAD)
        if ref is not None:
            parents = [str(ref.target)]
    except KeyError:
        pass

    # Create commit
    commit_id = repository.create_commit(
        PI_HEAD,
        author,
        committer,
        c_msg,
        tree_id,
        parents,
    )
    logger.debug('Created commit %s (pygit2)', commit_id)


def _pygit2_get_latest_commit_time(repo_path: str) -> int | None:
    """Get the timestamp of the latest commit using pygit2.

    Args:
        repo_path: Path to the bare git repository.

    Returns:
        Unix timestamp of the latest commit, or None if no commits exist.
    """
    try:
        repository = pygit2.Repository(repo_path)
        ref = repository.references.get(PI_HEAD)
        if ref is None:
            return None
        commit = repository.get(ref.target)
        if commit is None:
            return None
        return int(commit.commit_time)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    except (KeyError, pygit2.GitError):
        return None


def check_valid_repo(repo: str) -> None:
    """Verify that a path is a valid bare git repository.

    Args:
        repo: Path to the repository to check.

    Raises:
        FileNotFoundError: If the path doesn't exist or isn't a valid bare git repo.
    """
    if not os.path.isdir(repo):
        raise FileNotFoundError(f'Path does not exist: {repo}')
    musts = ['objects', 'refs']
    for must in musts:
        if not os.path.exists(os.path.join(repo, must)):
            raise FileNotFoundError(f'Path is not a valid bare git repository: {repo}')


@contextlib.contextmanager
def _exclusive_lock(path: str) -> Generator[None, None, None]:
    """Hold an exclusive lock on ``path`` for the duration of the block.

    This must be flock(2), not lockf(3): public-inbox locks inbox.lock with
    flock(2), and the two lock types never block each other on Linux. Also,
    lockf(3) locks belong to the process, so they do not exclude other
    threads, and closing any other descriptor of the file drops them.

    Uses a raw descriptor rather than ``open()`` because nothing is ever
    read from or written to the lock file; closing it also drops the lock,
    so no separate unlock call is needed.

    Raises:
        RuntimeError: If the lock file cannot be opened or locked.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    except OSError as exc:
        raise RuntimeError(f'Could not obtain an exclusive lock on {path}') from exc
    try:
        try:
            flock(fd, LOCK_EX)
        except OSError as exc:
            raise RuntimeError(f'Could not obtain an exclusive lock on {path}') from exc
        yield
    finally:
        os.close(fd)


def git_write_commit(repo: str, env: dict[str, str], c_msg: str, body: bytes, dest: str = 'm') -> None:
    """Create a git commit containing a single file with the given content.

    This is a low-level function that creates git objects (blob, tree, commit).
    Uses pygit2 when available, otherwise falls back to git subprocess commands.
    The commit is made to refs/heads/master.

    Args:
        repo: Path to the bare git repository.
        env: Environment variables for the git commit (GIT_AUTHOR_*, GIT_COMMITTER_*).
        c_msg: The commit message (typically the email subject).
        body: The file content to store (typically the serialized email).
        dest: Filename for the blob in the tree (default: 'm').

    Raises:
        FileNotFoundError: If the repository path is invalid.
        RuntimeError: If any git operation fails or lock cannot be acquired.
    """
    check_valid_repo(repo)
    # The lock shouldn't be held open for very long, so wait without a timeout
    with _exclusive_lock(os.path.join(repo, 'ezpi.lock')):
        if _use_pygit2():
            _pygit2_write_commit(repo, env, c_msg, body, dest)
        else:
            # Create a blob first
            ee, out, err = git_run_command(repo, ['hash-object', '-w', '--stdin'], stdin=body)
            if ee > 0:
                raise RuntimeError(f'Could not create a blob in {repo}: {err.decode()}')
            blob = out.strip(b'\n')
            # Create a tree object now
            treeline = b'100644 blob ' + blob + b'\t' + dest.encode()
            # Now mktree
            ee, out, err = git_run_command(repo, ['mktree'], stdin=treeline)
            if ee > 0:
                raise RuntimeError(f'Could not mktree in {repo}: {err.decode()}')
            tree = out.decode().strip()
            # Find out if we are the first commit or not
            ee, out, err = git_run_command(repo, ['rev-parse', f'{PI_HEAD}^0'])
            if ee > 0:
                commit_args = ['commit-tree', '-m', c_msg, tree]
            else:
                commit_args = ['commit-tree', '-p', PI_HEAD, '-m', c_msg, tree]
            # Commit the tree
            ee, out, err = git_run_command(repo, commit_args, env=env)
            if ee > 0:
                raise RuntimeError(f'Could not commit-tree in {repo}: {err.decode()}')
            # Finally, update the ref
            commit = out.decode().strip()
            ee, out, err = git_run_command(repo, ['update-ref', PI_HEAD, commit])
            if ee > 0:
                raise RuntimeError(f'Could not update-ref in {repo}: {err.decode()}')


def add_plaintext(
    repo: str,
    content: str,
    subject: str,
    authorname: str,
    authoremail: str,
    domain: str | None = None,
) -> None:
    """Add plaintext content to the repository as an RFC822 message.

    This is a convenience wrapper that creates a minimal RFC822 message
    from plaintext content and adds it to the repository.

    Args:
        repo: Path to the bare git repository.
        content: The plaintext message body.
        subject: The email subject line.
        authorname: Display name for the From header.
        authoremail: Email address for the From header.
        domain: Optional domain for generating the Message-Id.

    Raises:
        FileNotFoundError: If the repository path is invalid.
        RuntimeError: If the git commit operation fails.

    Example::

        ezpi.add_plaintext(
            '/path/to/repo.git',
            content='Hello, world!',
            subject='Greeting',
            authorname='John Doe',
            authoremail='john@example.com',
        )
    """
    m = f'From: {authorname} <{authoremail}>\nSubject: {subject}\n\n' + content
    add_rfc822(repo, m.encode(), domain=domain)


def clean_header(hdrval: str) -> str:
    """Decode and clean an email header value.

    Handles RFC2047 encoded headers (e.g., =?utf-8?q?...?=) and normalizes
    whitespace. Invalid encodings are handled gracefully with replacement.

    Args:
        hdrval: The raw header value to decode.

    Returns:
        The decoded and cleaned header value as a string.
    """
    decoded = ''
    for hstr, hcs in email.header.decode_header(hdrval):
        if hcs is None:
            hcs = 'utf-8'
        try:
            decoded += hstr.decode(hcs, errors='replace')
        except LookupError:
            # Try as utf-8
            decoded += hstr.decode('utf-8', errors='replace')
        except (UnicodeDecodeError, AttributeError):
            decoded += hstr
    new_hdrval = re.sub(r'\n?\s+', ' ', decoded)
    return new_hdrval.strip()


def add_rfc822(
    repo: str,
    content: email.message.Message | bytes,
    domain: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Add an RFC822 message to the repository.

    This is the main entry point for adding email messages to a public-inbox
    repository. The message can be provided as raw bytes or as an
    email.message.Message object.

    The function automatically:

    - Adds Date header if missing
    - Generates Message-Id if missing
    - Fixes charset for non-ASCII content
    - Extracts author info from the From header

    Args:
        repo: Path to the bare git repository.
        content: The email message as bytes or a Message object.
        domain: Optional domain for generating the Message-Id if missing.
        env: Optional git environment variables for the commit.

    Raises:
        ValueError: If the message is missing required From or Subject headers.
        FileNotFoundError: If the repository path is invalid.
        RuntimeError: If the git commit operation fails.

    Example::

        # From bytes
        ezpi.add_rfc822('/path/to/repo.git', email_bytes)

        # From Message object
        msg = email.message.EmailMessage()
        msg['From'] = 'sender@example.com'
        msg['Subject'] = 'Test'
        msg.set_content('Hello')
        ezpi.add_rfc822('/path/to/repo.git', msg)
    """
    msg: email.message.Message
    if isinstance(content, bytes):
        msg = email.message_from_bytes(content, policy=EMLPOLICY)
    else:
        msg = content

    # Make sure we have at least a From and a subject
    raw_subject = msg.get('Subject')
    if raw_subject is None:
        raise ValueError('Message must contain a valid Subject header')
    h_subject = clean_header(raw_subject)
    if not h_subject:
        raise ValueError('Message must contain a valid Subject header')

    raw_from = msg.get('From')
    if raw_from is None:
        raise ValueError('Message must contain a valid From header')
    h_from = clean_header(raw_from)
    if not h_from:
        raise ValueError('Message must contain a valid From header')
    parts = parseaddr(h_from)
    a_name = parts[0]
    a_email = parts[1]
    if not a_name:
        a_name = DEFAULT_NAME

    h_date = msg.get('Date')
    if not h_date:
        h_date = formatdate()
        msg.add_header('Date', h_date)

    if not msg.get('Message-Id'):
        msgid = make_msgid(domain=domain)
        msg.add_header('Message-Id', msgid)
        logger.debug('Added a message-id: %s', msgid)

    # Ensure text messages with non-ASCII content have proper charset set.
    # This handles cases where external callers pass Message objects with
    # Unicode content but incorrect/missing charset (e.g., claiming us-ascii).
    if msg.get_content_maintype() == 'text':
        payload = msg.get_payload()
        if isinstance(payload, str) and not payload.isascii():
            # Re-encode payload as UTF-8 bytes and set proper charset
            msg.set_payload(payload.encode('utf-8'))
            msg.set_charset('utf-8')
        elif not msg.get_content_charset():
            msg.set_charset('utf-8')

    body = msg.as_bytes(policy=EMLPOLICY)

    if env is None:
        env = {
            'GIT_COMMITTER_NAME': DEFAULT_NAME,
            'GIT_COMMITTER_EMAIL': DEFAULT_ADDR,
            'GIT_COMMITTER_DATE': formatdate(),
        }
    env['GIT_AUTHOR_NAME'] = a_name
    env['GIT_AUTHOR_EMAIL'] = a_email
    env['GIT_AUTHOR_DATE'] = h_date

    git_write_commit(repo, env, h_subject, body)


def run_hook(repo: str) -> None:
    """Run the post-commit hook if it exists and is executable.

    The hook is ``hooks/post-commit`` inside ``repo`` and runs with
    ``repo`` as its working directory.

    Args:
        repo: Path to the bare git repository, or to the v2 inbox
            directory. A v2 inbox keeps its hook at the inbox level, so
            that it survives epoch rotation.
    """
    hookpath = os.path.join(repo, 'hooks', 'post-commit')
    if os.access(hookpath, os.X_OK):
        logger.debug('Running %s', hookpath)
        curdir = os.getcwd()
        os.chdir(repo)
        pp = subprocess.Popen(['hooks/post-commit'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (_output, error) = pp.communicate()
        if pp.returncode > 0:
            logger.critical('Running post-commit hook failed')
            logger.critical('STDERR follows')
            logger.critical(error.decode())
        os.chdir(curdir)


# v2 format constants
V2_SIZE_THRESHOLD = 1024 * 1024 * 1024  # 1GB


def init_epoch(v2path: str, epoch: int) -> str:
    """Create a new epoch repository in a v2 inbox.

    Creates a bare git repository at v2path/git/{epoch}.git and updates
    the alternates file in all.git to include the new epoch.

    Args:
        v2path: Path to the v2 inbox directory.
        epoch: Epoch number (0-based integer).

    Returns:
        Path to the newly created epoch repository.

    Raises:
        RuntimeError: If git init fails.
    """
    epoch_path = os.path.join(v2path, 'git', f'{epoch}.git')
    if _use_pygit2():
        _pygit2_init_repo(epoch_path)
    else:
        ee, _out, err = git_run_command('', ['init', '--bare', epoch_path])
        if ee > 0:
            raise RuntimeError(f'Could not init epoch {epoch}: {err.decode()}')

    # Update all.git/objects/info/alternates
    all_git = os.path.join(v2path, 'all.git')
    alternates_dir = os.path.join(all_git, 'objects', 'info')
    alternates_file = os.path.join(alternates_dir, 'alternates')

    # Read existing alternates if present
    existing_lines: list[str] = []
    if os.path.exists(alternates_file):
        with open(alternates_file) as f:
            existing_lines = [line.strip() for line in f if line.strip()]

    # Add new epoch's objects directory (relative path from all.git/objects)
    new_entry = f'../../git/{epoch}.git/objects'
    if new_entry not in existing_lines:
        existing_lines.append(new_entry)
        os.makedirs(alternates_dir, exist_ok=True)
        with open(alternates_file, 'w') as f:
            f.write('\n'.join(existing_lines) + '\n')

    logger.debug('Created epoch %d at %s', epoch, epoch_path)
    return epoch_path


def init_v2_inbox(v2path: str) -> str:
    """Initialize a new public-inbox v2 format inbox.

    Creates the v2 directory structure including:
    - inbox.lock file for global locking
    - git/ directory for epoch repositories
    - git/0.git as the first epoch
    - all.git/ as read-only endpoint with alternates

    Args:
        v2path: Path where the v2 inbox should be created.

    Returns:
        Path to the first epoch repository (git/0.git).

    Raises:
        RuntimeError: If any git operation fails.
        FileExistsError: If v2path already exists.
    """
    if os.path.exists(v2path):
        raise FileExistsError(f'Path already exists: {v2path}')

    # Create directory structure
    os.makedirs(v2path)
    os.makedirs(os.path.join(v2path, 'git'))

    # Create inbox.lock
    open(os.path.join(v2path, 'inbox.lock'), 'w').close()

    # Create all.git as empty bare repo
    all_git = os.path.join(v2path, 'all.git')
    if _use_pygit2():
        _pygit2_init_repo(all_git)
    else:
        ee, _out, err = git_run_command('', ['init', '--bare', all_git])
        if ee > 0:
            raise RuntimeError(f'Could not init all.git: {err.decode()}')

    # Create first epoch (this also sets up alternates)
    epoch_path = init_epoch(v2path, 0)

    logger.debug('Initialized v2 inbox at %s', v2path)
    return epoch_path


def get_latest_epoch(v2path: str) -> tuple[int, str]:
    """Find the highest numbered epoch in a v2 inbox.

    Args:
        v2path: Path to the v2 inbox directory.

    Returns:
        Tuple of (epoch_number, epoch_path).

    Raises:
        FileNotFoundError: If no epochs exist.
    """
    git_dir = os.path.join(v2path, 'git')
    if not os.path.isdir(git_dir):
        raise FileNotFoundError(f'No git directory in v2 inbox: {v2path}')

    max_epoch = -1
    for entry in os.listdir(git_dir):
        if entry.endswith('.git'):
            try:
                epoch_num = int(entry[:-4])
                if epoch_num > max_epoch:
                    max_epoch = epoch_num
            except ValueError:
                continue

    if max_epoch < 0:
        raise FileNotFoundError(f'No epochs found in v2 inbox: {v2path}')

    epoch_path = os.path.join(git_dir, f'{max_epoch}.git')
    return max_epoch, epoch_path


def get_all_epochs(v2path: str) -> list[tuple[int, str]]:
    """List all epochs in a v2 inbox, sorted ascending by epoch number.

    Args:
        v2path: Path to the v2 inbox directory.

    Returns:
        A list of (epoch_number, epoch_path) tuples. Empty if no epochs exist.

    Raises:
        FileNotFoundError: If the inbox has no git/ directory.
    """
    git_dir = os.path.join(v2path, 'git')
    if not os.path.isdir(git_dir):
        raise FileNotFoundError(f'No git directory in v2 inbox: {v2path}')

    found: list[tuple[int, str]] = []
    for entry in os.listdir(git_dir):
        if not entry.endswith('.git'):
            continue
        try:
            epoch_num = int(entry[:-4])
        except ValueError:
            continue
        found.append((epoch_num, os.path.join(git_dir, entry)))

    found.sort(key=lambda x: x[0])
    return found


def get_epoch_size(epoch_path: str) -> int:
    """Calculate the total size of a git epoch repository.

    Sums the size of pack files and loose objects.

    Args:
        epoch_path: Path to the epoch bare git repository.

    Returns:
        Total size in bytes.
    """
    total_size = 0
    objects_dir = os.path.join(epoch_path, 'objects')

    if not os.path.isdir(objects_dir):
        return 0

    # Sum pack files
    pack_dir = os.path.join(objects_dir, 'pack')
    if os.path.isdir(pack_dir):
        for entry in os.listdir(pack_dir):
            if entry.endswith('.pack'):
                total_size += os.path.getsize(os.path.join(pack_dir, entry))

    # Sum loose objects (two-character directories)
    for entry in os.listdir(objects_dir):
        if len(entry) == 2 and entry not in ('info', 'pack'):
            subdir = os.path.join(objects_dir, entry)
            if os.path.isdir(subdir):
                for obj in os.listdir(subdir):
                    total_size += os.path.getsize(os.path.join(subdir, obj))

    return total_size


def should_rotate_epoch(epoch_path: str, mode: str) -> bool:
    """Check if a new epoch should be created.

    Args:
        epoch_path: Path to the current epoch repository.
        mode: Rotation mode - 'size' or 'annual'.

    Returns:
        True if a new epoch should be created.
    """
    if mode == 'size':
        return get_epoch_size(epoch_path) >= V2_SIZE_THRESHOLD

    if mode == 'annual':
        # Check if latest commit is from a previous year
        import datetime

        if _use_pygit2():
            timestamp = _pygit2_get_latest_commit_time(epoch_path)
            if timestamp is None:
                return False
        else:
            ee, out, _err = git_run_command(epoch_path, ['log', '-1', '--format=%ct', PI_HEAD])
            if ee > 0:
                # No commits yet, no rotation needed
                return False
            try:
                timestamp = int(out.decode().strip())
            except (ValueError, OSError):
                return False
        try:
            tz = datetime.timezone.utc
            commit_year = datetime.datetime.fromtimestamp(timestamp, tz=tz).year
            current_year = datetime.datetime.now(tz=tz).year
            return commit_year < current_year
        except (ValueError, OSError):
            return False

    return False


def _create_v2_inbox(v2path: str) -> None:
    """Create a v2 inbox so that no other process ever sees it half-made.

    Several writers (e.g. webhook workers) can find the inbox missing at
    the same time. inbox.lock cannot make them wait, because it lives
    inside the inbox. So each writer builds its own copy next to the
    target and renames it into place. rename(2) is atomic, and it fails
    when the target is a non-empty directory, so exactly one copy wins
    and the others use it. The all.git alternates are relative paths, so
    moving the inbox does not break them.
    """
    parent = os.path.dirname(os.path.abspath(v2path))
    os.makedirs(parent, exist_ok=True)
    # Same parent directory, because rename(2) is only atomic within one
    # filesystem. A killed writer leaves only this directory behind, never
    # a half-made inbox at v2path.
    tmpdir = tempfile.mkdtemp(dir=parent, prefix='.ezpi-init-')
    try:
        tmp_inbox = os.path.join(tmpdir, 'inbox')
        init_v2_inbox(tmp_inbox)
        try:
            os.rename(tmp_inbox, v2path)
        except OSError:
            if not os.path.isdir(v2path):
                raise
            logger.debug('Another writer created %s first, using it', v2path)
        else:
            logger.info('Initialized v2 inbox at %s', v2path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def add_rfc822_v2(
    v2path: str,
    content: email.message.Message | bytes,
    domain: str | None = None,
    env: dict[str, str] | None = None,
    auto_epoch: str = 'size',
) -> None:
    """Add an RFC822 message to a public-inbox v2 format repository.

    This function manages the v2 inbox structure, creating it if necessary
    and handling epoch rotation based on the specified mode.

    Args:
        v2path: Path to the v2 inbox directory.
        content: The email message as bytes or a Message object.
        domain: Optional domain for generating the Message-Id if missing.
        env: Optional git environment variables.
        auto_epoch: Epoch rotation mode - 'size' (default) or 'annual'.

    Raises:
        ValueError: If the message is missing required headers.
        RuntimeError: If any git operation fails, the lock cannot be
            acquired, or the inbox cannot be created or has no epochs.

    Example::

        ezpi.add_rfc822_v2('/path/to/inbox', email_bytes)
        ezpi.add_rfc822_v2('/path/to/inbox', msg, auto_epoch='annual')
    """
    if not os.path.exists(v2path):
        try:
            _create_v2_inbox(v2path)
        except OSError as exc:
            raise RuntimeError(f'Could not create v2 inbox at {v2path}') from exc
    with _exclusive_lock(os.path.join(v2path, 'inbox.lock')):
        try:
            epoch_num, epoch_path = get_latest_epoch(v2path)
        except FileNotFoundError as exc:
            raise RuntimeError(str(exc)) from exc

        # Check if rotation is needed
        if should_rotate_epoch(epoch_path, auto_epoch):
            new_epoch = epoch_num + 1
            epoch_path = init_epoch(v2path, new_epoch)
            logger.info('Rotated to new epoch %d', new_epoch)

        # Write to the epoch
        add_rfc822(epoch_path, content, domain=domain, env=env)


# ---------------------------------------------------------------------------
# Reading public-inbox v2 repositories
# ---------------------------------------------------------------------------
#
# The design and algorithms in this section mirror korgalore's pi_feed.py
# (https://git.kernel.org/pub/scm/utils/korg-helpers/korgalore.git). Fallback
# choices in particular (rebase recovery, noop handling) are deliberate and
# should not be "improved" without understanding what they trade off.

# Sentinel values used when a message is missing a Subject or Message-ID.
# These strings match korgalore's so recovery comparisons are bit-identical.
_NO_MSGID = '(no message-id)'
_NO_SUBJECT = '(no subject)'


def _peek_cursor_headers(msg_bytes: bytes) -> dict[str, str]:
    """Extract msgid+subject from raw message bytes for cursor state.

    Uses the stdlib email parser with the compat32 policy (what public-inbox
    treats as canonical). Missing fields fall back to the same sentinels
    korgalore uses so rebase-recovery comparisons are identical.
    """
    msg = email.message_from_bytes(msg_bytes, policy=email.policy.compat32)
    subject = msg.get('Subject', _NO_SUBJECT) or _NO_SUBJECT
    msgid = msg.get('Message-ID', _NO_MSGID) or _NO_MSGID
    return {'subject': str(subject), 'msgid': str(msgid)}


def _commit_date(gitdir: str, commit: str) -> str:
    """Return the committer date of ``commit`` in git's ``%ci`` format.

    Format example: ``2026-04-23 14:29:50 +0000``. This is what the
    rebase-recovery search uses as its ``--since-as-filter`` anchor.
    """
    retcode, out, err = git_run_command(gitdir, ['log', '-1', '--format=%ci', commit])
    if retcode != 0:
        raise CursorStateError(f'Could not read commit date for {commit}: {err.decode(errors="replace")}')
    return out.decode().strip()


def _is_on_current_history(gitdir: str, commit: str) -> bool:
    """Return True if ``commit`` is reachable from :data:`PI_HEAD`.

    We intentionally diverge from korgalore's ``cat-file -e <commit>^`` probe
    here: that probe only fires once unreachable objects have been pruned by
    ``git gc``, so it misses a rebase until the next maintenance sweep.
    ``merge-base --is-ancestor`` asks the right question -- "is this hash
    on the current history?" -- regardless of object-store state.
    """
    retcode, _out, _err = git_run_command(gitdir, ['merge-base', '--is-ancestor', commit, PI_HEAD])
    return retcode == 0


def _top_commit(gitdir: str) -> str | None:
    """Return the current HEAD commit hash, or None if the repo has no commits."""
    retcode, out, _err = git_run_command(gitdir, ['rev-parse', PI_HEAD])
    if retcode != 0:
        return None
    return out.decode().strip() or None


class _BatchCatFile:
    """Persistent ``git cat-file --batch`` pipe for bulk object reads.

    Walking a public-inbox epoch used to fork three short-lived ``git``
    processes per yielded message (existence check + m-blob check + blob
    read). Over a real inbox that cost dominates wall time. Keeping a single
    ``cat-file --batch`` pipe open for the duration of the walk collapses
    every per-message check into one or two line-based I/O round trips.

    Always use as a context manager. When wrapped in a generator, the pipe
    is torn down deterministically on generator close -- including the
    implicit close that runs when the caller ``break``\\ s out of iteration
    or abandons the generator to garbage collection.
    """

    def __init__(self, gitdir: str) -> None:
        # --buffer is deliberately NOT passed so each line written to stdin
        # triggers an immediate response; see git-cat-file(1) BATCH OUTPUT.
        self._proc = subprocess.Popen(
            ['git', '--git-dir', gitdir, '--no-pager', 'cat-file', '--batch'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def read(self, spec: str) -> bytes | None:
        """Fetch the object referenced by ``spec`` (e.g. ``<commit>:m``).

        Returns the raw object bytes, or ``None`` if git reports the spec as
        missing. Raises :class:`CursorError` if the pipe is closed or the
        framing is corrupt.
        """
        stdin = self._proc.stdin
        stdout = self._proc.stdout
        assert stdin is not None and stdout is not None
        stdin.write(spec.encode() + b'\n')
        stdin.flush()
        header = stdout.readline()
        if not header:
            raise CursorError('git cat-file --batch pipe closed unexpectedly')
        if header.endswith(b' missing\n'):
            return None
        # Header is "<oid> <type> <size>\n".
        try:
            size = int(header.rstrip(b'\n').rsplit(b' ', 1)[-1])
        except (ValueError, IndexError) as exc:
            raise CursorError(f'Unparseable cat-file batch header: {header!r}') from exc
        # Pipes are free to short-read; loop until we have the full payload.
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = stdout.read(remaining)
            if not chunk:
                raise CursorError('git cat-file --batch short read')
            chunks.append(chunk)
            remaining -= len(chunk)
        # Each object is followed by a single LF in the batch stream.
        trailer = stdout.read(1)
        if trailer != b'\n':
            raise CursorError(f'Missing object trailer in cat-file batch stream: {trailer!r}')
        return b''.join(chunks)

    def close(self) -> None:
        """Terminate the underlying git process. Idempotent and safe."""
        if self._proc.poll() is None:
            # Closing stdin is what tells cat-file --batch to exit.
            if self._proc.stdin is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._proc.stdin.close()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        # Popen never closes its pipes on its own, even after the process
        # has exited, so do it here regardless of how we got here.
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()

    def __enter__(self) -> _BatchCatFile:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _committer_date_from_commit_object(commit_bytes: bytes) -> str:
    """Extract the committer date from a raw git commit object.

    Formats the result as git's ``%ci`` style (``YYYY-MM-DD HH:MM:SS +ZZZZ``)
    so it is a drop-in substitute for :func:`_commit_date`. This lets callers
    compute the committer date from a commit object they already hold in
    memory (typically from a :class:`_BatchCatFile` read) instead of spawning
    a separate ``git log -1`` subprocess.
    """
    for line in commit_bytes.split(b'\n'):
        if not line.startswith(b'committer '):
            continue
        # "committer <name> <<email>> <unix-ts> <+HHMM>"
        parts = line.rsplit(b' ', 2)
        if len(parts) != 3:
            break
        try:
            unix_ts = int(parts[1])
            tz_str = parts[2].decode()
            sign = 1 if tz_str[0] == '+' else -1
            hours = int(tz_str[1:3])
            minutes = int(tz_str[3:5])
        except (ValueError, IndexError) as exc:
            raise CursorError(f'Unparseable committer line: {line!r}') from exc
        offset = datetime.timedelta(hours=sign * hours, minutes=sign * minutes)
        dt = datetime.datetime.fromtimestamp(unix_ts, tz=datetime.timezone(offset))
        return dt.strftime('%Y-%m-%d %H:%M:%S %z')
    raise CursorError('No committer line found in commit object')


def _cursor_entry_from_batch(cf: _BatchCatFile, commit: str) -> dict[str, str]:
    """Build a cursor-state epoch entry for ``commit`` using an open batch.

    Fetches both the ``m`` blob (for msgid/subject) and the commit object
    (for committer date) via ``cf``. If the commit has no ``m`` blob (a
    noop/purge commit that happens to be the current HEAD of an epoch),
    msgid and subject fall back to the shared sentinels.
    """
    m_bytes = cf.read(f'{commit}:m')
    if m_bytes is not None:
        headers = _peek_cursor_headers(m_bytes)
    else:
        headers = {'subject': _NO_SUBJECT, 'msgid': _NO_MSGID}
    commit_obj = cf.read(f'{commit}^{{commit}}')
    if commit_obj is None:
        raise CursorError(f'Commit {commit} is not present in the repository')
    return {
        'commit': commit,
        'msgid': headers['msgid'],
        'subject': headers['subject'],
        'commit_date': _committer_date_from_commit_object(commit_obj),
    }


def _iter_messages_internal(
    v2path: str,
    since: dict[int, str],
    include_commit_date: bool,
) -> Iterator[tuple[int, str, bytes, str | None]]:
    """Shared iteration core for :func:`iter_messages` and :func:`iter_new_messages`.

    Opens one long-lived ``git cat-file --batch`` pipe per epoch. Yields a
    4-tuple ``(epoch, commit, m_bytes, commit_date)`` where ``commit_date``
    is the committer date in git's ``%ci`` format when
    ``include_commit_date=True`` and ``None`` otherwise. When it is ``None``
    we skip fetching the commit object entirely, saving one batch round trip
    per message on the pure-iter path.
    """
    for epoch, gitdir in get_all_epochs(v2path):
        if _top_commit(gitdir) is None:
            # Empty epoch -- nothing to yield, and any stored cursor position
            # for it is moot (the ref doesn't exist yet).
            continue
        since_commit = since.get(epoch)
        if since_commit is not None:
            if not _is_on_current_history(gitdir, since_commit):
                raise StaleCommitError(epoch, since_commit)
            revspec = f'{since_commit}..{PI_HEAD}'
        else:
            revspec = PI_HEAD

        retcode, out, err = git_run_command(gitdir, ['rev-list', '--reverse', revspec])
        if retcode != 0:
            raise CursorError(f'git rev-list failed in epoch {epoch}: {err.decode(errors="replace")}')

        commits = out.decode().splitlines()
        if not commits:
            continue
        # The generator close() machinery will run this `with` block's
        # __exit__ if the caller breaks out of iteration or abandons the
        # generator -- so the batch subprocess is always torn down cleanly.
        with _BatchCatFile(gitdir) as cf:
            for commit in commits:
                m_bytes = cf.read(f'{commit}:m')
                if m_bytes is None:
                    # Ambiguous: either a noop commit (no m blob in its tree)
                    # or a corrupt/missing commit object. Distinguish via a
                    # direct query, same bad-object detection korgalore
                    # learned the hard way (see korgalore pi_feed.py:371).
                    if cf.read(f'{commit}^{{commit}}') is None:
                        raise CursorError(f'Commit {commit} in epoch {epoch} is not a valid object')
                    continue
                commit_date: str | None = None
                if include_commit_date:
                    commit_obj = cf.read(f'{commit}^{{commit}}')
                    if commit_obj is None:
                        raise CursorError(f'Commit {commit} in epoch {epoch} vanished mid-read')
                    commit_date = _committer_date_from_commit_object(commit_obj)
                yield epoch, commit, m_bytes, commit_date


def iter_messages(
    v2path: str,
    since: dict[int, str] | None = None,
) -> Iterator[tuple[int, str, bytes]]:
    """Yield ``(epoch, commit, raw_bytes)`` for messages in chronological order.

    Walks every epoch in order, skipping no-op commits (purge/rm commits
    without an ``m`` blob). If ``since`` is given (maps epoch number to
    commit hash), only commits strictly after that hash are yielded in that
    epoch. Epochs not present in ``since`` are walked from their first commit.

    Args:
        v2path: Path to the v2 inbox directory.
        since: Optional ``{epoch: commit_hash}`` map marking a cursor
            position per epoch.

    Yields:
        Tuples of ``(epoch_number, commit_hash, raw_rfc822_bytes)``.

    Raises:
        StaleCommitError: If a ``since`` hash is not in the repo. Carries
            the epoch and hash so the cursor-aware layer can recover.
        CursorError: On unexpected git failures while reading blobs.
    """
    for epoch, commit, m_bytes, _ in _iter_messages_internal(v2path, since or {}, include_commit_date=False):
        yield epoch, commit, m_bytes


def _cursor_path(v2path: str, cursor_name: str) -> str:
    return os.path.join(v2path, f'ezpi-cursor.{cursor_name}.json')


def load_cursor(v2path: str, cursor_name: str) -> dict[str, Any] | None:
    """Load cursor state for ``cursor_name``, or None if no state exists.

    Args:
        v2path: Path to the v2 inbox directory.
        cursor_name: The cursor identifier.

    Returns:
        A dict with the cursor state, or None if the cursor has never been
        saved.

    Raises:
        CursorStateError: If the cursor file exists but cannot be parsed.
    """
    path = _cursor_path(v2path, cursor_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise CursorStateError(f'Could not read cursor {cursor_name}: {exc}') from exc
    if not isinstance(data, dict):
        raise CursorStateError(f'Cursor {cursor_name} is not a JSON object')
    return data


def _write_cursor(v2path: str, cursor_name: str, data: dict[str, Any]) -> None:
    """Atomically write cursor state to disk, holding an exclusive lock.

    The lock is per-cursor-file (not per-inbox) so writers for different
    cursors never block each other.
    """
    path = _cursor_path(v2path, cursor_name)
    os.makedirs(v2path, exist_ok=True)
    lock_path = path + '.lock'
    with _exclusive_lock(lock_path):
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)


def save_cursor(
    v2path: str,
    cursor_name: str,
    epoch: int,
    commit: str,
    msg_bytes: bytes,
    commit_date: str | None = None,
) -> None:
    """Persist a cursor position after processing a message.

    Stores msgid+subject (extracted from ``msg_bytes``) and the committer
    date of ``commit``; all three are required for rebase recovery.

    Args:
        v2path: Path to the v2 inbox directory.
        cursor_name: The cursor identifier.
        epoch: The epoch containing ``commit``.
        commit: The commit hash just processed.
        msg_bytes: The raw message bytes yielded for that commit.
        commit_date: Optional pre-computed committer date in git's ``%ci``
            format (``YYYY-MM-DD HH:MM:SS +ZZZZ``). If omitted, a
            ``git log -1 --format=%ci`` subprocess is spawned. Callers
            iterating with :func:`iter_new_messages` and ``auto_advance``
            receive this value for free and pass it through to avoid the
            extra fork.
    """
    gitdir = os.path.join(v2path, 'git', f'{epoch}.git')
    headers = _peek_cursor_headers(msg_bytes)
    if commit_date is None:
        commit_date = _commit_date(gitdir, commit)

    existing = load_cursor(v2path, cursor_name) or {}
    raw_epochs = existing.get('epochs', {})
    epochs: dict[str, Any] = dict(raw_epochs) if isinstance(raw_epochs, dict) else {}
    epochs[str(epoch)] = {
        'commit': commit,
        'msgid': headers['msgid'],
        'subject': headers['subject'],
        'commit_date': commit_date,
    }
    data = {
        'cursor': cursor_name,
        'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'epochs': epochs,
    }
    _write_cursor(v2path, cursor_name, data)


def reset_cursor(v2path: str, cursor_name: str) -> None:
    """Delete the cursor state file. No-op if it doesn't exist."""
    path = _cursor_path(v2path, cursor_name)
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def _save_cursor_raw(
    v2path: str,
    cursor_name: str,
    per_epoch: dict[int, dict[str, str]],
) -> None:
    """Write cursor state from a fully-assembled per-epoch dict.

    Used by first-use initialization and recovery, where we don't have a
    single ``(commit, msg_bytes)`` pair to save but a full snapshot.
    """
    data = {
        'cursor': cursor_name,
        'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'epochs': {str(k): v for k, v in per_epoch.items()},
    }
    _write_cursor(v2path, cursor_name, data)


def recover_cursor(v2path: str, cursor_name: str, epoch: int) -> str:
    """Re-locate a cursor's stored commit after history was rewritten.

    Uses the stored ``commit_date`` to anchor a ``--since-as-filter`` search,
    then matches on ``(subject, msgid)``. Mirrors korgalore's algorithm
    (pi_feed.py:248-303) including its fallback choices:

    - If ``rev-list`` itself errors, fast-forward to the current top commit.
    - If ``rev-list`` yields no candidates, save state at the top commit.
    - If no candidate matches the stored headers, fall back to the **first**
      candidate after the date (logged as a warning). Trades possible-skip
      for small re-delivery risk.

    Args:
        v2path: Path to the v2 inbox directory.
        cursor_name: The cursor identifier.
        epoch: The epoch whose stored position needs recovery.

    Returns:
        The new commit hash, also written back into the cursor file.

    Raises:
        CursorStateError: If there is no stored state for ``epoch`` or
            required fields are missing.
    """
    state = load_cursor(v2path, cursor_name)
    if state is None:
        raise CursorStateError(f'Cursor {cursor_name} has no saved state')
    epochs_state = state.get('epochs', {})
    if not isinstance(epochs_state, dict) or str(epoch) not in epochs_state:
        raise CursorStateError(f'No state for epoch {epoch} in cursor {cursor_name}')
    info = epochs_state[str(epoch)]
    stored_msgid = info.get('msgid')
    stored_subject = info.get('subject')
    commit_date = info.get('commit_date')
    if not commit_date:
        raise CursorStateError(f'Cursor {cursor_name} has no commit_date for epoch {epoch} (cannot recover)')

    gitdir = os.path.join(v2path, 'git', f'{epoch}.git')

    # One batch pipe for the whole recovery: candidate-walk reads, the
    # fast-forward fallback read, and the final chosen-commit read all go
    # through a single persistent git process.
    with _BatchCatFile(gitdir) as cf:

        def _fast_forward(reason: str) -> str:
            top = _top_commit(gitdir)
            if top is None:
                # Epoch is empty -- drop any stale state we had for it.
                fresh = {int(k): v for k, v in epochs_state.items() if k != str(epoch)}
                _save_cursor_raw(v2path, cursor_name, fresh)
                raise CursorStateError(
                    f'Cannot recover cursor {cursor_name} for epoch {epoch}: {reason} and epoch is empty'
                )
            logger.warning('Cursor %s epoch %d: %s, fast-forwarding to %s', cursor_name, epoch, reason, top)
            new_epochs_ff = {int(k): dict(v) for k, v in epochs_state.items()}
            new_epochs_ff[epoch] = _cursor_entry_from_batch(cf, top)
            _save_cursor_raw(v2path, cursor_name, new_epochs_ff)
            return top

        retcode, out, _err = git_run_command(
            gitdir, ['rev-list', '--reverse', '--since-as-filter', commit_date, PI_HEAD]
        )
        if retcode != 0:
            return _fast_forward('rev-list failed during recovery')

        candidates = out.decode().splitlines()
        if not candidates:
            return _fast_forward('no commits at or after stored commit_date')

        matched: str | None = None
        for candidate in candidates:
            m_bytes = cf.read(f'{candidate}:m')
            if m_bytes is None:
                continue  # noop commit in the candidate range
            headers = _peek_cursor_headers(m_bytes)
            if headers['subject'] == stored_subject and headers['msgid'] == stored_msgid:
                matched = candidate
                break

        if matched is not None:
            chosen = matched
            logger.debug('Cursor %s epoch %d: recovered exact match at %s', cursor_name, epoch, chosen)
        else:
            chosen = candidates[0]
            logger.warning(
                'Cursor %s epoch %d: no exact match after rebase, resuming at first candidate %s '
                '(may skip or re-deliver)',
                cursor_name,
                epoch,
                chosen,
            )

        new_epochs = {int(k): dict(v) for k, v in epochs_state.items()}
        new_epochs[epoch] = _cursor_entry_from_batch(cf, chosen)
        _save_cursor_raw(v2path, cursor_name, new_epochs)
        return chosen


def iter_new_messages(
    v2path: str,
    cursor_name: str,
    auto_advance: bool = False,
    start: str = 'head',
) -> Iterator[tuple[int, str, bytes]]:
    """Yield messages the named cursor has not seen yet.

    On first use (no cursor file exists), behavior depends on ``start``:

    - ``'head'`` (default): advance cursor to the current HEAD of every
      epoch and yield nothing. The caller will only see messages added
      after this first call.
    - ``'beginning'``: walk the entire history from the oldest commit.

    New epochs appearing after the cursor was saved are walked from their
    first commit automatically.

    If a stored commit hash has been rewritten away (rebase), this function
    transparently invokes :func:`recover_cursor` and resumes. Recovery always
    succeeds by falling through to the top commit in the worst case.

    Args:
        v2path: Path to the v2 inbox directory.
        cursor_name: The cursor identifier.
        auto_advance: If True, persist cursor state automatically after each
            yielded message. If False, caller must call :func:`save_cursor`.
        start: First-use policy, ``'head'`` or ``'beginning'``.

    Yields:
        Tuples of ``(epoch_number, commit_hash, raw_rfc822_bytes)``.

    Raises:
        ValueError: If ``start`` is neither ``'head'`` nor ``'beginning'``.
    """
    if start not in ('head', 'beginning'):
        raise ValueError(f"start must be 'head' or 'beginning', got {start!r}")

    state = load_cursor(v2path, cursor_name)
    if state is None:
        # First use: set cursor based on `start` policy.
        if start == 'head':
            _initialize_cursor_at_head(v2path, cursor_name)
            return
        # 'beginning': fall through with no `since` dict -> walk everything.
        since: dict[int, str] = {}
    else:
        epochs_state = state.get('epochs', {})
        since = {}
        if isinstance(epochs_state, dict):
            for k, v in epochs_state.items():
                try:
                    since[int(k)] = v['commit']
                except (KeyError, TypeError, ValueError):
                    continue

    while True:
        try:
            for epoch, commit, msg_bytes, commit_date in _iter_messages_internal(
                v2path, since, include_commit_date=auto_advance
            ):
                yield epoch, commit, msg_bytes
                if auto_advance:
                    # commit_date came from the per-epoch batch pipe, so this
                    # save_cursor call does not need to spawn its own git
                    # subprocess.
                    save_cursor(v2path, cursor_name, epoch, commit, msg_bytes, commit_date=commit_date)
                    since[epoch] = commit
            return
        except StaleCommitError as exc:
            # A stored hash is gone; recover this epoch and restart iteration.
            # Recovery updates state on disk; refresh our local `since` map
            # so already-yielded epochs keep their position.
            recovered = recover_cursor(v2path, cursor_name, exc.epoch)
            since[exc.epoch] = recovered
            continue


def _initialize_cursor_at_head(v2path: str, cursor_name: str) -> None:
    """Seed a brand-new cursor at the HEAD of every existing epoch."""
    per_epoch: dict[int, dict[str, str]] = {}
    for epoch, gitdir in get_all_epochs(v2path):
        top = _top_commit(gitdir)
        if top is None:
            continue
        with _BatchCatFile(gitdir) as cf:
            per_epoch[epoch] = _cursor_entry_from_batch(cf, top)
    _save_cursor_raw(v2path, cursor_name, per_epoch)


def command() -> None:
    """CLI entry point for the ezpi command.

    This function is registered as the 'ezpi' console script entry point.
    It reads message content from stdin and adds it to a git repository.

    Usage examples::

        # Add RFC822 message to bare repo
        ezpi -r /path/to/repo.git --rfc822 < message.eml

        # Add plaintext with headers to bare repo
        ezpi -r /path/to/repo.git -f "Name <email>" -s "Subject" < content.txt

        # Add to v2 inbox (auto-creates if needed)
        ezpi --v2-path /path/to/inbox --rfc822 < message.eml

        # Add to v2 with annual epoch rotation
        ezpi --v2-path /path/to/inbox --auto-epoch annual --rfc822 < message.eml
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    repo_group = parser.add_mutually_exclusive_group(required=True)
    repo_group.add_argument(
        '-r', '--repo', default=None, help='Bare git repository where to write the commit (must exist)'
    )
    repo_group.add_argument('--v2-path', dest='v2path', default=None, help='Path to public-inbox v2 format inbox')
    parser.add_argument(
        '--auto-epoch',
        dest='auto_epoch',
        choices=['size', 'annual'],
        default=None,
        help='Epoch rotation mode for v2 format: size (1GB, default) or annual (Jan 1)',
    )
    parser.add_argument(
        '-d',
        '--dry-run',
        dest='dryrun',
        action='store_true',
        default=False,
        help='Do not write the commit, just show the commit that would be written.',
    )
    parser.add_argument('-q', '--quiet', action='store_true', default=False, help='Only output errors to the stdout')
    parser.add_argument('-v', '--verbose', action='store_true', default=False, help='Show debugging output')
    parser.add_argument('--rfc822', action='store_true', default=False, help='Treat stdin as an rfc822 message')
    parser.add_argument(
        '-f', '--from', dest='hdr_from', default=None, help='From header for the message, if not using --rfc822'
    )
    parser.add_argument(
        '-s', '--subject', dest='hdr_subj', default=None, help='Subject header for the message, if not using --rfc822'
    )
    parser.add_argument(
        '-p',
        '--run-post-commit-hook',
        action='store_true',
        dest='runhook',
        default=False,
        help='Run hooks/post-commit in the repo or v2 inbox after a successful commit (if present)',
    )
    parser.add_argument('--domain', default=None, help='Domain to use when creating message-ids')

    _args = parser.parse_args()

    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if _args.quiet:
        ch.setLevel(logging.CRITICAL)
    elif _args.verbose:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)

    # Validate --auto-epoch is only used with --v2-path
    if _args.auto_epoch and not _args.v2path:
        logger.critical('ERROR: --auto-epoch can only be used with --v2-path')
        sys.exit(1)

    # Default auto_epoch to 'size' for v2 paths
    if _args.v2path and not _args.auto_epoch:
        _args.auto_epoch = 'size'

    if sys.stdin.isatty():
        logger.critical('ERROR: Provide the message contents on stdin')
        sys.exit(1)

    if _args.rfc822:
        if _args.hdr_from or _args.hdr_subj:
            logger.critical('ERROR: Either provide --rfc822 or -s/-f, not both')
            sys.exit(1)
        try:
            content = sys.stdin.buffer.read()
            if _args.v2path:
                add_rfc822_v2(_args.v2path, content, domain=_args.domain, auto_epoch=_args.auto_epoch)
            else:
                add_rfc822(_args.repo, content, domain=_args.domain)
        except (ValueError, RuntimeError, FileExistsError) as ex:
            logger.critical('ERROR: %s', ex)
            sys.exit(1)
        if _args.runhook:
            run_hook(_args.v2path or _args.repo)
        return

    if not _args.hdr_from or not _args.hdr_subj:
        logger.critical('ERROR: Must provide -s and -f parameters for plaintext content')
        sys.exit(1)

    parts = parseaddr(_args.hdr_from)
    a_name = parts[0]
    a_email = parts[1]
    if not a_name:
        a_name = DEFAULT_NAME

    try:
        content_str = sys.stdin.read()
        if _args.v2path:
            # Build RFC822 message from plaintext for v2
            m = f'From: {a_name} <{a_email}>\nSubject: {_args.hdr_subj}\n\n' + content_str
            add_rfc822_v2(_args.v2path, m.encode(), domain=_args.domain, auto_epoch=_args.auto_epoch)
        else:
            add_plaintext(_args.repo, content_str, _args.hdr_subj, a_name, a_email, domain=_args.domain)
    except (ValueError, RuntimeError, FileExistsError) as ex:
        logger.critical('ERROR: %s', ex)
        sys.exit(1)

    if _args.runhook:
        run_hook(_args.v2path or _args.repo)


if __name__ == '__main__':
    command()
