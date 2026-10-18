Stored tendies
==============

``!tendie`` returns a random locally stored post from the configured X account,
including its original X.com URL. Random, latest, days, and status are available
to all IRC users:

* ``!tendie latest`` returns the newest post by original creation time.
* ``!tendie days`` reports complete elapsed days since that post was created,
  rounded down using timezone-aware UTC timestamps, with its URL. Import time
  has no effect. An empty collection or malformed creation timestamp produces
  an explanatory response. Future creation times report zero days.
* ``!tendie status`` reports the number stored and the last successful import
  or live synchronization, in UTC (or ``never``).

``!tendie update`` requires an authenticated administrator. It fetches newer
posts through the official X API and reports the number inserted. Existing IDs
are never overwritten or counted twice.

Random, latest, days, and status requests make no network requests. Post text
is HTML-unescaped, stripped of IRC control characters, collapsed to a single
line, and truncated on UTF-8 boundaries to fit a 400-byte response including
its complete source URL.

Configuration
-------------

The example ``config.yaml`` includes these settings::

    database: sqlite:pmxbot.sqlite
    tendie_author: !env TENDIE_AUTHOR
    tendie_admin_accounts: {}
    x_bearer_token: !env X_BEARER_TOKEN
    tendie_sync_cooldown: 300
    tendie_include_replies: false
    tendie_include_quotes: true

Set ``tendie_author`` to the source X username in private configuration, or
set ``TENDIE_AUTHOR`` in the bot's environment. There is no built-in account.
A leading ``@`` is optional; usernames are validated before API requests.
All account names below are placeholders. Reads, incremental IDs, counts,
cooldowns, and synchronization metadata are scoped to the configured account,
so changing it does not serve posts from a previous account. Stored history
is retained if you switch back.

Authorize updates by mapping an IRC services account to an existing local
administrator username, for example::

    tendie_admin_accounts:
        services_account: local_admin

The IRC server must support the
`IRCv3 account-tag extension <https://ircv3.net/specs/extensions/account-tag>`_.
The bot requests this capability when mappings are configured. The caller
must identify to the mapped services account on that server. The server's
``account`` tag authenticates the account; nicknames, hostmasks, client-only
``+account`` tags, and text in a message do not grant access. Mapping keys are
case insensitive; ambiguous mappings are denied. The mapped local user must
currently be enabled, have role ``admin``, and not require a password change.
The role is checked for every update, so demotion or disabling takes effect
immediately. IRC account mappings must be provisioned in trusted configuration;
there is no automatic mapping from IRC nicknames to local usernames. The
public ``!config`` command cannot change ``tendie_admin_accounts`` or the
primary ``database`` setting; edit the configuration file and restart instead.

Updates are denied by default, including when the server supplies no account
tag or the caller arrives through a transport without authenticated IRC event
metadata. A denied request never opens the tendie collection, constructs an X
provider, or changes synchronization metadata or cooldown. These restrictions
apply only to live IRC updates; the local import utility remains a trusted
operator tool. Public read commands need no account mapping or authentication.

IRC updates run in a bounded background worker. The bot promptly acknowledges
mapped requests while the worker checks the current local administrator role,
opens its own database connection, and fetches/imports provider pages. No update
SQLite connection or network request runs on the IRC event-loop thread.
Requests use a snapshot of the configured database, source, token, and filtering
settings. Failed role checks return a denial without starting synchronization.

Only one IRC update may be active or awaiting result delivery; additional
requests receive a busy response rather than accumulating in an executor queue.
The worker never writes to the IRC connection directly. A scheduled callback
drains its bounded result queue on the bot thread, normally within one second,
and addresses the completion or safe error to the requester in the originating
channel or private conversation. Delivery follows normal logging and silent-mode
rules. The slot is released when the result is drained, including after errors.
The local import utility remains synchronous.

Tendie uses the existing primary ``database`` setting and pmxbot's
``SQLiteStorage`` connection and schema-initialization conventions. Opening
the collection additively creates only ``tendie_posts``, ``tendie_sync``, and
its dedicated index in that database; unrelated tables are left untouched.
There is no separate tendie database setting or fallback file. MongoDB and
in-memory databases are unsupported for this feature and produce an
explanatory response. Relative SQLite paths resolve from the process working
directory, just as for the bot's other storage classes.

Set ``X_BEARER_TOKEN`` in the bot's environment, or supply ``x_bearer_token``
through private configuration. Never commit credentials. The configuration
value takes precedence and is redacted from startup logs. Without credentials,
reads and file imports still work; update explains how to configure access.
The token needs access to the official X v2 user lookup and user posts endpoints:
`user lookup <https://docs.x.com/x-api/users/get-user-by-username>`_ and
`user posts <https://docs.x.com/x-api/users/get-posts>`_. Account access and
provider history limits determine how much history can be fetched.

