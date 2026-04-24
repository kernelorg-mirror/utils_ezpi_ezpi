Python API Reference
====================

This page documents the public Python API for EZPI.

Core Functions
--------------

.. autofunction:: ezpi.add_rfc822

.. autofunction:: ezpi.add_plaintext

v2 Format Functions
-------------------

.. autofunction:: ezpi.add_rfc822_v2

.. autofunction:: ezpi.init_v2_inbox

.. autofunction:: ezpi.init_epoch

.. autofunction:: ezpi.get_latest_epoch

.. autofunction:: ezpi.get_epoch_size

.. autofunction:: ezpi.should_rotate_epoch

Reading Functions
-----------------

.. versionadded:: 0.6

These functions let you consume messages out of a v2 inbox that you (or
someone else) is keeping up to date. EZPI itself never fetches from
remotes -- refresh the local checkout with ``git pull``, ``lei up``, etc.
before calling these.

.. autofunction:: ezpi.iter_new_messages

.. autofunction:: ezpi.iter_messages

.. autofunction:: ezpi.get_all_epochs

Cursor State
^^^^^^^^^^^^

.. versionadded:: 0.6

Named cursors track per-reader position. EZPI stores one JSON file per
cursor alongside the inbox, at ``{v2path}/ezpi-cursor.{name}.json``.

.. autofunction:: ezpi.load_cursor

.. autofunction:: ezpi.save_cursor

.. autofunction:: ezpi.reset_cursor

.. autofunction:: ezpi.recover_cursor

Reading Exceptions
^^^^^^^^^^^^^^^^^^

.. versionadded:: 0.6

.. autoexception:: ezpi.CursorError

.. autoexception:: ezpi.CursorStateError

.. autoexception:: ezpi.StaleCommitError

Utility Functions
-----------------

.. autofunction:: ezpi.run_hook

.. autofunction:: ezpi.clean_header

Low-Level Functions
-------------------

These functions are primarily for internal use but are documented
for advanced users.

.. autofunction:: ezpi.git_write_commit

.. autofunction:: ezpi.git_run_command

.. autofunction:: ezpi.check_valid_repo

Constants
---------

.. py:data:: ezpi.DEFAULT_NAME

   Default author name used when none is provided: ``'EZ PI'``

.. py:data:: ezpi.DEFAULT_ADDR

   Default author email used when none is provided: ``'ezpi@localhost'``

.. py:data:: ezpi.DEFAULT_SUBJ

   Default subject used when none is provided: ``'EZPI commit'``

.. py:data:: ezpi.V2_SIZE_THRESHOLD

   Size threshold for epoch rotation in bytes (1GB): ``1073741824``

.. py:data:: ezpi.PI_HEAD

   Git ref used for commits: ``'refs/heads/master'``
