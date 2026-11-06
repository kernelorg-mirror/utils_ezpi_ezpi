Release notes
=============

Unreleased
----------

Python 3.11 or newer is required
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

ezpi no longer supports Python 3.9 and 3.10. Both have reached their
end of life, and pygit2 no longer ships wheels for them. ezpi is now
tested on Python 3.11 to 3.15.

Messages passed as bytes are stored unchanged
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:func:`ezpi.add_rfc822` and :func:`ezpi.add_rfc822_v2` now store a
message given as ``bytes`` exactly as received. Before, ezpi parsed the
message and wrote it out again. This transcoded 8-bit bodies in other
charsets to UTF-8 and added MIME headers, which broke DKIM signatures.

ezpi still makes two changes to ``bytes`` input:

- If ``Date`` or ``Message-Id`` is missing, ezpi adds it at the top of
  the headers.
- A leading mbox ``From`` line is dropped.

Line endings are now kept as they are. Before, CRLF line endings were
changed to LF.

``email.message.Message`` objects work as before: a text payload with
non-ASCII content or no charset is re-encoded as UTF-8.

Non-ASCII text in add_plaintext()
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:func:`ezpi.add_plaintext` stored non-ASCII characters in the content as
U+FFFD replacement characters. It now stores them as UTF-8.

.. _release-0.7.0:

v0.7.0
------

This release makes writes safe when several writers work on the same
inbox at the same time. There is no new API.

Locking that works with public-inbox
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

ezpi now locks ``inbox.lock``, the per-epoch ``ezpi.lock`` and the
cursor locks with ``flock(2)``. Before, it used ``lockf(3)``. On Linux,
the two kinds of lock never block each other, so an ezpi write could
run at the same time as ``public-inbox-index`` or
``public-inbox-purge``. ``lockf(3)`` locks also belong to the whole
process, so two threads writing to the same inbox did not wait for
each other. Both problems are now fixed.

.. warning::

   An ezpi 0.6 writer and an ezpi 0.7.0 writer do not lock each other
   out. If more than one program writes to the same inbox, upgrade all
   of them together.

Safe creation of new v2 inboxes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When several workers wrote the first messages of a new inbox at the
same time, most of them failed with ``FileNotFoundError``. ezpi now
builds a new inbox under a temporary ``.ezpi-init-*`` name next to the
target and renames it into place, so other writers never see a
half-made inbox.

If the inbox cannot be created, ezpi now raises ``RuntimeError``, as
documented, instead of a bare ``OSError``. If you caught ``OSError``
for this case, catch ``RuntimeError`` instead.

Other notable changes
~~~~~~~~~~~~~~~~~~~~~

- **Post-commit hook for v2 inboxes** -- the CLI used to ignore
  ``--run-post-commit-hook`` together with ``--v2-path``. It now runs
  ``<inbox>/hooks/post-commit`` with the inbox as the working
  directory. The hook lives in the inbox, not in an epoch, so it keeps
  working when a new epoch is created. See :doc:`cli`.
- **No more leaked file descriptors** -- lock files and the pipes of
  ``git cat-file`` are now always closed. Callers that run their tests
  with ``-W error`` no longer see ``ResourceWarning``, and long-running
  writers no longer risk running out of file descriptors.

.. _release-0.6:

v0.6
----

Reading public-inbox v2 repositories
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

ezpi has grown a read API. You can now iterate over messages in a
local public-inbox v2 inbox that someone else is keeping in sync --
for example, a clone of ``lore.kernel.org`` refreshed with ``git pull``,
``lei up``, or ``rsync``. ezpi itself never fetches from remotes; it
only consumes what is already on disk.

Readers identify themselves with a **named cursor**, and ezpi remembers
per-cursor where each reader left off, in a plain JSON file stored
alongside the inbox at ``{inbox}/ezpi-cursor.{name}.json``. The
top-level entry point is :func:`ezpi.iter_new_messages`, which yields
only messages the cursor has not yet seen::

    for epoch, commit, msg_bytes in ezpi.iter_new_messages(
        '/path/to/inbox', 'myapp', auto_advance=True,
    ):
        process(msg_bytes)

Highlights:

