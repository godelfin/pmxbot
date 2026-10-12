Production deployment
=====================

The production Debian server uses ``deploy/deploy-ircbot``. It updates
``/home/ircbot/pmxbot`` to ``origin/main``, installs ``.[viewer]`` with
``constraints.txt`` into ``/home/ircbot/venv``, checks dependency consistency,
and restarts ``ircbot.service`` and ``pmxbotweb.service``. The viewer extra supplies CherryPy and Jinja2.
An install or dependency-check failure prevents the restart. Deployments are
serialized with a nonblocking lock; a concurrent invocation fails.

The GitHub Actions deployment workflow connects as ``deploy`` without a remote
command. Configure its ``DEPLOY_HOST``, ``DEPLOY_SSH_KEY``, and
``DEPLOY_KNOWN_HOSTS`` secrets using the production host and its verified host
key. A push to ``main`` triggers deployment.

Ownership and bootstrap
-----------------------

An administrator provisions the ``deploy`` and ``ircbot`` accounts, Git,
Python with venv support, pip, sudo, and util-linux (for ``flock``).
``deploy`` owns the checkout and virtual environment; ``ircbot`` runs the
service and owns runtime data. Keeping the existing paths under
``/home/ircbot`` does not imply that ``ircbot`` owns the code.

For a new installation, provision the directories and environment as root::

    install -d -o root -g root -m 0755 /home/deploy
    install -d -o root -g root -m 0755 /home/ircbot
    install -o deploy -g deploy -m 0644 /dev/null /home/deploy/.deploy-ircbot.lock
    install -d -o deploy -g deploy -m 0755 /home/ircbot/pmxbot
    install -d -o deploy -g deploy -m 0755 /home/ircbot/venv
    sudo -u deploy git clone https://github.com/godelfin/pmxbot.git /home/ircbot/pmxbot
    sudo -u deploy python3 -m venv /home/ircbot/venv
    install -d -o ircbot -g ircbot -m 0700 /home/ircbot/data

Ensure ``deploy`` can traverse ``/home/ircbot`` and ``ircbot`` can read and
execute the checkout and venv. For an existing server, change ownership of
only those two trees to ``deploy:deploy`` after checking for runtime files.
Move databases, images, logs, and other mutable data to a separate
``ircbot``-owned directory, such as ``/home/ircbot/data``. Point the service's
working directory and application configuration at that runtime directory.
Keep both home directories root-owned so neither account can replace the
checkout, venv, or shell startup files through a writable parent directory.
Do not recursively change ownership of all of ``/home/ircbot``. Provision the
lock file only during bootstrap, never replace it while a deployment is running.

The deployment hard reset discards tracked local changes. Keep production
configuration and secrets outside the checkout. An administrator should verify
that ``origin`` is ``https://github.com/godelfin/pmxbot.git``. Dependencies
remain subject to the repository's constraints; they are not a complete lock
file. Failed installs can leave a partially updated venv, so inspect deployment
output and repair the environment before retrying.

Restricted SSH boundary
-----------------------

Install the script from an administrator-reviewed revision, not automatically
from the deployment checkout. For example, in a separate trusted review
checkout, inspect the diff and export the exact approved commit::

    git show <approved-commit>:deploy/deploy-ircbot > /tmp/deploy-ircbot.reviewed
    bash -n /tmp/deploy-ircbot.reviewed

Transfer that reviewed file to the server through an administrator account.
As root, install it from an administrator-owned staging directory::

    install -o root -g root -m 0755 /root/deploy-ircbot.reviewed /usr/local/bin/deploy-ircbot

Repeat this review and installation explicitly for script updates. Never
symlink the installed command to the checkout, execute the checkout's script
from a wrapper, or let ``deploy`` replace the installed command or its parent
directories. Installing script changes as part of deployment would let a
repository change alter the SSH entry point without administrator review.

