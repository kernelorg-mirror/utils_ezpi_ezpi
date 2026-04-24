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

Reading Messages
----------------

.. versionadded:: 0.6

EZPI can also read messages out of a public-inbox v2 repository that someone
else has populated (for example, a local clone of ``lore.kernel.org``). EZPI
does **not** fetch or clone remotes for you -- keep the repository up to
date yourself with ``git pull``, ``lei up``, ``rsync``, or whatever tool
fits your workflow.

Named Cursors
^^^^^^^^^^^^^

Readers identify themselves with a **cursor name**. EZPI remembers, per
cursor, the last commit seen in each epoch so subsequent calls only yield
new messages. Cursors are plain JSON files stored next to the inbox
(``{inbox}/ezpi-cursor.{name}.json``).

First use (default): cursor is initialized at the current HEAD of every
epoch and nothing is yielded. Only messages added after that first call
will be delivered to the cursor::

    import ezpi

    # First call -- seeds the cursor at HEAD, yields nothing.
    for epoch, commit, msg_bytes in ezpi.iter_new_messages('/path/to/inbox', 'myapp'):
        ...

    # Remote has added two messages; sync the repo first, then:
    for epoch, commit, msg_bytes in ezpi.iter_new_messages(
        '/path/to/inbox', 'myapp', auto_advance=True,
    ):
        process(msg_bytes)  # yielded twice

Pass ``start='beginning'`` to walk the full history on first use instead::

    for epoch, commit, msg_bytes in ezpi.iter_new_messages(
        '/path/to/inbox', 'myapp', start='beginning',
    ):
        process(msg_bytes)

Manual vs. Automatic Advance
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

With ``auto_advance=True``, EZPI persists cursor state after each yielded
message. If your processing can fail partway through (e.g., you deliver to
a flaky IMAP server), use manual advance so the cursor only moves once you
confirm success::

    for epoch, commit, msg_bytes in ezpi.iter_new_messages('/path/to/inbox', 'myapp'):
        if deliver(msg_bytes):
            ezpi.save_cursor('/path/to/inbox', 'myapp', epoch, commit, msg_bytes)

New Epochs
^^^^^^^^^^

When a public-inbox rolls over to a new epoch, EZPI notices automatically
on the next ``iter_new_messages`` call and walks the new epoch from its
first commit. No caller action is required.

Rebase Recovery
^^^^^^^^^^^^^^^

public-inbox repositories occasionally rewrite history to permanently
delete a message. When EZPI detects that the cursor's stored commit is no
longer on the current history, it locates the new position by matching on
Message-ID and Subject (anchored by the stored commit date) and resumes.
Recovery is automatic -- no special handling in caller code.

.. note::

    Rebase recovery uses ``git rev-list --since-as-filter``, which requires
    **git 2.40 or newer**. Ordinary message reading works on any git.

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
