#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2020 by The Linux Foundation
# SPDX-License-Identifier: MIT-0
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import os
import sys
import ezpi

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from typing import Optional


def get_config_from_git(regexp: str, defaults: Optional[dict] = None) -> dict:
    gitconfig = defaults if defaults else dict()

    args = ['config', '-z', '--get-regexp', regexp]
    ee, out, err = ezpi.git_run_command('', args)
    if ee > 0 or not len(out):
        return gitconfig

    for line in out.decode().split('\x00'):
        if not line:
            continue
        key, value = line.split('\n', 1)
        try:
            chunks = key.split('.')
            cfgkey = chunks[-1]
            gitconfig[cfgkey.lower()] = value
        except ValueError:
            pass

    return gitconfig


def run_hook(feedrepo: str, fromhdr: str, domain: str):
    # Look if we have a GL_USER and GL_REPO in the env
    user = os.getenv('GL_USER')
    if not user:
        user = os.getenv('USER')
    repo = os.getenv('GL_REPO')
    if not repo:
        repo = os.getcwd()
    ll = list()
    ll.append('---')
    ll.append('service: git')
    ll.append('operation: receive')
    ll.append(f'repo: {repo}')
    ll.append(f'user: {user}')
    ll.append('changes:')
    revdumps = list()
    # Do we have a push cert?
    cert = os.getenv('GIT_PUSH_CERT')
    if cert:
        ll.append('git_push_cert:')
        for key, val in os.environ.items():
            if key.find('GIT_PUSH_CERT_') == 0:
                ll.append(f'  {key.lower()}: {val}')

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        oldrev, newrev, ref = line.strip().split()
        ll.append(f'  - oldrev: {oldrev}')
        ll.append(f'    newrev: {newrev}')
        ll.append(f'    ref: {ref}')

        args = ['rev-list', '--topo-order', '--no-merges', '--reverse', '--pretty=oneline', newrev]
        if set(oldrev) != {0}:
            args += [f'^{oldrev}']

        ee, out, err = ezpi.git_run_command('', args)
        if ee > 0 or not len(out):
            continue
        revdumps.append((oldrev, newrev, out.decode()))
    ll.append('')

    msg = MIMEMultipart()
    msg['From'] = fromhdr
    msg['Subject'] = f'post-receive: {repo}'
    msg.attach(MIMEText('\n'.join(ll), 'plain'))
    for oldrev, newrev, revdump in revdumps:
        filename = f'revlist-{oldrev[:12]}-{newrev[:12]}.txt'
        att = MIMEText(revdump, 'plain')
        att.add_header('Content-Disposition', f'attachment; filename={filename}')
        msg.attach(att)
    if cert:
        args = ['cat-file', 'blob', cert]
        ee, out, err = ezpi.git_run_command('', args)
        if ee == 0 and out:
            att = MIMEText(out.decode(), 'plain')
            att.add_header('Content-Disposition', 'attachment; filename=git-push-certificate.txt')
            msg.attach(att)

    try:
        ezpi.add_rfc822(feedrepo, msg, domain)
        sys.stderr.write('Recorded in the activity feed\n')
    except RuntimeError:
        # Could not add it to the feed, complain
        sys.stderr.write('FAILED writing to the activity feed!\n')


if __name__ == '__main__':
    if sys.stdin.isatty():
        # Nothing passed via stdin, so nothing to add to the feed
        sys.exit(0)
    config = get_config_from_git(r'activityfeed\..*')
    _feedrepo = config.get('repo')
    if not config.get('repo'):
        # The audit repo is not defined in gitconfig, so nothing for us to do.
        sys.exit(0)
    _fromhdr = config.get('from')
    _domain = config.get('domain')
    if not _domain:
        _domain = 'localhost'

    if not _fromhdr:
        _fromhdr = f'Post-Receive Hook <post-receive@{_domain}>'

    run_hook(_feedrepo, _fromhdr, _domain)