Use a root-owned authorized-keys file outside ``deploy``'s writable home,
such as ``/etc/ssh/authorized_keys/deploy``, and configure ``AuthorizedKeysFile``
for that account in sshd. Its deployment key entry should have these options
before the public key::

    restrict,command="/usr/local/bin/deploy-ircbot" ssh-ed25519 <deployment-public-key>

Validate the SSH configuration before reloading sshd and retain an administrator
session while testing. Disable other login methods for ``deploy``. The
``restrict`` option disables forwarding, PTY allocation, and user rc execution;
the script also rejects arguments and nonempty ``SSH_ORIGINAL_COMMAND``.
The GitHub key should authorize only this forced command.
Keep any shell startup files for ``deploy`` absent or root-owned and reviewed;
sshd executes forced commands through the account's shell. Do not allow
user-controlled SSH environment variables (including ``BASH_ENV``, ``ENV``,
or ``PATH``), ``PermitUserEnvironment``, or user rc files to introduce an
alternate execution path. Keep ``deploy``'s home root-owned as above.

Using ``visudo``, grant only this exact restart command in a root-owned sudoers
file (verify the systemctl path on the server)::

    deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart ircbot.service pmxbotweb.service

Do not grant sudo for shells, pip, Git, file installation, or arbitrary systemctl
commands. Repository code and dependency build hooks execute as ``deploy``;
the service executes as ``ircbot``. Code updates are therefore trusted to run
with those accounts' privileges, but cannot modify the root-owned SSH policy,
deployment entry point, or service unit.

Server-local configuration
--------------------------

These files intentionally remain outside version control:

* The root-owned ``ircbot.service`` systemd unit and any drop-ins. Set
  ``User=ircbot``, reference the venv executables, and use a runtime directory
  outside the checkout. Enable the service during bootstrap.
* Environment files, application configuration, credentials, and secrets.
  Limit read access to the service account and administrators as appropriate.
* SSH authorization, sshd account restrictions, host keys, and sudoers policy.
* Runtime databases, images, logs, backups, and the deployment lock file.

After provisioning, invoke ``/usr/local/bin/deploy-ircbot`` as ``deploy`` or
connect with the restricted SSH key without a remote command. Inspect
``systemctl status ircbot.service`` and ``journalctl -u ircbot.service`` as an
administrator, then verify the bot and web viewer. To recover a bad release,
revert it on ``main`` and redeploy, or have an administrator explicitly install
a known-good revision and its dependencies. A hard reset does not migrate or
restore runtime data; manage those backups separately.

Production services
-------------------

IRC bot
~~~~~~~

``ircbot.service`` runs the IRC bot as the ``ircbot`` user.

* Unit: ``/etc/systemd/system/ircbot.service``
* Environment: ``/etc/ircbot.env``
* Launcher: ``/home/ircbot/run``
* Configuration: ``/home/ircbot/data/config.yaml``
* Python environment: ``/home/ircbot/venv``

Web viewer
~~~~~~~~~~

``pmxbotweb.service`` runs the web viewer as the ``ircbot`` user.

* Unit: ``/etc/systemd/system/pmxbotweb.service``
* Working directory: ``/home/ircbot/pmxbot``
* Executable: ``/home/ircbot/venv/bin/pmxbotweb``
* Configuration: ``/home/ircbot/data/config.yaml``
* Port: ``8080``

The public request path is::

    Browser -> Cloudflare -> Caddy -> pmxbotweb:8080


Service operations
------------------

Check service status::

    sudo systemctl status ircbot
    sudo systemctl status pmxbotweb

Start services::

    sudo systemctl start ircbot
    sudo systemctl start pmxbotweb

Stop services::

    sudo systemctl stop ircbot
    sudo systemctl stop pmxbotweb

Restart services::

    sudo systemctl restart ircbot
    sudo systemctl restart pmxbotweb

Follow logs::

    sudo journalctl -u ircbot -f
    sudo journalctl -u pmxbotweb -f

Show the last 100 lines::

    sudo journalctl -u ircbot -n 100 --no-pager
    sudo journalctl -u pmxbotweb -n 100 --no-pager

