Application users
=================

``pmxbot.users.UserStore`` provides canonical application identity in the main
SQLite database. It uses the existing ``SQLiteStorage`` lifecycle and accepts
the same SQLite URI (or a filename). Opening the store creates the ``users``
table if absent; opening it repeatedly preserves all existing records. No
historical nicknames or attribution strings are imported. There is no separate
database, migration framework, authentication handler, or bot startup hook.

For example, code independent of either the bot or web process can use::

    from pmxbot.users import UserStore

    store = UserStore('sqlite:pmxbot.sqlite')
    try:
        user = store.create('Alice', display_name='Alice Example')
        assert store.get_by_username(' alice ').id == user.id
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
``can_pair_irc`` grant. Future authentication must reject disabled accounts.
This is a narrow permission policy; granting it does not authenticate an IRC
session. F7 must decide how credentials or external authentication IDs attach
to users. F30 must store authenticated session bindings separately and refer to
``users.id``. Nicknames, hostmasks, ``created_by``, and ``requested_by`` strings
are unverified attribution and must never serve as canonical user references.
