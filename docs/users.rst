Application users
=================

``pmxbot.users.UserStore`` provides canonical application identity in the main
SQLite database. It uses the existing ``SQLiteStorage`` lifecycle and accepts
the same SQLite URI (or a filename). Opening the store creates the ``users``
and ``user_passwords`` tables if absent; opening it repeatedly preserves all
existing records. No historical nicknames or attribution strings are imported.
There is no separate database, migration framework, or bot startup hook.
The web viewer uses this service for login; see :doc:`deployment`.

For example, code independent of either the bot or web process can use::

    from pmxbot.users import UserStore

    store = UserStore('sqlite:pmxbot.sqlite')
    try:
        user = store.create('Alice', display_name='Alice Example', password='example passphrase')
        assert store.get_by_username(' alice ').id == user.id
        assert store.authenticate('alice', 'example passphrase').id == user.id
        store.update(user.id, enabled=False)
    finally:
        store.close()

``create``, ``get_by_id``, ``get_by_username``, ``list_users``, and ``update``
return immutable ``User`` records (a list for ``list_users``). IDs are stable
integers and are never reused, even if a row is removed externally. Creation
times are SQLite UTC timestamps in ``YYYY-MM-DD HH:MM:SS`` format. Updates can
change display names, enabled state, and the pairing grant; usernames and IDs
remain fixed. Missing records raise ``UserNotFound``. Invalid field types raise
``UserError``.

Usernames are trimmed, then must contain 1–64 ASCII letters, digits, dots,
underscores, or hyphens, starting with a letter or digit. The lookup key is the
trimmed username's ``casefold()``; the stored username preserves its letter case.
Invalid usernames raise ``InvalidUsername`` on both create and lookup.
``UsernameTaken`` reports a duplicate normalized username. SQLite enforces
uniqueness, valid stored usernames, and agreement of the lookup key with the
username (ASCII casefold equals SQLite's ``lower``), including concurrent writes.
Display names are human-facing text and need not be unique.

Accounts default to enabled, with ``can_pair_irc=False``. Future pairing code
must check ``user.may_pair_irc``, which requires both ``enabled`` and the explicit
``can_pair_irc`` grant. Web authentication rejects disabled accounts.
This is a narrow permission policy; granting it does not authenticate an IRC
session. The web viewer provides local login and server-side sessions. F30 must
store authenticated session bindings separately and refer to
``users.id``. Nicknames, hostmasks, ``created_by``, and ``requested_by`` strings
are unverified attribution and must never serve as canonical user references.

Passwords
---------

``create(..., password=...)`` can attach local credentials atomically, and
``set_password(user.id, password)`` sets or replaces them later. Accounts without
a password cannot authenticate locally. ``authenticate(username, password)``
returns a public ``User`` record only when the password matches and the account
is enabled. Wrong passwords, missing accounts, missing credentials, malformed
hashes, and disabled accounts raise the same ``InvalidCredentials`` error.
Valid password inputs perform the same derivation even for unknown accounts.

Passwords are stored as one-way salted hashes, never recoverable encryption or
plaintext. The standard-library PBKDF2-HMAC-SHA256 implementation uses 600,000
iterations and an independent random 16-byte salt for each assignment. Algorithm,
work factor, salt, and digest are stored in ``user_passwords`` with a foreign key
to ``users.id``. Hashes are excluded from public user records and listings.
Verification uses a constant-time digest comparison. The credential table is
added safely to databases that already contain users; existing users retain
their IDs and have no password until explicitly assigned one.

Passwords accept 1–1024 Unicode characters, including spaces, without trimming,
normalization, or truncation. This is a storage limit, not a password strength
policy. Administrators provision strong passwords; web transport, rate limiting,
and sessions are documented in :doc:`deployment`. Web registration enforces
matching passwords and its strength policy, and creates disabled accounts
awaiting administrator approval. There is no password reset UI.
