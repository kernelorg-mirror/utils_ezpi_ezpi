Command Line Interface
======================

EZPI provides the ``ezpi`` command for adding messages to repositories
from the command line.

Synopsis
--------

::

    ezpi (-r REPO | --v2-path V2PATH) [options] < message

Options
-------

Repository Selection (mutually exclusive, one required)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``-r REPO``, ``--repo REPO``
    Path to a bare git repository. The repository must already exist.

``--v2-path V2PATH``
    Path to a public-inbox v2 format inbox. Will be created if it
    doesn't exist.

v2 Options
^^^^^^^^^^

``--auto-epoch {size,annual}``
    Epoch rotation mode for v2 format. Only valid with ``--v2-path``.

    - ``size`` (default): Create new epoch when current exceeds 1GB
    - ``annual``: Create new epoch on first write after January 1

Message Format
^^^^^^^^^^^^^^

``--rfc822``
    Treat stdin as an RFC822 formatted message. Cannot be used with
    ``-f`` or ``-s``.

``-f FROM``, ``--from FROM``
    From header for plaintext messages. Required if not using ``--rfc822``.
    Format: ``"Name <email@example.com>"``

``-s SUBJECT``, ``--subject SUBJECT``
    Subject header for plaintext messages. Required if not using ``--rfc822``.

Other Options
^^^^^^^^^^^^^

``--domain DOMAIN``
    Domain to use when generating Message-Id headers.

``-p``, ``--run-post-commit-hook``
    Run ``hooks/post-commit`` after a successful commit, if present and
    executable. The hook runs with the repository as its working
    directory. For ``--v2-path``, the hook lives in the inbox directory
    (``<inbox>/hooks/post-commit``), not in an epoch, so it keeps working
    when a new epoch is created.

``-d``, ``--dry-run``
    Do not write the commit, just show what would be written.

``-q``, ``--quiet``
    Only output errors.

``-v``, ``--verbose``
    Show debugging output.

``-h``, ``--help``
    Show help message.

Examples
--------

Add an RFC822 message to a bare repository::

    ezpi -r /srv/mail/archive.git --rfc822 < message.eml

Add plaintext content::

    echo "Meeting notes from today's call" | ezpi -r /srv/mail/notes.git \
        -f "Alice <alice@example.com>" \
        -s "Meeting Notes 2024-01-15"

Add to a v2 inbox (auto-creates)::

    ezpi --v2-path /srv/mail/lists/linux-kernel --rfc822 < message.eml

Add to v2 with annual epoch rotation::

    ezpi --v2-path /srv/mail/lists/archive \
        --auto-epoch annual \
        --rfc822 < message.eml

Pipe from another command::

    git format-patch -1 --stdout | ezpi -r /srv/patches.git --rfc822

Exit Status
-----------

``0``
    Success.

``1``
    Error (invalid arguments, missing headers, git failure, etc.).
