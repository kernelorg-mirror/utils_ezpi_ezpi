"""Tests for ezpi.add_rfc822 function."""

from __future__ import annotations

import email
import email.message
import subprocess
from pathlib import Path

import pytest
from conftest import get_commit_body, get_commit_message

import ezpi


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
        assert get_commit_message(bare_repo) == 'Test Subject'

        # Verify the message body was stored
        body = get_commit_body(bare_repo)
        assert b'This is a test message body.' in body
        assert b'From: Test User <test@example.com>' in body


class TestAddRfc822BytesPreserved:
    """Bytes input must be archived exactly as received.

    Archives keep DKIM-Signature headers so messages can be verified
    later, and any change to the body or signed headers breaks them.
    Messages written by ezpi should also match what public-inbox-mda
    stores for the same input.
    """

    HEADERS = (
        b'From: Carlos <carlos@example.org>\n'
        b'Subject: Hola\n'
        b'Message-Id: <preserve@example.org>\n'
        b'Date: Mon, 14 Aug 2023 18:01:39 +0200\n'
    )

    @pytest.mark.parametrize(
        'raw',
        [
            pytest.param(
                HEADERS + b'MIME-Version: 1.0\n'
                b'Content-Type: text/plain; charset=iso-8859-1\n'
                b'Content-Transfer-Encoding: 8bit\n'
                b'\n'
                b'Carlos L\xf3pez\n',
                id='8bit-latin1',
            ),
            pytest.param(
                HEADERS + b'Content-Type: text/plain\n\nplain ascii\n',
                id='no-charset',
            ),
            pytest.param(
                HEADERS + b'\nno content-type at all\n',
                id='no-content-type',
            ),
            pytest.param(
                HEADERS.replace(b'\n', b'\r\n') + b'\r\ncrlf line endings\r\n',
                id='crlf',
            ),
        ],
    )
    def test_stored_byte_identical(self, bare_repo: Path, raw: bytes) -> None:
        """Complete messages are stored without any changes."""
        ezpi.add_rfc822(str(bare_repo), raw)

        assert get_commit_body(bare_repo) == raw

    @pytest.mark.parametrize(
        ('drop', 'added'),
        [
            (b'Date: Mon, 14 Aug 2023 18:01:39 +0200\n', b'Date: '),
            (b'Message-Id: <preserve@example.org>\n', b'Message-Id: <'),
        ],
    )
    @pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
    def test_missing_header_is_prepended(self, bare_repo: Path, drop: bytes, added: bytes, eol: bytes) -> None:
        """Missing Date or Message-Id is added without touching the rest.

        public-inbox needs both headers, but adding them must not
        re-serialize the message. The new header uses the message's own
        line ending, so the result does not mix LF and CRLF.
        """
        raw = (self.HEADERS.replace(drop, b'') + b'\nCarlos L\xf3pez\n').replace(b'\n', eol)

        ezpi.add_rfc822(str(bare_repo), raw, domain='example.org')

        body = get_commit_body(bare_repo)
        first, rest = body.split(eol, 1)
        assert first.startswith(added)
        assert rest == raw

    def test_mbox_from_line_is_dropped(self, bare_repo: Path) -> None:
        """The mbox envelope line is not part of the message.

        Callers often pass messages split from an mbox file. public-inbox
        does not store the envelope line either.
        """
        raw = self.HEADERS + b'\nbody\n'

        ezpi.add_rfc822(str(bare_repo), b'From carlos@example.org Mon Aug 14 18:01:39 2023\n' + raw)

        assert get_commit_body(bare_repo) == raw


