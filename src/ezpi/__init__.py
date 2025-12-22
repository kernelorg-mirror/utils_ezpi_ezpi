#!/usr/bin/env python3
#
# Copyright (C) 2020 by The Linux Foundation
# SPDX-License-Identifier: MIT-0
#
"""EZPI - A library for writing RFC2822 messages to public-inbox repositories.

This module provides functions to add email messages to public-inbox v2
format repositories. Messages can be provided as raw bytes or
email.message.Message objects. The v2 inbox structure is created
automatically and epochs are rotated based on size or annual boundaries.

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
"""
from __future__ import annotations

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import email
import email.header
import email.message
import email.policy
import logging
import os
import re
import subprocess
from collections.abc import Sequence
from email import charset
from email.utils import formatdate, make_msgid, parseaddr
from fcntl import LOCK_EX, LOCK_UN, lockf
from typing import IO

# Optional pygit2 support
try:
    import pygit2
    HAS_PYGIT2 = True
except ImportError:
    pygit2 = None  # type: ignore[assignment]
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
__VERSION__ = '0.5.0'


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

    # Create signatures
    author = pygit2.Signature(
        env.get('GIT_AUTHOR_NAME', DEFAULT_NAME),
        env.get('GIT_AUTHOR_EMAIL', DEFAULT_ADDR),
    )
    committer = pygit2.Signature(
        env.get('GIT_COMMITTER_NAME', DEFAULT_NAME),
        env.get('GIT_COMMITTER_EMAIL', DEFAULT_ADDR),
    )

    # Get parent commit if exists
    parents: Sequence[str] = []
    try:
        ref = repository.references.get(PI_HEAD)  # type: ignore[attr-defined]
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
        parents,  # type: ignore[arg-type]
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
        ref = repository.references.get(PI_HEAD)  # type: ignore[attr-defined]
        if ref is None:
            return None
        commit = repository.get(ref.target)  # type: ignore[attr-defined]
        if commit is None:
            return None
        return int(commit.commit_time)
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
    # Lock the repository
    lockfh: IO[str] | None = None
    try:
        # The lock shouldn't be held open for very long, so try without a timeout
        lockfh = open(os.path.join(repo, 'ezpi.lock'), 'w')  # noqa: SIM115
        lockf(lockfh, LOCK_EX)
    except OSError as exc:
        raise RuntimeError('Could not obtain an exclusive lock') from exc

    try:
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
    finally:
        lockf(lockfh, LOCK_UN)


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

    Args:
        repo: Path to the bare git repository.
    """
    hookpath = os.path.join(repo, 'hooks', 'post-commit')
    if os.access(hookpath, os.X_OK):
        logger.debug('Running %s', hookpath)
        curdir = os.getcwd()
        os.chdir(repo)
        pp = subprocess.Popen(['hooks/post-commit'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (output, error) = pp.communicate()
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
        ee, out, err = git_run_command('', ['init', '--bare', epoch_path])
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
        ee, out, err = git_run_command('', ['init', '--bare', all_git])
        if ee > 0:
            raise RuntimeError(f'Could not init all.git: {err.decode()}')

    # Create first epoch (this also sets up alternates)
    epoch_path = init_epoch(v2path, 0)

    logger.info('Initialized v2 inbox at %s', v2path)
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
            ee, out, err = git_run_command(epoch_path, ['log', '-1', '--format=%ct', PI_HEAD])
            if ee > 0:
                # No commits yet, no rotation needed
                return False
            try:
                timestamp = int(out.decode().strip())
            except (ValueError, OSError):
                return False
        try:
            commit_year = datetime.datetime.fromtimestamp(timestamp).year
            current_year = datetime.datetime.now().year
            return commit_year < current_year
        except (ValueError, OSError):
            return False

    return False


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
        RuntimeError: If any git operation fails or lock cannot be acquired.

    Example::

        ezpi.add_rfc822_v2('/path/to/inbox', email_bytes)
        ezpi.add_rfc822_v2('/path/to/inbox', msg, auto_epoch='annual')
    """
    # Lock the inbox
    lockfh: IO[str] | None = None
    try:
        if os.path.exists(v2path):
            lockfh = open(os.path.join(v2path, 'inbox.lock'), 'w')  # noqa: SIM115
            lockf(lockfh, LOCK_EX)
    except OSError as exc:
        raise RuntimeError('Could not obtain v2 inbox lock') from exc

    try:
        # Initialize if needed
        if not os.path.exists(v2path):
            epoch_path = init_v2_inbox(v2path)
            # Re-acquire lock after init
            lockfh = open(os.path.join(v2path, 'inbox.lock'), 'w')  # noqa: SIM115
            lockf(lockfh, LOCK_EX)
        else:
            epoch_num, epoch_path = get_latest_epoch(v2path)

            # Check if rotation is needed
            if should_rotate_epoch(epoch_path, auto_epoch):
                new_epoch = epoch_num + 1
                epoch_path = init_epoch(v2path, new_epoch)
                logger.info('Rotated to new epoch %d', new_epoch)

        # Write to the epoch
        add_rfc822(epoch_path, content, domain=domain, env=env)

    finally:
        if lockfh:
            lockf(lockfh, LOCK_UN)
            lockfh.close()


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
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    repo_group = parser.add_mutually_exclusive_group(required=True)
    repo_group.add_argument('-r', '--repo', default=None,
                            help='Bare git repository where to write the commit (must exist)')
    repo_group.add_argument('--v2-path', dest='v2path', default=None,
                            help='Path to public-inbox v2 format inbox')
    parser.add_argument('--auto-epoch', dest='auto_epoch', choices=['size', 'annual'], default=None,
                        help='Epoch rotation mode for v2 format: size (1GB, default) or annual (Jan 1)')
    parser.add_argument('-d', '--dry-run', dest='dryrun', action='store_true', default=False,
                        help='Do not write the commit, just show the commit that would be written.')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors to the stdout')
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Show debugging output')
    parser.add_argument('--rfc822', action='store_true', default=False,
                        help='Treat stdin as an rfc822 message')
    parser.add_argument('-f', '--from', dest='hdr_from', default=None,
                        help='From header for the message, if not using --rfc822')
    parser.add_argument('-s', '--subject', dest='hdr_subj', default=None,
                        help='Subject header for the message, if not using --rfc822')
    parser.add_argument('-p', '--run-post-commit-hook', action='store_true', dest='runhook', default=False,
                        help='Run hooks/post-commit after a successful commit (if present)')
    parser.add_argument('--domain', default=None,
                        help='Domain to use when creating message-ids')

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
        if _args.runhook and _args.repo:
            run_hook(_args.repo)
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

    if _args.runhook and _args.repo:
        run_hook(_args.repo)


if __name__ == '__main__':
    command()
