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
matching passwords and its strength policy, and creates active accounts that
can sign in immediately, save favorite covers and enable Torta Mode. Image
creation and editing remain disabled until an administrator explicitly grants
image-generation permission. Administrators can reset passwords through the
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
exists yet, register through the web form or provision one with
``UserStore.create(..., password=...)`` in a trusted Python session.

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
main database. Email-based password recovery is not provided.

Favorite album artwork
----------------------

Every enabled, authenticated user may favorite artwork, including users without
image-generation permission. Favorites are independent of administrator roles
and of any future image ratings. A favorite chooses the version that represents
an album; it does not rate image quality.

Albums with at least two artwork versions show a star beside the selected image.
Select the outlined star to save that version as your favorite, replacing your
previous choice for the album. Select the filled star to remove your preference.
The button supports keyboard activation and announces save results. Saving uses
a CSRF-protected asynchronous request and preserves the selected artwork,
browser history and unsaved generation settings. If a response is lost, the page
rechecks your saved preference. If that also fails, reload to verify the state.

Gallery covers and initial album artwork use the same selection rules:

#. A valid explicit image selection takes precedence (including shared URLs and
   history navigation).
#. Your personal favorite wins when present.
#. Without a personal favorite, a unique leader in aggregate favorites wins.
#. Tied or zero counts retain the newest-image fallback: creation timestamp
   descending, then image ID descending.

Album order, pagination and missing-thumbnail fallbacks remain unchanged.
Favorites affect subsequent page loads, without automatically changing artwork
already selected in an open album page. Individual users' preferences are not
listed publicly; anonymous visitors see only the resulting default artwork.

``pmxbot.favorites.FavoriteStore`` initializes the additive SQLite schema when
first used for a mutation. Read-only gallery and album requests work unchanged
on databases without this table. ``user_album_favorites`` stores user, album,
image, creation timestamp and a cache key used to enforce image membership with
composite foreign keys. The primary key is ``(user_id, album_id)``. Deleting a
user, album, image or album/image link cascades to its favorite records when
using the application's foreign-key-enabled connections. Other user and artwork
records are preserved. Toggle operations serialize with ``BEGIN IMMEDIATE``;
cover selection batches a gallery page in a fixed number of queries.

Torta Mode
----------

Signed-in users see a **Torta Mode** checkbox beside **Sign out** in the header
on every page. It defaults to off and is stored per account in the SQLite
``users.nsfw`` boolean column. Existing databases gain this column additively;
existing user identities, hashes, roles and other permissions remain unchanged.
The setting is independent of image generation and administrator access, so any
enabled signed-in user may enable or disable it for their own account.

When off, list and gallery views omit albums whose band name or album title
contains the whole word ``torta``, ``tortas``, ``nalga`` or ``nalgas``, ignoring
case. This applies to search results, result counts, pagination and Previous/Next
album navigation.
Longer words such as ``Tortasaurus`` and ``Retorta`` remain visible. Enabling
Torta Mode removes this filter for that user. Direct album URLs remain readable
regardless of the setting.

Anonymous visitors see neither the checkbox nor its label and always receive
filtered collections. Signing out restores that behavior; signing back in
restores the account's saved setting. Changes require an authenticated,
CSRF-protected POST and do not revoke existing sessions. The checkbox submits
on change; without JavaScript, use its **Save Torta Mode** button.

Prompt clarification rules
--------------------------

Administrators grant **Prompt rules → Allowed** on **Administration → Users**.
The ``can_manage_prompt_rules`` capability defaults to denied for both new and
existing users. It is independent of administrator role, image generation and
IRC pairing. Changing it revokes existing sessions and records before/after
values in ``user_admin_audit``. A permitted ordinary user can use **Prompt rules**
in the navigation without receiving paid generation or user administration
access. An administrator without this grant cannot manage or preview rules.

At ``/admin/prompt_rules``, create, edit, delete, enable or disable a rule.
Choose **Where to match**: **Band name**, **Album title**, or **Either name**;
enter a normal word or phrase in **Text to find**; then choose **Match type**:
**Whole word/phrase** (the default) or **Contains text**. Each rule also needs
nonblank clarification text, priority and an enabled state.
**Save rule and order** saves clarification, priority and state while preserving
all existing matching conditions. All enabled matches apply once per rule,
sorted by ascending numeric priority, then ascending stable rule ID. Identical
instruction text in separate rules is not deduplicated.

Both match types compare after Unicode NFKC, whitespace collapse and casefold
normalization, as used for music identities. For example, ``STRASSE`` matches
``Straße``, and ``blue moon`` matches ``BLUE`` followed by a tab and ``moon``.
Whole word/phrase requires that neither end touches a Unicode letter, number
or underscore: ``ass`` matches ``big-ass!`` but does not match ``bass``,
``classic`` or ``ass_band``. Contains text permits matches inside larger words.
Punctuation, hyphens and all other supplied characters are literal: ``a+b``
matches that text; ``blue moon`` does not match ``blue-moon``. Either name tests
the band and album separately and applies once even if both match; a phrase
cannot span the names in a new rule.

Text to find is limited to 255 characters, clarification text to 10000,
and preview band/album inputs to 1000 each. NUL and other control characters
are rejected except tabs and line breaks. Priorities are integers from
-2147483648 to 2147483647. Rule IDs are positive integers up to 2147483647.
Invalid forms retain recognized scalar values and display safe validation
errors; unknown fields and repeated values are never reflected.

**Preview** uses saved rules and the same evaluator and album prompt builder as
production. It displays the matching rule IDs in order with readable matching
descriptions, appended text and full initial album prompt. It calls no image provider, creates no jobs and saves no
albums or artwork. Save a rule before previewing its effect. Management and
preview submissions require a signed-in active account and session-bound CSRF
protection; accounts requiring a password change must complete it first.

On first use, a transactional migration seeds all seven existing name-dependent
clarifications. These compatibility rules preserve original Unicode normalization,
whole-word boundaries, Torta plural handling, the two distinct Torta hints,
Medve's once-only either-field match, and the title-context shared introductory
and metaphor wording. Each rule describes its compatibility matching without
displaying internal conditions. Existing ordinary rules retain their original contains-text
behavior; rules with both band and album conditions still require both (AND),
and the summary shows both conditions. To change any rule's conditions, open
**Replace matching conditions**, choose the new structured controls, and press
its separate button. Replacement uses the selected match behavior and replaces
all previous conditions, including legacy combined-name and AND conditions.
Changing only clarification, priority or enabled state preserves compatibility.
Deleted seeds never return on restart, and edited seeds are never overwritten.