Show logs from the last seven days::

    sudo journalctl -u ircbot --since "7 days ago"
    sudo journalctl -u pmxbotweb --since "7 days ago"

Web authentication
------------------

Accounts can be provisioned by administrators or requested through the web
registration form. Registration requires matching passwords of 12–1024 characters
with uppercase and lowercase letters and a number. New web accounts are disabled
until an administrator enables them; registration never signs the applicant in.
For example, use ``store.update(store.get_by_username('Alice').id, enabled=True)``
inside the administrator session below after approving the account.

To provision an account directly in the main SQLite database, use an
administrator's interactive Python session on the server (using the service
venv). Prompt for the initial password rather than putting it in a shell command,
configuration file, or source code::

    from contextlib import closing
    from getpass import getpass
    from pmxbot.users import UserStore

    with closing(UserStore('sqlite:/home/ircbot/data/pmxbot.sqlite')) as store:
        user = store.create('Alice', password=getpass('Initial password: '))
        # For an existing account:
        # store.set_password(store.get_by_username('Alice').id, getpass())

Use the same ``database`` URI in the bot, viewer, and provisioning session.
See :doc:`users` for account naming, password storage, and disabling accounts.
The web registration form creates disabled accounts; there is no password reset
UI. Administrators should assign strong, unique passwords and communicate them
through a trusted private channel.

The viewer enables CherryPy RAM sessions in its single server process. Only
``users.id`` is stored as authentication data; the browser receives an opaque
session identifier. Successful login deletes the previous session and rotates
its identifier. Logout is a CSRF-protected POST that clears the session, rotates
its identifier, and expires the cookie. Sessions expire after 30 minutes of
inactivity, and server restarts log everyone out. Each identity lookup re-reads
the account; disabled or deleted users lose authorization on the next lookup.
Password replacement alone does not revoke existing sessions; disable the account
and restart the viewer if all existing sessions must be revoked immediately.

Cookies are host-only, ``HttpOnly``, ``SameSite=Lax``, scoped to ``web_base``
(or ``/``), and ``Secure`` by default. Keep ``web_session_secure: true`` in
production, including when Caddy terminates TLS and forwards HTTP internally.
The viewer deliberately does not infer cookie security from forwarded headers.
For isolated local HTTP development only, explicitly set
``web_session_secure: false``. A quoted string such as ``"false"`` is rejected.
The following production configuration works with a prefixed mount::

    database: sqlite:/home/ircbot/data/pmxbot.sqlite
    web_base: /bot
    web_session_secure: true

Serve the public site exclusively over HTTPS. At Cloudflare, use verified TLS to
Caddy; Caddy must redirect public HTTP to HTTPS and preserve the ``/bot`` prefix
when proxying. Bind CherryPy to loopback (``web_host: 127.0.0.1``), or firewall
its port so only the trusted proxy can reach it. Verify in browser developer tools
that the ``pmxbot_session`` cookie has Secure, HttpOnly, SameSite=Lax, and the
expected Path after signing in. A successful login redirects to a relative
same-application path, so the viewer does not need to trust ``X-Forwarded-Host``
or ``X-Forwarded-Proto``. Configure HSTS at the public HTTPS proxy. Never log
request bodies, cookies, or authorization headers at the proxy, viewer, or APM
layer. Credentials must be submitted in form bodies; CherryPy authentication
access logs omit query strings even for rejected requests. Configure the proxy
to omit query strings on login/logout access logs as well.

All existing browsing routes remain public. A shared account navigation fragment
shows the active username and a POST logout form. Responses containing session
state use ``Cache-Control: no-store``; Cloudflare/Caddy must honor it and must not
cache HTML with ``Set-Cookie``. CSRF tokens use a random process-local HMAC key
and the session identifier; they rotate with the identifier and are never stored
in cookies or the database. The key needs no deployment secret or persistent
file, and disappears with RAM sessions on restart.

