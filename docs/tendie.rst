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

``!tendie update`` directs administrators to the authenticated website. It never
starts synchronization from IRC. When ``albums_url`` is an absolute HTTP(S)
public application URL, the response links to ``/admin/tendies`` beneath it.
Otherwise, the response explains that administrators must use the website
without guessing a hostname. No IRC services account or account-tag support is
required.

Random, latest, days, and status requests make no network requests. Post text
is HTML-unescaped, stripped of IRC control characters, collapsed to a single
line, and truncated on UTF-8 boundaries to fit a 400-byte response including
its complete source URL.

Configuration
-------------

The example ``config.yaml`` includes these settings::

    database: sqlite:pmxbot.sqlite
    tendie_author: !env TENDIE_AUTHOR
    web_base: /bot
    albums_url: https://bot.example/bot
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

Administrators synchronize at ``/admin/tendies`` on the existing authenticated
website (beneath ``web_base``, if mounted in a subdirectory). The page shows the
configured source account, stored-post count, full-completion timestamps, and
saved/imported counts for the most recent run, including incomplete work.
Sign in as a local administrator first; normal
users cannot access the page or its polling endpoint. Disabled users and users
required to change their password cannot start updates or view protected status.
Authorization is rechecked on every request using the existing canonical users
and session policy.

The buttons submit CSRF-protected POSTs to ``/admin/tendies``. GET and HEAD
requests only display information; they never queue or start updates. The
administrator-only ``/admin/tendie_status`` endpoint returns current summary and
job status for polling. A normal form submission redirects back to the page,
which displays completion or a safe error and polls while the job is active.
JavaScript is optional: reload the page to check results. Tokens and provider
response details are never included in the page, status JSON, or error messages.

Set ``albums_url`` to the public application root, including its mount path,
for example ``https://bot.example/bot``. It supplies both album links and the
IRC link to the administration page; it grants no access. ``web_base`` is only
the application mount path, such as ``/bot``, and cannot supply a public hostname
or scheme. Administrator links reject URLs containing credentials, whitespace,
query strings, or fragments. Existing album-link fallbacks are unchanged.
The ``!config`` IRC command has been removed entirely. IRC users cannot mutate
runtime configuration, including ``albums_url`` or the primary ``database``
setting. Edit trusted configuration and restart to change settings.

The viewer manages one bounded tendie executor using the same CherryPy plugin
lifecycle and thread-pool conventions as generation jobs. Tendie jobs use the
existing account-scoped synchronization metadata rather than image/album job
records. Provider requests and imports run outside HTTP request threads, with
worker-owned SQLite connections. Queued/running status and safe completion
messages persist in the primary database. Duplicate requests are rejected,
including requests reaching another web process. Abandoned job claims expire
after 30 minutes, like synchronization leases; GET does not mutate stale claims.
The executor drains active work when the viewer stops. The local import utility
remains a trusted, synchronous operator tool.

Tendie uses the existing primary ``database`` setting and pmxbot's
``SQLiteStorage`` connection and schema-initialization conventions. Opening
the collection additively creates ``tendie_posts``, ``tendie_sync``,
``tendie_runs``, ``tendie_pages``, and their dedicated indexes in that database;
unrelated tables are left untouched. Existing posts and metadata are retained.
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

Durable live synchronization and recovery
-----------------------------------------

**Update tendies (X API)** is a paid action. It resumes the account's unfinished
run, replaying saved clean pages first, then requesting only the failed or next
unfetched page. A new run begins only when there is no unfinished run. The user
ID, original ``since_id``, request parameters, pagination position, filtering
policy, numeric high/low IDs, and completion state are durable. The first run
has no lower bound and bootstraps available history; subsequent runs use the
last completed ``live_since_id``. Offline posts never substitute for this bound.

Use **New pages to request per update** to start with just **1 page** (the website
default). Check the saved/imported counts and result, then choose a larger limit
for the next update, up to **100**. The website remembers the last submitted
choice. Each submission gets its own budget; it does not change the saved run's
source, filters, bounds, or pagination parameters. Explicit range recovery uses
the same selected limit.

Reaching the limit pauses normally with progress saved, rather than reporting
an API failure. The run stays incomplete and its live checkpoint does not advance
until the remaining range finishes. Update again after the configured cooldown
to resume at the next position. Saved-page replay does not use the new-page
budget and local replay remains independent of it. A page contains up to 100
posts; this is not an HTTP-request budget. Hydration retries can make up to four
requests for the same page, and the initial account lookup is separate.

Each successfully decoded timeline response is committed to ``tendie_pages``
before validating it, retrying it, or requesting another page. This includes
HTTP-error bodies and HTTP 200 partial-error responses, with their usable data,
error metadata, status, and request parameters. Authorization headers are never
saved; reflected bearer tokens are redacted. Raw responses are not exposed by
the website or IRC. Keep the database and its backups private.

