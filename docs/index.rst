EZPI Documentation
==================

EZPI is a Python library for writing RFC2822 messages to public-inbox
compatible git repositories. It provides both a Python API and a CLI tool.

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   quickstart
   cli
   api
   v2
   releases

Features
--------

- Write RFC2822 email messages to bare git repositories
- Support for both bytes and ``email.message.Message`` objects
- Automatic generation of missing headers (Date, Message-Id)
- Proper handling of Unicode content and headers
- Public-inbox v2 format support with automatic epoch management
- Read messages from local v2 inboxes using named cursors, with automatic
  rebase recovery
- CLI tool for integration with shell scripts

.. versionadded:: 0.6
   Reading support via :func:`ezpi.iter_new_messages` and named cursors.
   See :doc:`quickstart` for usage and :doc:`api` for the full reference.

Installation
------------

For CLI usage, install with pipx::

    pipx install ezpi

For library usage, install with pip::

    pip install ezpi

Or install from source::

    git clone https://git.kernel.org/pub/scm/utils/ezpi/ezpi.git
    cd ezpi
    pip install .

Quick Example
-------------

.. code-block:: python

    import ezpi

    # Add an RFC822 message from bytes
    with open('message.eml', 'rb') as f:
        ezpi.add_rfc822('/path/to/repo.git', f.read())

    # Add plaintext content
    ezpi.add_plaintext(
        '/path/to/repo.git',
        content='Hello world',
        subject='Test',
        authorname='Sender',
        authoremail='sender@example.com',
    )

Getting help
------------

To report a problem or suggest a feature, please send plaintext email to
tools@kernel.org.

License
-------

EZPI is licensed under the MIT-0 license.

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