The viewer allows at most 30 combined login and registration attempts per rolling minute across all clients,
including successes, before returning 429 with Retry-After. This deliberately
bounds password derivation work without trusting forwarded IP addresses; shared
traffic can exhaust the allowance and temporarily deny other users login. Public
browsing remains available. Add per-client limiting at the trusted proxy if
needed. RAM sessions, the CSRF key, and the throttle are process-local: run one
viewer process. Multiple workers or replicas require a reviewed shared session
and rate-limit design before deployment.

Protected endpoints, including variation submission, call
``pmxbot.web.auth.require_authenticated_user()`` for a canonical User (including
``may_pair_irc``), or receive HTTP 401. ``current_user()`` returns a User or None
for public rendering. Future form mutations must accept POST only and call
``require_csrf(token)`` in addition to the authentication helper. This feature
does not pair IRC sessions or grant access based on IRC attribution strings.

Background artwork generation
-----------------------------

Album variation POSTs validate the six creative properties, persist a job in the
main SQLite database, and return JSON (202) to JavaScript on the album page.
A gray Artwork history thumbnail shows a loading spinner while JavaScript polls
the job every five seconds. Completion reloads the album with the generated
image selected; failures show a safe error inline. Returning to the album resumes
polling its pending jobs. No provider or hosting call runs in the request.
Without JavaScript, submission redirects (303) to
``web_base/jobs/<random-job-id>``. This stable bookmarkable page refreshes every
five seconds while pending; terminal pages link to artwork or show a safe error.
The jobs list has no account or album navigation link; it remains accessible
directly for troubleshooting. ``web_base/jobs`` lists the latest 100 jobs for the signed-in account and the
current anonymous browser session. Authenticated job URLs require their owner
to sign in, including after a viewer restart. Anonymous URLs are bearer links:
anyone with the unguessable URL can see status and public album/image links,
but never the submitted properties, credentials, attribution or provider errors.
Bookmark anonymous URLs before leaving; their session list disappears on a
viewer restart. These bearer links remain compatible with legacy anonymous jobs.
Album browsing remains public, but new web variations require an enabled,
signed-in account. The server rejects signed-out POST requests before input
preparation or enqueueing; a CSRF token alone does not authorize generation.
Signed-out visitors see a disabled generation button and a sign-in link that
returns to the selected album/image. Expired or revoked accounts must sign in
again. IRC nickname/channel attribution and worker routing are unaffected.

Jobs live in ``generation_jobs``, separate from ``image_cache``. Each stores a
UUID, album/source IDs, immutable structured inputs and backend/settings JSON,
canonical user ID and attribution when present, anonymous session ownership,
UTC creation/start/completion timestamps, status, resulting image ID or safe
error. The lifecycle is ``queued -> running -> succeeded/failed``. Only success
sets the result image ID. Upload failure can leave immutable local artwork
through the existing generation service, but the job remains failed; an
administrator can use ``ImageCache.retry_upload(image_id)`` to repair hosting
without another provider call. Failed jobs never masquerade as ready artwork.

Optional shared standalone worker
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Both switches default to false, so upgrading preserves current behavior:
web variations run in CherryPy, and IRC ``!music`` uses its existing in-memory
queue. To trial one shared worker, use the same configuration in all services::

    images_enabled: true
    irc_generation_standalone: true
    web_generation_standalone: true
    generation_worker_concurrency: 1
    database: sqlite:////home/ircbot/data/pmxbot.sqlite
    images_directory: /home/ircbot/data/images

``irc_generation_standalone`` routes new ``!music`` requests to SQLite instead
of the bot's thread queue. ``!music <album ID>`` remains a cached lookup.
``web_generation_standalone``
disables CherryPy's executor and routes variation execution to the standalone
worker. The album UI, status endpoints, ownership and polling are unchanged.
Neither switch disables image generation; each selects where it executes.

The four supported configurations are:

========================== ========================== ================================
IRC standalone             Web standalone             Execution
========================== ========================== ================================
false                      false                      IRC local queue; CherryPy variations
true                       false                      Worker IRC; CherryPy variations
false                      true                       IRC local queue; worker variations
true                       true                       Worker handles both job types
========================== ========================== ================================

Install the viewer extra (``.[viewer]``) for the worker's runtime dependencies.
Run ``pmxbot-generation-worker /home/ircbot/data/config.yaml`` manually, or install
the reviewed ``deploy/pmxbot-generation-worker.service`` into
``/etc/systemd/system/`` as administrator. The sample unit runs as ``ircbot``
with the existing virtual environment and environment file. After reviewing::

    sudo systemctl daemon-reload
    sudo systemctl enable --now pmxbot-generation-worker.service

Restart the bot and viewer after changing switches. The worker must receive the
same switches, absolute database/image paths, provider settings and hosting
credentials. It refuses to start when neither switch is enabled or images are
disabled. Watch ``journalctl -u pmxbot-generation-worker -f``. Submit ``!music``
and a web variation: both return immediately; IRC posts a finished result to
its original channel and the browser selects its new image after polling.

The deployment script restarts the optional worker only when its unit is enabled.
Add this exact additional sudoers permission through the existing administrator
review procedure if using it::

    deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart pmxbot-generation-worker.service

No unit or privileged deployment script is automatically installed from the
checkout. The default bot/viewer deployment needs no extra service.

For rollback, stop the worker first, change switches to false, and restart the
bot/viewer. Drain IRC jobs before disabling its switch: persisted IRC jobs wait
until an IRC-enabled worker returns; terminal notifications still drain from the
bot. Queued web jobs can be processed by CherryPy after rollback. Stop old
executors before transferring a job type; overlapping owners fail startup
rather than steal work. Changing worker settings requires restarting it.

CLI queue inspection
~~~~~~~~~~~~~~~~~~~~

``pmxbot-jobs`` inspects the database without claiming jobs, running recovery,
creating storage, migrating the schema, or contacting providers. It can run
alongside the worker. After deploying the updated package, use the runtime
account and existing SQLite path::

    sudo -u ircbot /home/ircbot/venv/bin/pmxbot-jobs --database /home/ircbot/data/pmxbot.sqlite

Alternatively, supply ``/home/ircbot/data/config.yaml`` as the positional argument
instead of ``--database``. Database resolution uses the same conventions as image
storage. Direct database selection needs no configuration or provider credentials.

The default view reports total queued/running/succeeded/failed counts and the
latest 50 jobs, with job ID prefix, status, kind, album, saved API and requester.
Counts always cover the whole database; filters apply to the displayed jobs::

    pmxbot-jobs --database /home/ircbot/data/pmxbot.sqlite --status pending
    pmxbot-jobs --database /home/ircbot/data/pmxbot.sqlite --status failed --limit 20
    pmxbot-jobs --database /home/ircbot/data/pmxbot.sqlite --kind music
    pmxbot-jobs --database /home/ircbot/data/pmxbot.sqlite --job <job-ID-or-prefix>
    pmxbot-jobs --database /home/ircbot/data/pmxbot.sqlite --json

Pending jobs are listed oldest first. Other lists show the newest first.
``--limit`` accepts 1 through 1000. ``--job`` accepts a full ID or a unique
8–32-character hexadecimal prefix and shows the source/result IDs, ownership,
channel, UTC timestamps, delivery state, safe error and saved generation settings.
A queued job's settings may differ from the configuration now on disk.
JSON output contains counts and selected job summaries/settings; it excludes
prompts, immutable input payloads, anonymous bearer tokens and credentials.

For a refreshing terminal view, Debian's ``watch`` can invoke the command::

    sudo -u ircbot watch -n 2 /home/ircbot/venv/bin/pmxbot-jobs --database /home/ircbot/data/pmxbot.sqlite --status pending