The first update imports available timeline history. Later updates use the
account-specific ``live_since_id`` checkpoint as ``since_id`` and follow
pagination tokens. The checkpoint covers every validated fetched post, including
filtered-out replies and quotes. It advances atomically with the import and
last-success timestamp only after every page and post succeeds. Failures leave
it unchanged, and successful empty responses preserve its previous value.
Without a checkpoint, a live update requests available history even if offline
imports or an earlier version have populated stored posts; idempotent insertion
prevents duplicates. Offline backfills never advance the live checkpoint. Reposts
are always excluded. Replies are excluded by default and quotes included;
change the options above to filter future imports. Filtering does not remove
already stored posts. To backfill posts previously excluded by filtering,
use a local file import.

Updates have a 15-second timeout per request and a maximum of 100 timeline
pages per attempt. HTTP errors, rate limits, malformed data, repeated cursors,
and partial responses abort the whole import, leaving stored posts and the
last-success timestamp unchanged. No automatic retries are made. Retry after
the local cooldown and any provider rate limit has expired. The attempt time
is persisted even on failure. The cooldown defaults to 300 seconds (minimum
one second). A process lock and persisted 30-minute synchronization lease reject
concurrent synchronizations, including file imports. A lease abandoned by a
crashed process expires automatically. Provider pages are fetched before the
short import transaction, so network requests hold no primary-database write
lock. Imports still commit atomically; failures release their lease. Other bot
features can read and write their own tables during provider requests.

Offline JSON and CSV imports
----------------------------

Pass the same configuration YAML used by the bot::

    python -m pmxbot.tendie posts.json --config /path/to/config.yaml
    python -m pmxbot.tendie posts.csv --config /path/to/config.yaml

The importer uses the same ``database``, ``tendie_author``, cooldown, and filtering
settings as the command, including ``!env`` references. Run it with the same
working directory for relative database paths. Alternatively, explicitly pass
the primary database URI and source account::

    python -m pmxbot.tendie posts.json --database sqlite:/path/to/pmxbot.sqlite --author ExampleAccount

Exactly one of ``--config`` and ``--database`` is required. Neither path creates
a separate default collection database.

JSON is an array of post objects::

    [
      {
        "id": "1234567890123456789",
        "text": "Example stored post",
        "author": "ExampleAccount",
        "created_at": "2026-10-17T12:00:00Z"
      }
    ]

CSV uses these same column names::

    id,text,author,created_at
    1234567890123456789,Example stored post,ExampleAccount,2026-10-17T12:00:00Z

Both formats use UTF-8. IDs must be decimal strings (up to 30 digits).
The author must match the configured source account (case insensitive, optional
``@``). ``--author`` overrides the source in the configuration;
``TENDIE_AUTHOR`` provides the environment fallback.
Creation timestamps must be ISO 8601 with an explicit timezone. URLs are
constructed from the validated post ID and author; supplied URLs and import
timestamps are ignored. Use normal CSV quoting for commas and newlines.

An optional ``referenced_tweets`` field uses X v2 reference objects, for example
``[{"type": "replied_to", "id": "123"}]``. In CSV this field is a JSON string
inside a quoted CSV cell. Reference types ``retweeted``, ``replied_to``, and
``quoted`` support the same filtering as live imports. When reference metadata
is omitted, the row is treated as an original post. Prepare files accordingly.
Use ``--include-replies`` and/or ``--exclude-quotes`` to change import filtering.

File imports can backfill older IDs and are idempotent; they share the durable
cooldown and success metadata with live updates, but never change the live
checkpoint, even when their post IDs are newer than stored live posts. Invalid
files roll back the
entire import and exit unsuccessfully. IRC users cannot supply file paths.

Adding a provider
-----------------

``Collection.sync(provider, ...)`` accepts any object implementing
``posts(since_id)``. It must yield dictionaries with ``id``, ``text``, ``author``
(username), ``created_at``, and optional ``referenced_tweets``. Live providers
must be imported with ``Collection.sync(provider, live=True, ...)`` and should
honor the exclusive ``since_id`` lower bound. The default ``live=False`` is
for offline backfills; archival providers may ignore ``since_id`` and must not
opt into live checkpoint advancement. Raise an exception on any incomplete result, including errors
received alongside usable posts. The collection validates and filters rows,
constructs URLs, inserts unique IDs, and commits data and success metadata
only when the iterator finishes successfully. Provider iteration runs outside
the database write transaction; collected posts are validated and inserted
inside one short transaction.
