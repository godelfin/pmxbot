Application users
=================

``pmxbot.users.UserStore`` provides canonical application identity in the main
SQLite database. It uses the existing ``SQLiteStorage`` lifecycle and accepts
the same SQLite URI (or a filename). Opening the store creates the ``users``,
``user_passwords`` and ``user_admin_audit`` tables if absent; opening it repeatedly
preserves all
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
awaiting administrator approval. Administrators can reset passwords through the
user-management page.


Administration and permissions
------------------------------

Roles are ``user`` (the default) and ``admin``. Roles control administration
only. ``can_generate_images`` independently controls artwork generation, and
``can_pair_irc`` retains its separate pairing policy. Promoting or demoting a
user never changes either grant. ``User.has_permission('generate_images')``
checks an explicit capability; ``User.is_admin`` checks administration access.
Disabled accounts and accounts awaiting a mandatory password change cannot use
these capabilities. ``enabled`` is the account-active flag.

Opening an older SQLite database adds the role, image-generation grant,
mandatory-password-change flag, session version, and ``user_admin_audit`` table
transactionally. Existing IDs, usernames, password hashes, pairing grants,
enabled flags and any existing image-generation grants are preserved. Before
this migration, all enabled authenticated users could generate images, so a
missing image-generation column is backfilled as allowed for existing accounts
only. New accounts default to denied and require an explicit administrator grant,
independently of account activation. Existing grants and denials remain unchanged
when the store is reopened.

Bootstrap the first administrator from a trusted local shell, using the same
SQLite database as the viewer and an existing enabled account with a password::

    python -m pmxbot.users_cli --database sqlite:/path/to/pmxbot.sqlite Alice

This explicit operation takes no password on the command line, is audited, and
refuses to run while an active administrator exists. It preserves all image
permissions. Bootstrap invalidates the user's existing sessions; sign in again.
Restrict operating-system access to the database and this shell. If no user
exists yet, provision one with ``UserStore.create(..., password=...)`` in a
trusted Python session, or approve a registered account using the local API.

Administrators see **Administration → Users** in the navigation. The page lists
all accounts and allows changing role, image permission and active status.
Saving changes and resetting passwords require POST and a session-bound CSRF
token. The last active administrator cannot be disabled or demoted, including
by concurrent requests. A user cannot promote themself without existing admin
access. The local ``update`` and ``set_password`` APIs are trusted provisioning
operations; web handlers use ``admin_update`` and ``admin_reset_password``,
which recheck the actor's current role and session version inside the write
transaction.

A reset generates a random temporary password, displayed only in the POST
response. Copy it and communicate it securely. The password is never stored in
plaintext or included in audit records. The user must sign in with it and set a
different password before accessing other pages or authenticated operations.
The change form uses the registration strength policy, requires the current
password and matching confirmation, and signs the user out on success.
Administrators may reset their own password; they are signed out and must
complete the same flow.

Password, role, permission and account-status changes increment a persistent
session version. Each authenticated request re-reads the account and rejects
stale sessions, including sessions on another web process. Re-enabling an
account or restoring a permission does not restore old sessions. Public
browsing remains available to anonymous visitors.

``user_admin_audit`` persists bootstrap, account edits, and administrative resets
in the same transaction as each mutation. Records contain an actor ID (NULL
for local bootstrap), target ID, action, SQLite UTC timestamp and JSON changes.
Account edits include before/after values; resets record only the mandatory
change flag. Passwords and hashes are excluded. Failed operations roll back and
do not produce successful-action audit entries. Back up this table with the
main database. No email recovery or registration behavior is changed.