An existing database without a job table reports an empty queue. A missing or
unreadable database exits with an error and is never created. Job lookup failures
exit with status 1; invalid CLI arguments exit with status 2. This is an
administrator view across all owners, controlled by filesystem access.

Job schema and delivery
~~~~~~~~~~~~~~~~~~~~~~~

The existing JobStore gets an additive, transaction-serialized migration:
``kind`` defaults to ``variation`` for existing jobs; ``channel`` and
``delivered_at`` support IRC notifications. Music jobs snapshot the album's
artist/title, the exact IRC prompt and API settings at submission. They use
``source_image_id = 0`` to indicate an initial cover with no source. Credentials
and paths stay in service configuration. The existing generation service handles
both Images and Responses APIs; web Responses continuation remains unchanged.

IRC jobs retain the requesting nickname and channel, separately from web account
and anonymous session identities. Identical pending IRC requests reuse a job
within that nickname/channel. IRC notifications are claimed atomically from
SQLite, so a bot restart does not normally lose queued work or terminal results.
Delivery is at most once: a crash after claiming a notification but before
sending it to IRC can lose that notification. IRC has no transactional delivery
acknowledgment; album history still contains successful artwork. Run one bot
for this database/network; multiple IRC networks sharing it are unsupported.

Recovery and limits
~~~~~~~~~~~~~~~~~~~

The scheduler polls SQLite once per second and atomically claims queued jobs
using ``BEGIN IMMEDIATE``. ``generation_worker_concurrency`` defaults to 1 and
accepts integers 1 through 4. With both switches on, it is a shared IRC/web bound;
with mixed modes, separate executors and legacy IRC work have separate bounds.

An executor must hold the POSIX kernel file lock for every job type it owns:
``<database>.generation-worker.lock`` for variations and
``<database>.irc.generation-worker.lock`` for music. Locks are acquired in a
fixed order before recovery. This allows the IRC-only standalone worker and the
CherryPy variation executor to coexist, while excluding multiple owners of the
same type. Never delete lock files while any executor could be alive. Atomic
claims provide additional protection. Use one POSIX host, local SQLite and local
image files; network filesystems and distributed execution are unsupported.
Stop any older all-job worker before upgrading: it does not understand job types.

On startup, only abandoned ``running`` jobs of the executor's owned types become
failed with a safe message; queued jobs remain available. There are no automatic
job retries. SIGTERM/SIGINT stop new claims and drain active calls before releasing
locks. Both sample worker and viewer should have ``TimeoutStopSec=600``; Python
threads cannot forcibly cancel remote calls, and a forced kill is recovered as
a crash. A standalone scheduler/persistence failure exits with an error for
operator attention; the sample unit does not automatically restart it.

When web standalone is false, CherryPy's existing lifecycle starts/stops its
variation executor once. It retains locks after scheduler failure until explicit
shutdown/restart. When true, the viewer starts no executor and can restart
independently of generation. Existing RAM sessions still require one viewer
process; never start an in-process executor before forking WSGI workers.
Standalone operation isolates generation resources and lifetime from both
applications, although database contention remains shared. Worker downtime
leaves requests queued. With IRC standalone disabled, the bot's existing
generation threads remain outside the worker pool's concurrency limit.

Exactly-once execution across OpenAI and SQLite is impossible: the provider
may succeed before a crash and the response or completion may never be
persisted. An image may already be saved even though its job is subsequently
marked failed. Check album history and local artwork before manually submitting
another variation; that explicit new submission may incur another API charge.
The executor never automatically requeues a failed or abandoned job.

F34 (#39) is not merged. Exact inputs/settings/source requests already queued or
running are reused atomically within the same account or anonymous session.
Different owners remain separate to avoid revealing private jobs. There is no
new existing-artwork equivalence algorithm. F34's future shared lookup should
run after validation and before ``JobStore.enqueue``; its versioned generation
identity can replace the exact JSON comparison without changing job lifecycle
or executor provider logic. Completed and failed jobs are currently not reused.
There are no queue caps, per-user quotas, job expiration or automatic retries.