Each clean page is validated and imported in its own atomic transaction with
the applied-page position and next cursor. Invalid pages roll back only that
page. Posts from earlier committed pages remain available to public reads.
IDs remain unique and idempotent; author checks, timestamps, trusted source URLs,
and IRC formatting still apply. Reposts are excluded; replies default to
excluded and quotes to included. Runs retain their initial filtering options
across resumes, even if configuration changes. Partial-error pages are quarantined
in full, including posts without reference metadata; they are never silently
treated as original posts. A later clean response can resolve the same position.

The live checkpoint covers validated filtered-out posts as well as imported
ones, but advances only when the entire run's requested range is processed.
Newest-first bootstrap/backfill progress stays separate, so an early high ID
cannot skip older unfinished pages. The checkpoint, final page batch, run
completion, ``last_live_success``, and ``last_success`` commit together. Partial
progress updates the run's progress time and counts, never these full-completion
timestamps. Offline file imports can still update ``last_success``; the page
shows the last complete live synchronization separately.

**Replay saved pages locally (no X requests)** is a local-only action, available
even without a bearer token or credits. It processes saved clean pages left
unapplied by a crash, safely skips applied positions, and stops at missing or
partial-error responses. It cannot invent missing ``referenced_tweets`` metadata.
The same operation is available to trusted shell operators::

    python -m pmxbot.tendie --recover --config /path/to/config.yaml

This command never contacts X. Run filtering options take precedence over CLI
filter switches during recovery. The administrator page reports saved responses,
staged post copies (including retry duplicates), newly imported posts in the run,
processed pages, and the next unresolved page position. The remaining unfetched
page count is unknown. Safe results state when a run is incomplete and explain
which action is local and which spends API credits. Reload without JavaScript
or use authenticated status polling to see progress.

Pagination tokens can expire. HTTP 400/410 while using a saved token marks it
rejected and stops normal updates without further X requests. This may also
indicate another invalid request parameter; inspect trusted staging if needed.
**Recover older unfinished range (X API)** explicitly authorizes a new request
without the rejected token, using exclusive ``until_id`` equal to the oldest
fully validated post in the contiguous processed prefix, and the original
``since_id`` lower bound. Partial/invalid pages never supply that boundary.
Previously completed pages are not fetched again and their high-water ID stays
pending until the remaining older range finishes. The original responses remain
in staging. There is no automatic full-timeline restart. If there is no validated
boundary, this action is unavailable; the run stays incomplete for operator
investigation. Range recovery depends on X supporting these bounds and exposing
the requested history. Completion describes only the history X exposes, not
deleted posts or data outside provider history/access limits. Provider errors
leave the range incomplete rather than advancing its checkpoint.

Requests time out after 15 seconds; an action processes at most 100 page
positions, requesting at most the selected number of new pages before pausing
with resumable progress. Trusted Python callers can pass ``page_limit`` to
``synchronize`` or ``Collection.sync`` (default 100); the website defaults to 1.
Only responses whose errors
are exclusively ``field-hydration-failure`` retry the same parameters/token,
after 1, 2, and 4 seconds (at most four requests per position per action).
Every decoded retry response is saved first. Other errors, credit failures,
rate limits, malformed data, and repeated cursors stop the action. No retry
restarts the timeline. Explicit later paid updates may retry unresolved pages
again, so review the status before spending more credits.

The cooldown defaults to 300 seconds (minimum one second) and paid attempts
remain recorded on failure. Local replay leaves this paid-request cooldown
unchanged. A process lock and durable 30-minute synchronization lease reject
concurrent work, including offline imports; web job claims also prevent duplicate
requests across processes. Ownership is checked before requests and transactions,
and active work refreshes the lease. Expired leases can be reclaimed. Network
requests hold no SQLite write lock. A paid response arriving after lease loss
is still staged, but the old worker cannot import or advance progress.

Staging is retained without automatic deletion; account/run scoping preserves
it when switching accounts. Back up the primary database, including the journal.
A disk/commit failure can prevent saving the response just received; the action
then stops without advancing or fetching the next page. Undecodable responses
have no usable JSON page to replay. Responses discarded before this change have
no saved local copy and cannot be recovered by this feature.

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
opt into live checkpoint advancement. Raise an exception on any incomplete
result, including errors received alongside usable posts. The generic iterator
contract remains atomic: the collection validates and filters rows, constructs
URLs, inserts unique IDs, and commits data and success metadata only when that
iterator finishes successfully. This preserves offline import semantics.

The built-in ``XProvider`` takes the durable page path instead: ``LiveRun``
supplies its response-journaling and lease callbacks to ``XProvider.get()``,
then validates and applies each saved page in a separate transaction. Its
``posts`` iterator is not used for live synchronization. Additional paid
providers need an equivalent page callback integration before using them for
durable live work; the generic iterator alone cannot preserve partial provider
responses.