class TestAddRfc822WithMessage:
    """Tests for add_rfc822 with Message object input."""

    def test_basic_message_object(self, bare_repo: Path) -> None:
        """Test adding an email.message.Message object."""
        msg = email.message.EmailMessage()
        msg['From'] = 'Test User <test@example.com>'
        msg['Subject'] = 'Message Object Test'
        msg['Message-Id'] = '<msgobj@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        msg.set_content('This is a message from a Message object.')

        ezpi.add_rfc822(str(bare_repo), msg)

        # Verify commit was created with correct subject
        assert get_commit_message(bare_repo) == 'Message Object Test'

        # Verify the message body was stored
        body = get_commit_body(bare_repo)
        assert b'This is a message from a Message object.' in body

    def test_message_object_with_custom_env(self, bare_repo: Path) -> None:
        """Test adding a Message with custom git environment."""
        msg = email.message.EmailMessage()
        msg['From'] = 'Author <author@example.com>'
        msg['Subject'] = 'Custom Env Test'
        msg['Message-Id'] = '<customenv@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        msg.set_content('Testing custom environment.')

        custom_env = {
            'GIT_COMMITTER_NAME': 'Custom Committer',
            'GIT_COMMITTER_EMAIL': 'committer@example.com',
            'GIT_COMMITTER_DATE': 'Mon, 01 Jan 2024 12:00:00 +0000',
        }

        ezpi.add_rfc822(str(bare_repo), msg, env=custom_env)

        # Verify commit was created
        assert get_commit_message(bare_repo) == 'Custom Env Test'

        # Verify committer info
        result = subprocess.run(
            ['git', '--git-dir', str(bare_repo), 'log', '-1', '--format=%cn <%ce>'],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == 'Custom Committer <committer@example.com>'


class TestAddRfc822Unicode:
    """Tests for add_rfc822 Unicode handling."""

    def test_message_with_non_breaking_hyphen(self, bare_repo: Path) -> None:
        """Test that messages with non-breaking hyphen (\u2011) are handled."""
        # This reproduces the error from the traceback:
        # UnicodeEncodeError: 'ascii' codec can't encode character '\u2011'
        msg = email.message.EmailMessage()
        msg['From'] = 'Test User <test@example.com>'
        msg['Subject'] = 'Unicode Test'
        msg['Message-Id'] = '<unicode-test@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        # Use non-breaking hyphen (U+2011) which caused the original error
        msg.set_content('This has a non\u2011breaking hyphen.')

        ezpi.add_rfc822(str(bare_repo), msg)

        body = get_commit_body(bare_repo)
        assert b'non' in body
        assert b'breaking hyphen' in body

    @pytest.mark.parametrize('content_type', [None, 'text/plain; charset=us-ascii'])
    def test_message_unicode_payload_gets_utf8_charset(self, bare_repo: Path, content_type: str | None) -> None:
        """Message objects with Unicode payloads are re-encoded as UTF-8.

        Callers such as groupsio-webhook.py build messages with Unicode
        text and either no charset or a wrong one. Without the fix-up,
        as_bytes() raises UnicodeEncodeError or writes a wrong charset.
        """
        msg = email.message.Message()
        msg['From'] = 'Test User <test@example.com>'
        msg['Subject'] = 'Unicode Payload Test'
        msg['Message-Id'] = '<unicode-payload@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        if content_type:
            msg['Content-Type'] = content_type
        msg.set_payload('Content with non\u2011breaking hyphen and \u00e9 accent.')

        ezpi.add_rfc822(str(bare_repo), msg)

        body = get_commit_body(bare_repo)
        assert b'charset="utf-8"' in body
        assert 'non\u2011breaking hyphen and \u00e9 accent'.encode() in body

    def test_message_with_various_unicode(self, bare_repo: Path) -> None:
        """Test that messages with various Unicode characters are handled."""
        msg = email.message.EmailMessage()
        msg['From'] = 'Test User <test@example.com>'
        msg['Subject'] = 'Unicode Test 2'
        msg['Message-Id'] = '<unicode-test2@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        # Various Unicode characters: emoji, CJK, accents
        msg.set_content('Hello: \u00e9\u00e8\u00ea \u4e2d\u6587 \U0001f600')

        ezpi.add_rfc822(str(bare_repo), msg)

        body = get_commit_body(bare_repo)
        assert b'Hello' in body

    def test_bytes_with_unicode(self, bare_repo: Path) -> None:
        """Test that bytes input with UTF-8 content works."""
        msg_str = (
            'From: Test <test@example.com>\n'
            'Subject: UTF-8 Test\n'
            'Message-Id: <utf8@example.com>\n'
            'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            'Content-Type: text/plain; charset=utf-8\n'
            '\n'
            'Non\u2011breaking hyphen and \u00e9\u00e8\u00ea accents.'
        )
        msg_bytes = msg_str.encode()

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        body = get_commit_body(bare_repo)
        assert b'hyphen' in body


class TestAddRfc822UnicodeHeaders:
    """Tests for Unicode in email headers."""

    def test_unicode_in_subject(self, bare_repo: Path) -> None:
        """Test Subject header with Unicode characters."""
        msg = email.message.EmailMessage()
        msg['From'] = 'Test User <test@example.com>'
        msg['Subject'] = "Caf\u00e9 menu \u2014 today's special"
        msg['Message-Id'] = '<unicode-subject@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        msg.set_content('Body text.')

        ezpi.add_rfc822(str(bare_repo), msg)

        # Commit message should contain the decoded subject
        commit_msg = get_commit_message(bare_repo)
        assert 'Caf' in commit_msg
        assert 'menu' in commit_msg

    def test_unicode_in_from_name(self, bare_repo: Path) -> None:
        """Test From header with Unicode in display name."""
        msg = email.message.EmailMessage()
        msg['From'] = 'Ren\u00e9 Fran\u00e7ois <rene@example.com>'
        msg['Subject'] = 'Unicode From Test'
        msg['Message-Id'] = '<unicode-from@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        msg.set_content('Body text.')

        ezpi.add_rfc822(str(bare_repo), msg)

        body = get_commit_body(bare_repo)
        assert b'rene@example.com' in body

    def test_unicode_in_multiple_headers(self, bare_repo: Path) -> None:
        """Test Unicode in multiple headers simultaneously."""
        msg = email.message.EmailMessage()
        msg['From'] = '\u5c71\u7530\u592a\u90ce <yamada@example.jp>'  # Japanese name
        msg['To'] = 'M\u00fcller <mueller@example.de>'  # German umlaut
        msg['Subject'] = '\u4e2d\u6587\u4e3b\u9898'  # Chinese subject
        msg['Message-Id'] = '<unicode-multi@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        msg.set_content('Body with \u00e9\u00e8\u00ea accents.')

        ezpi.add_rfc822(str(bare_repo), msg)

        body = get_commit_body(bare_repo)
        assert b'yamada@example.jp' in body
        assert b'mueller@example.de' in body

    def test_rfc2047_encoded_subject(self, bare_repo: Path) -> None:
        """Test already RFC2047-encoded Subject header."""
        # This is how Unicode subjects often arrive from external sources
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: =?utf-8?q?Caf=C3=A9_menu?=\n'
            b'Message-Id: <rfc2047@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'Body text.\n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        # Subject should be decoded for commit message
        commit_msg = get_commit_message(bare_repo)
        assert 'Caf' in commit_msg

    def test_rfc2047_encoded_from(self, bare_repo: Path) -> None:
        """Test already RFC2047-encoded From header."""
        msg_bytes = (
            b'From: =?utf-8?b?UmVuw6kgRnJhbsOnb2lz?= <rene@example.com>\n'
            b'Subject: RFC2047 From Test\n'
            b'Message-Id: <rfc2047-from@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'Body text.\n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        body = get_commit_body(bare_repo)
        assert b'rene@example.com' in body


class TestAddRfc822UnicodeEdgeCases:
    """Tests for Unicode edge cases - goal is to always write, even if data is lossy."""

    def test_invalid_utf8_bytes_in_body(self, bare_repo: Path) -> None:
        """Test message with invalid UTF-8 byte sequences in body."""
        # \xff\xfe is invalid UTF-8
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Invalid UTF-8 Test\n'
            b'Message-Id: <invalid-utf8@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'Content-Type: text/plain; charset=utf-8\n'
            b'\n'
            b'Valid text then \xff\xfe invalid bytes.\n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        body = get_commit_body(bare_repo)
        assert b'Valid text' in body

    def test_null_bytes_in_body(self, bare_repo: Path) -> None:
        """Test message with null bytes in body."""
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Null Byte Test\n'
            b'Message-Id: <null-byte@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'Before null\x00after null.\n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        body = get_commit_body(bare_repo)
        assert b'Before null' in body

    def test_malformed_rfc2047_subject(self, bare_repo: Path) -> None:
        """Test message with malformed RFC2047 encoded subject."""
        # Malformed: missing closing ?=
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: =?utf-8?q?Broken_encoding\n'
            b'Message-Id: <malformed-rfc2047@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'Body text.\n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        # Should still create a commit, even if subject is mangled
        body = get_commit_body(bare_repo)
        assert b'Body text' in body

    def test_latin1_body_claiming_utf8(self, bare_repo: Path) -> None:
        """Test message claiming UTF-8 but containing Latin-1 encoded text."""
        # \xe9 is é in Latin-1 but invalid as standalone UTF-8
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Latin1 as UTF8 Test\n'
            b'Message-Id: <latin1-utf8@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'Content-Type: text/plain; charset=utf-8\n'
            b'\n'
            b'Caf\xe9 should be \xc3\xa9 in proper UTF-8.\n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        body = get_commit_body(bare_repo)
        assert b'Caf' in body

    def test_empty_body(self, bare_repo: Path) -> None:
        """Test message with empty body."""
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Empty Body Test\n'
            b'Message-Id: <empty-body@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        commit_msg = get_commit_message(bare_repo)
        assert 'Empty Body Test' in commit_msg

    def test_only_whitespace_body(self, bare_repo: Path) -> None:
        """Test message with only whitespace in body."""
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Whitespace Body Test\n'
            b'Message-Id: <whitespace-body@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'   \n\t\n   \n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        commit_msg = get_commit_message(bare_repo)
        assert 'Whitespace Body Test' in commit_msg

    def test_very_long_subject_with_unicode(self, bare_repo: Path) -> None:
        """Test very long subject line with Unicode."""
        long_subject = 'Re: ' + '\u00e9' * 200  # 200 é characters
        msg = email.message.EmailMessage()
        msg['From'] = 'Test <test@example.com>'
        msg['Subject'] = long_subject
        msg['Message-Id'] = '<long-subject@example.com>'
        msg['Date'] = 'Mon, 01 Jan 2024 12:00:00 +0000'
        msg.set_content('Body text.')

        ezpi.add_rfc822(str(bare_repo), msg)

        body = get_commit_body(bare_repo)
        assert b'Body text' in body

    def test_binary_garbage_that_looks_like_email(self, bare_repo: Path) -> None:
        """Test bytes that have minimal valid headers but garbage body."""
        msg_bytes = (
            b'From: Test <test@example.com>\n'
            b'Subject: Binary Garbage Test\n'
            b'Message-Id: <binary-garbage@example.com>\n'
            b'Date: Mon, 01 Jan 2024 12:00:00 +0000\n'
            b'\n'
            b'\x00\x01\x02\x03\x80\x81\x82\xff\xfe\xfd\n'
        )

        ezpi.add_rfc822(str(bare_repo), msg_bytes)

        commit_msg = get_commit_message(bare_repo)
        assert 'Binary Garbage Test' in commit_msg


class TestAddRfc822Validation:
    """Tests for add_rfc822 input validation."""

    def test_missing_subject_raises(self, bare_repo: Path) -> None:
        """Test that missing Subject header raises ValueError."""
        msg_bytes = b"""From: Test User <test@example.com>
Message-Id: <nosubject@example.com>

Body without subject.
"""
        with pytest.raises(ValueError, match='Subject'):
            ezpi.add_rfc822(str(bare_repo), msg_bytes)

    def test_missing_from_raises(self, bare_repo: Path) -> None:
        """Test that missing From header raises ValueError."""
        msg_bytes = b"""Subject: No From Test
Message-Id: <nofrom@example.com>

Body without from.
"""
        with pytest.raises(ValueError, match='From'):
            ezpi.add_rfc822(str(bare_repo), msg_bytes)
