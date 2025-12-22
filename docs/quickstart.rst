Quick Start
===========

This guide will help you get started with EZPI.

Installation
------------

For CLI usage, install with pipx::

    pipx install ezpi

For library usage, install with pip::

    pip install ezpi

Writing Messages
----------------

EZPI manages public-inbox v2 repositories automatically, creating the
directory structure and rotating epochs as needed.

From Bytes
^^^^^^^^^^

If you have raw RFC822 message data::

    import ezpi

    msg_bytes = b"""From: sender@example.com
    Subject: Hello World

    This is the message body.
    """

    ezpi.add_rfc822_v2('/path/to/inbox', msg_bytes)

From Message Objects
^^^^^^^^^^^^^^^^^^^^

Using Python's email library::

    import email.message
    import ezpi

    msg = email.message.EmailMessage()
    msg['From'] = 'sender@example.com'
    msg['Subject'] = 'Hello World'
    msg.set_content('This is the message body.')

    ezpi.add_rfc822_v2('/path/to/inbox', msg)

From Plaintext
^^^^^^^^^^^^^^

For plaintext content, use single-repo mode (see below)::

    import ezpi

    repo_path = '/path/to/inbox/git/0.git'
    ezpi.add_plaintext(
        repo_path,
        content='This is the message body.',
        subject='Hello World',
        authorname='Sender Name',
        authoremail='sender@example.com',
    )

Using the CLI
-------------

EZPI provides a command-line interface::

    # Add an RFC822 message to a v2 inbox
    ezpi --v2-path /path/to/inbox --rfc822 < message.eml

    # Add plaintext to a v2 inbox
    echo "Message body" | ezpi --v2-path /path/to/inbox \
        -f "Sender <sender@example.com>" \
        -s "Subject line"

See :doc:`cli` for full CLI documentation and :doc:`v2` for v2 format details.

Single-Repo Mode
----------------

For scenarios where v2 epoch management is not needed (e.g., publishing to
hosts that only support single-level repositories), you can write directly
to a bare git repository::

    git init --bare /path/to/messages.git

Then use the non-v2 functions::

    import ezpi

    ezpi.add_rfc822('/path/to/messages.git', msg_bytes)

Or via CLI::

    ezpi -r /path/to/messages.git --rfc822 < message.eml
