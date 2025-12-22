Quick Start
===========

This guide will help you get started with EZPI.

Installation
------------

For CLI usage, install with pipx::

    pipx install ezpi

For library usage, install with pip::

    pip install ezpi

Creating a Repository
---------------------

EZPI writes to bare git repositories. Create one first::

    git init --bare /path/to/messages.git

Writing Messages
----------------

From Bytes
^^^^^^^^^^

If you have raw RFC822 message data::

    import ezpi

    msg_bytes = b"""From: sender@example.com
    Subject: Hello World

    This is the message body.
    """

    ezpi.add_rfc822('/path/to/messages.git', msg_bytes)

From Message Objects
^^^^^^^^^^^^^^^^^^^^

Using Python's email library::

    import email.message
    import ezpi

    msg = email.message.EmailMessage()
    msg['From'] = 'sender@example.com'
    msg['Subject'] = 'Hello World'
    msg.set_content('This is the message body.')

    ezpi.add_rfc822('/path/to/messages.git', msg)

From Plaintext
^^^^^^^^^^^^^^

For plaintext content::

    import ezpi

    ezpi.add_plaintext(
        '/path/to/messages.git',
        content='This is the message body.',
        subject='Hello World',
        authorname='Sender Name',
        authoremail='sender@example.com',
    )

Using the CLI
-------------

EZPI provides a command-line interface::

    # Add an RFC822 message
    ezpi -r /path/to/messages.git --rfc822 < message.eml

    # Add plaintext
    echo "Message body" | ezpi -r /path/to/messages.git \
        -f "Sender <sender@example.com>" \
        -s "Subject line"

See :doc:`cli` for full CLI documentation.

Using v2 Format
---------------

For public-inbox v2 format repositories with automatic epoch management::

    import ezpi

    # Writes to v2 inbox, creating it if needed
    ezpi.add_rfc822_v2('/path/to/inbox', msg_bytes)

Or via CLI::

    ezpi --v2-path /path/to/inbox --rfc822 < message.eml

See :doc:`v2` for full v2 format documentation.
