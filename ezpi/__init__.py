#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2020 by The Linux Foundation
# SPDX-License-Identifier: MIT-0
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import os
import email
import email.policy

import subprocess
import logging

from email.utils import formatdate, getaddresses, make_msgid

from typing import Optional, Tuple, Dict

from email import charset
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
__VERSION__ = '0.2.2'


def git_run_command(gitdir: str, args: list, stdin: Optional[bytes] = None,
                    env: Optional[Dict] = None) -> Tuple[int, bytes, bytes]:
    if not env:
        env = dict()
    if gitdir:
        env['GIT_DIR'] = gitdir
    args = ['git', '--no-pager'] + args
    logger.debug('Running %s', ' '.join(args))
    pp = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    (output, error) = pp.communicate(input=stdin)

    return pp.returncode, output, error


def check_valid_repo(repo: str) -> None:
    # check that it exists and has 'objects' and 'refs'
    if not os.path.isdir(repo):
        raise FileNotFoundError(f'Path does not exist: {repo}')
    musts = ['objects', 'refs']
    for must in musts:
        if not os.path.exists(os.path.join(repo, must)):
            raise FileNotFoundError(f'Path is not a valid bare git repository: {repo}')


def git_write_commit(repo: str, env: dict, c_msg: str, body: bytes, dest: str = 'm') -> None:
    check_valid_repo(repo)
    # We use git porcelain commands here. We could use pygit2, but this would pull in a fairly
    # large external lib for what is effectively 4 commands that we need to run.
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
        args = ['commit-tree', '-m', c_msg, tree]
    else:
        args = ['commit-tree', '-p', PI_HEAD, '-m', c_msg, tree]
    # Commit the tree
    ee, out, err = git_run_command(repo, args, env=env)
    if ee > 0:
        raise RuntimeError(f'Could not commit-tree in {repo}: {err.decode()}')
    # Finally, update the ref
    commit = out.decode().strip()
    ee, out, err = git_run_command(repo, ['update-ref', PI_HEAD, commit])
    if ee > 0:
        raise RuntimeError(f'Could not update-ref in {repo}: {err.decode()}')


def add_plaintext(repo: str, content: str, subject: str, authorname: str, authoremail: str,
                  domain: Optional[str] = None) -> None:
    # We create a barebones rfc822 message out of any plaintext content
    m = f'From: {authorname} <{authoremail}>\nSubject: {subject}\n\n' + content
    add_rfc822(repo, m.encode(), domain=domain)


def add_rfc822(repo: str, content, domain: Optional[str] = None,
               env: Optional[Dict] = None) -> None:
    if isinstance(content, bytes):
        msg = email.message_from_bytes(content)
    else:
        msg = content

    # Make sure we have at least a From and a subject
    h_subject = msg.get('Subject')
    if not h_subject:
        raise ValueError('Message must contain a valid Subject header')

    h_from = msg.get('From')
    if not h_from:
        raise ValueError('Message must contain a valid From header')
    parts = getaddresses([h_from])
    a_name = parts[0][0]
    a_email = parts[0][1]
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

    if msg.get_content_maintype() == 'text' and not msg.get_content_charset():
        msg.set_charset('utf-8')

    # Sneak ourselves in as User-Agent, if not set
    if not msg.get('User-Agent'):
        msg.add_header('User-Agent', f'EZPI/{__VERSION__}')

    # we don't do as_bytes because we don't need to sent via smtp,
    # so some of the escapes python is doing are entirely unnecessary
    body = msg.as_string(policy=EMLPOLICY).encode()

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
    hookpath = os.path.join(repo, 'hooks', 'post-commit')
    if os.access(hookpath, os.X_OK):
        logger.debug('Running %s', hookpath)
        curdir = os.getcwd()
        os.chdir(repo)
        pp = subprocess.Popen(['hooks/post-commit'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (output, error) = pp.communicate()
        if pp.returncode > 0:
            logger.critical('Running post-update hook failed')
            logger.critical('STDERR follows')
            logger.critical(error.decode())
        os.chdir(curdir)


def command() -> None:
    import sys
    import argparse
    # noinspection PyTypeChecker
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-r', '--repo', default=None,
                        help='Bare git repository where to write the commit (must exist)')
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
    if sys.stdin.isatty():
        logger.critical('ERROR: Provide the message contents on stdin')
        sys.exit(1)

    if _args.rfc822:
        if _args.hdr_from or _args.hdr_subj:
            logger.critical('ERROR: Either provide --rfc822 or -s/-f, not both')
            sys.exit(1)
        try:
            content = sys.stdin.buffer.read()
            add_rfc822(_args.repo, content, domain=_args.domain)
        except (ValueError, RuntimeError) as ex:
            logger.critical('ERROR: %s', ex)
            sys.exit(1)
        if _args.runhook:
            run_hook(_args.repo)
        return

    if not _args.hdr_from or not _args.hdr_subj:
        logger.critical('ERROR: Must provide -s and -f parameters for plaintext content')
        sys.exit(1)

    parts = getaddresses([_args.hdr_from])
    a_name = parts[0][0]
    a_email = parts[0][1]
    if not a_name:
        a_name = DEFAULT_NAME

    try:
        content = sys.stdin.read()
        add_plaintext(_args.repo, content, _args.hdr_subj, a_name, a_email, domain=_args.domain)
    except (ValueError, RuntimeError) as ex:
        logger.critical('ERROR: %s', ex)
        sys.exit(1)

    if _args.runhook:
        run_hook(_args.repo)


if __name__ == '__main__':
    command()
