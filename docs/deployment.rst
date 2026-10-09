Production deployment
=====================

The production Debian server uses ``deploy/deploy-ircbot``. It updates
``/home/ircbot/pmxbot`` to ``origin/main``, installs ``.[viewer]`` with
``constraints.txt`` into ``/home/ircbot/venv``, checks dependency consistency,
and restarts ``ircbot.service``. The viewer extra supplies CherryPy and Jinja2.
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

    deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart ircbot.service

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

Provision accounts in the main SQLite database before using web login. From an
administrator's interactive Python session on the server (using the service
venv), prompt for the initial password rather than putting it in a shell command,
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
There is no registration or password reset UI. Administrators should assign
strong, unique passwords and communicate them through a trusted private channel.

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

The viewer allows at most 30 login attempts per rolling minute across all clients,
including successes, before returning 429 with Retry-After. This deliberately
bounds password derivation work without trusting forwarded IP addresses; shared
traffic can exhaust the allowance and temporarily deny other users login. Public
browsing remains available. Add per-client limiting at the trusted proxy if
needed. RAM sessions, the CSRF key, and the throttle are process-local: run one
viewer process. Multiple workers or replicas require a reviewed shared session
and rate-limit design before deployment.

Future protected endpoints can call
``pmxbot.web.auth.require_authenticated_user()`` for a canonical User (including
``may_pair_irc``), or receive HTTP 401. ``current_user()`` returns a User or None
for public rendering. Future form mutations must accept POST only and call
``require_csrf(token)`` in addition to the authentication helper. This feature
does not pair IRC sessions or grant access based on IRC attribution strings.