- **First-use policy** -- on the first call, ezpi seeds the cursor at
  the current HEAD of every epoch and yields nothing, so readers start
  from "now." Pass ``start='beginning'`` to backfill the entire history
  instead.
- **Automatic new-epoch detection** -- when a public-inbox rolls over
  to a new epoch, ezpi notices on the next call and walks the new
  epoch from its first commit. No caller action is required.
- **Automatic rebase recovery** -- public-inbox repositories
  occasionally rewrite history to permanently delete a message. When
  ezpi detects that the cursor's stored commit is no longer reachable,
  it relocates the cursor by matching on Message-ID and Subject,
  anchored by the stored commit date. Recovery is transparent to
  callers.
- **Manual or automatic cursor advance** -- with ``auto_advance=True``,
  ezpi persists cursor state after every yielded message. For delivery
  flows that can fail partway through (e.g., a flaky IMAP server), use
  manual advance and call :func:`ezpi.save_cursor` only after
  confirming delivery.

.. note::

   Rebase recovery uses ``git rev-list --since-as-filter``, which
   requires **git 2.40 or newer**. Ordinary reading works on any git.

See :doc:`quickstart` for a walkthrough and :doc:`api` for the full
reference.

Faster reads via persistent ``cat-file --batch``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Internally, epoch iteration now keeps a single ``git cat-file --batch``
process alive per epoch instead of spawning one subprocess per blob
lookup. This substantially reduces per-message overhead when walking
large archives.

Other notable changes
~~~~~~~~~~~~~~~~~~~~~

- **b4-style CI and linter config** -- ezpi adopted the CI harness
  and expanded linter configuration used by the b4 project, including
  a matrix build across supported Python versions
  (``ci.sh`` / ``ci-matrix.sh``).
- **Test coverage parity with korgalore** -- the test suite was
  extended to close gap-coverage holes identified against
  korgalore's pi-read tests.

.. _release-0.5.1:

v0.5.1
------

Packaging fixes for PyPI and older setuptools:

- **PyPI rendering** -- ``long_description_content_type`` is now
  declared explicitly so the project description renders as
  reStructuredText on PyPI instead of as plain text.
- **Older setuptools compatibility** -- restored ``setup.cfg`` and
  ``setup.py`` fallback shims for environments that cannot install
  from PEP 517-only projects.

.. _release-0.5.0:

v0.5.0
------

Public-inbox v2 format support
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

ezpi now writes directly to public-inbox v2 format inboxes, creating
the standard directory layout (``inbox.lock``, ``git/*.git`` epochs,
and ``all.git`` with alternates) and managing epoch rotation
automatically. The main entry point is :func:`ezpi.add_rfc822_v2`::

    ezpi.add_rfc822_v2('/path/to/inbox', msg_bytes)

Two rotation modes are supported: ``size`` (default; rotate when the
current epoch exceeds 1 GB) and ``annual`` (rotate on the first write
of a new calendar year). The CLI exposes these via ``--v2-path`` and
``--auto-epoch``::

    ezpi --v2-path /path/to/inbox --auto-epoch annual --rfc822 < msg.eml

See :doc:`v2` for the full v2 story.

Other notable changes
~~~~~~~~~~~~~~~~~~~~~

- **Optional pygit2 support** -- install ``ezpi[libgit2]`` to use
  ``pygit2`` for faster git operations when available. The
  ``git`` subprocess path remains the default and the fallback.
- **Sphinx documentation with Read the Docs** -- a full Sphinx
  documentation set with RTD integration, hosted at
  https://ezpi.readthedocs.io/.
- **pyproject.toml and src layout** -- the project now uses a modern
  ``pyproject.toml`` build with a ``src/ezpi`` layout.
- **pytest suite** -- tests are organised into logical groups under
  ``tests/`` and run via ``pytest``.
- **ruff and mypy --strict** -- static analysis runs on every change.
- **pipx-first install instructions** -- ``pipx install ezpi`` is the
  recommended way to get the CLI.
- **Unicode robustness** -- fixed encoding errors in
  :func:`ezpi.add_rfc822` when messages contain non-ASCII bytes.
- **IDE-friendly docstrings** -- public functions now carry
  docstrings for editor tooling.
