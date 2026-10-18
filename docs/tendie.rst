Stored tendies
==============

``!tendie`` returns a random locally stored post from the configured X account,
including its original X.com URL. All IRC users can use these subcommands:

* ``!tendie latest`` returns the newest post by original creation time.
* ``!tendie days`` reports complete elapsed days since that post was created,
  rounded down using timezone-aware UTC timestamps, with its URL. Import time
  has no effect. An empty collection or malformed creation timestamp produces
  an explanatory response. Future creation times report zero days.
* ``!tendie status`` reports the number stored and the last successful import
  or live synchronization, in UTC (or ``never``).
* ``!tendie update`` fetches newer posts through the official X API and reports
  the number inserted. Existing IDs are never overwritten or counted twice.

Random, latest, days, and status requests make no network requests. Post text
is HTML-unescaped, stripped of IRC control characters, collapsed to a single
line, and truncated on UTF-8 boundaries to fit a 400-byte response including
its complete source URL.

Configuration
-------------

The example ``config.yaml`` includes these settings::

    tendie_database: tendie.sqlite
    tendie_author: !env TENDIE_AUTHOR
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

Use a writable local SQLite file, independent of the main pmxbot database
(including when pmxbot uses MongoDB). Relative paths resolve from the process
working directory. Back up this database with SQLite's backup facilities;
WAL mode may also create ``-wal`` and ``-shm`` files while it is open.

Set ``X_BEARER_TOKEN`` in the bot's environment, or supply ``x_bearer_token``
through private configuration. Never commit credentials. The configuration
value takes precedence and is redacted from startup logs. Without credentials,
reads and file imports still work; update explains how to configure access.
The token needs access to the official X v2 user lookup and user posts endpoints:
`user lookup <https://docs.x.com/x-api/users/get-user-by-username>`_ and
`user posts <https://docs.x.com/x-api/users/get-posts>`_. Account access and
provider history limits determine how much history can be fetched.

The first update imports available timeline history. Later updates use the
largest stored post ID as ``since_id`` and follow pagination tokens. Reposts
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
one second). A process lock and SQLite's writer lock reject concurrent
synchronizations, including file imports, while WAL permits collection reads.
No database transaction or synchronization lock remains after a failed import.

Offline JSON and CSV imports
----------------------------

Run from the same environment as pmxbot, using the same database path::

    python -m pmxbot.tendie posts.json --author ExampleAccount --database /path/to/tendie.sqlite
    python -m pmxbot.tendie posts.csv --author ExampleAccount --database /path/to/tendie.sqlite

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
``@``). For the CLI, pass ``--author`` or set ``TENDIE_AUTHOR``; the standalone
CLI does not load ``config.yaml``.
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
cooldown and success metadata with live updates. Invalid files roll back the
entire import and exit unsuccessfully. IRC users cannot supply file paths.

Adding a provider
-----------------

``Collection.sync(provider, ...)`` accepts any object implementing
``posts(since_id)``. It must yield dictionaries with ``id``, ``text``, ``author``
(username), ``created_at``, and optional ``referenced_tweets``. Live providers
should honor the exclusive ``since_id`` lower bound; archival providers may
ignore it. Raise an exception on any incomplete result, including errors
received alongside usable posts. The collection validates and filters rows,
constructs URLs, inserts unique IDs, and commits data and success metadata
only when the iterator finishes successfully.
