.. image:: https://img.shields.io/pypi/v/pmxbot.svg
   :target: https://pypi.org/project/pmxbot

.. image:: https://img.shields.io/pypi/pyversions/pmxbot.svg

.. image:: https://github.com/pmxbot/pmxbot/workflows/tests/badge.svg
   :target: https://github.com/pmxbot/pmxbot/actions?query=workflow%3A%22tests%22
   :alt: tests

.. image:: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/charliermarsh/ruff/main/assets/badge/v2.json
    :target: https://github.com/astral-sh/ruff
    :alt: Ruff

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg
   :target: https://github.com/psf/black
   :alt: Code style: Black

.. image:: https://readthedocs.org/projects/pmxbot/badge/?version=latest
   :target: https://pmxbot.readthedocs.io/en/latest/?badge=latest

.. image:: https://img.shields.io/badge/skeleton-2023-informational
   :target: https://blog.jaraco.com/skeleton

.. image:: https://tidelift.com/badges/package/pypi/pmxbot
   :target: https://tidelift.com/subscription/pkg/pypi-pmxbot?utm_source=pypi-pmxbot&utm_medium=readme

pmxbot is bot for IRC and Slack written in
`Python <https://python.org>`_. Originally built for internal use
at `YouGov <https://yougov.com/>`_,
it's been sanitized and set free upon the world. You can find out more details
on `the project website <https://github.com/pmxbot/pmxbot>`_.

Commands
========

pmxbot listens to commands prefixed by a '!'
If it's a command, it knows it will reply, take an action, etc.
It can search the web, store quotes you, track karma, make decisions,
and do just about anything else you could want. It stores logs and quotes
and karma in either a sqlite or MongoDB
database, and there's a web interface for reviewing the logs and karma.

Contains
========

pmxbot will respond to things you say if it detects words and phrases it's
been told to recognize. For example, mention sql on rails.

Requirements
============

`pmxbot` requires Python 3. It also requires a few python packages as defined
in setup.py. Some optional dependencies are installed with
`extras
<https://packaging.python.org/installing/#installing-setuptools-extras>`_:

- mongodb: Enable MongoDB persistence (instead of sqlite).
- irc: IRC bot client.
- slack: Slack bot client.
- viewer: Enable the web viewer application.

Testing
=======

`pmxbot` includes a test suite that does some functional tests written against
the Python IRC server and quite a few unit tests as well. Install
`tox <https://pypi.org/project/tox>`_ and run ``tox`` to invoke the tests.

Configuration
=============

Configuration is based on very easy YAML files. Check out config.yaml in the
source tree for an example.

Silent mode
-----------

Configure secret command keywords using environment variables::

    silent_mode_disable_command: !env SILENT_MODE_DISABLE_CMD
    silent_mode_enable_command: !env SILENT_MODE_ENABLE_CMD

Set those environment variables before starting the bot. If the disable
keyword is ``sleep-secret``, send ``!sleep-secret`` to suppress bot output;
send ``!`` followed by the enable keyword to restore output. Keywords are
case-sensitive and must match the entire message (surrounding whitespace is
ignored). You can send these controls privately to the bot. Neither control
produces a reply, appears in help, or is passed to the bot's message logger.
The values are redacted from startup configuration logging.

Silent mode starts off on every restart. A missing, unset, null, or empty
disable keyword leaves it off and provides no disable command. An empty
enable keyword provides no enable command; configure both to allow toggling
without restarting. Mode applies to the whole bot across all channels and
private replies, including scheduled output. Incoming messages continue to
be logged and commands still run, including their side effects. Suppressed
replies are discarded rather than queued or logged as sent messages. IRC
connection traffic and private logging notices continue normally.

Image generation
----------------

Enable album cover generation with ``!music`` using::

    images_enabled: true
    openai_api_key: !env OPENAI_API_KEY
    r2_endpoint_url: !env R2_ENDPOINT_URL
    r2_access_key_id: !env R2_ACCESS_KEY_ID
    r2_secret_access_key: !env R2_SECRET_ACCESS_KEY
    r2_bucket: !env R2_BUCKET
    r2_public_url: !env R2_PUBLIC_URL
    images_directory: images
    images_model: gpt-image-1
    images_size: 1024x1024
    images_quality: low

Set ``OPENAI_API_KEY`` and the five ``R2_*`` environment variables above,
then restart the bot. Install updated dependencies with ``pip install -e .``.
Create an R2 bucket and an R2 S3 access key with Object Read & Write permissions
scoped to that bucket. Set ``R2_ENDPOINT_URL`` to the S3 API endpoint shown by
Cloudflare, usually ``https://<ACCOUNT_ID>.r2.cloudflarestorage.com`` (use the
jurisdiction-specific endpoint if applicable). Set ``R2_BUCKET`` to the bucket
name, and use the Access Key ID and Secret Access Key for the credentials,
not a Cloudflare bearer API token. See `Cloudflare's boto3 guide
<https://developers.cloudflare.com/r2/examples/aws/boto3/>`_.

Connect a public custom domain to the bucket and set ``R2_PUBLIC_URL`` to its
HTTPS base URL, such as ``https://images.example.com``. For development, you
can enable and use the bucket's public ``r2.dev`` URL. The public URL is
separate from the authenticated S3 endpoint; the bot does not enable public
access or create buckets. See `R2 public buckets
<https://developers.cloudflare.com/r2/buckets/public-buckets/>`_.

OpenAI generation is billed to your API account; images are uploaded to R2
and the public link is returned to the requesting channel or private
conversation. Credentials are redacted from startup configuration logs.
The implementation follows the `OpenAI Images API
<https://developers.openai.com/api/docs/guides/image-generation>`_.
Use a GPT Image model supporting PNG output; model, size, and quality are
passed to the API. Requests run in a background worker, one at a time;
additional requests receive a busy response instead of being queued.

PNG files are saved atomically under ``images_directory`` (relative to the
bot's working directory, or an absolute path). The ``image_cache`` table uses
the bot's main ``database`` setting (default: ``sqlite:pmxbot.sqlite``), alongside
logs, quotes, and karma. Image caching requires a persistent SQLite main
database; MongoDB is not supported for this feature. The worker opens its own
connection to the same database file. Directories and the ``image_cache`` table
are created on the first request. ``images_directory`` controls only image
files, not the database location.

Cache keys hash the Unicode NFKC-normalized, case-folded prompt with collapsed
whitespace, plus the model, size, quality, and output format. Punctuation is
preserved. The original prompt is sent to OpenAI. Equivalent prompts reuse
the hosted URL across restarts without API calls; different generation
settings create separate entries. The table stores the original and normalized
prompts, absolute local filename, hosted URL, generation and host metadata
(R2 endpoint, bucket, object key, and ETag), requester, channel, timestamps,
hit count, and last upload error. R2 objects use the cache key plus ``.png``
as their name, have ``image/png`` content type, and no requested expiration.
Public URLs do not use expiring signatures. Keep the database private because
it contains prompts and requester information.

Existing ImgBB cache entries are moved to R2 when next requested, by uploading
the saved local image and updating the same SQLite row after a successful
upload. No new OpenAI call is needed if the local file exists; a missing file
is regenerated. Old ImgBB uploads are not deleted. Changing the R2 bucket or
endpoint similarly reuploads saved images on demand. Changing only the public
base URL updates cached links without uploading or generating again.

If uploading fails, the saved image and cache row remain. Repeat the same
prompt to retry only the upload. If that pending image file is missing, the
bot regenerates it. Cached hosted URLs are not checked for expiration or
external deletion; delete the corresponding SQLite row to generate a new
image. Files and rows are retained until manually removed. Use one bot process
per cache; simultaneous processes sharing a cache are not coordinated.

``!music`` selects a random band and album from the quote libraries, persists
that pair in separate ``artists`` and ``albums`` tables in the main SQLite
database, and immediately starts the shared image generation/cache/upload
worker. New albums save the randomly selected genre, format, and format
description. Selecting the same artist/title again (ignoring case, Unicode
normalization, and whitespace) reuses its album ID and original metadata.
The acknowledgement includes the selected metadata; the completed reply is
``#<album ID> <hosted URL>``. Empty quote libraries do not create albums.
``!music <album ID>`` (also ``!music #<album ID>``) returns the most recently
created cached image with a hosted URL linked to that album, without generating
or uploading an image. The reply includes the saved band, album title, format,
format description, and genre, followed by the album ID and image URL.
Unknown IDs and albums without a hosted cached image
return an explanatory message.

``pmxbot.music.MusicLibrary.create_album`` creates/selects a persistent album
without image generation or provider credentials. Separately,
``pmxbot.music.generate_album_image`` accepts a library, image cache, and album
ID, loads the saved metadata, and generates or reuses its image. Creation and
image attribution are stored separately. Failed generation/upload leaves the
album available for retry by ID; successful cache hits retain image attribution.
Album/image associations are stored in ``album_images``, keyed by
``(album_id, cache_key)``. An album can have multiple images, but each cached image
can be linked to only one album, enforced by a unique index on ``cache_key``.
Linking an image to another album raises ``sqlite3.IntegrityError``.
``get_album`` returns an ``images`` collection containing
cache keys instead of scalar image fields. Recording an image adds a link; repeats
are idempotent. Image attribution comes from ``image_cache.requested_by`` and
``image_cache.created_at``; links carry no attribution. Cache keys remain logical
references to the independently managed image cache. Unknown album IDs are
rejected when recording a link.

Completed music migrations are no longer run at startup. Obsolete scalar columns
in older ``albums`` tables are ignored; fresh databases omit them. Older branch
code must not write to a migrated database. There is no revision/version model, preferred
image, or semantic ordering of images. There are no new IRC commands or
delayed-generation workflows.

Album IDs identify artist/title pairs rather than individual rendered images.
Album and image IDs are independent. New album IDs follow the album sequence;
the retired ``music_image_ids`` table is no longer consulted at runtime.
Matching prompts still reuse the existing image cache.

By default it reads the ``band`` and ``album`` libraries. It respects custom
``band`` and ``album`` mappings in ``quote_libraries``. Enable those commands
with ``band: band`` and ``album: album`` under ``quote_libraries``, then populate
them with ``!band add: <band name>`` and ``!album add: <album title>``.
``music`` is a built-in command; use another name such as ``tunes`` for a
song quote library.


Historical music database upgrades
---------------------------------

The completed recovery and legacy-ID cleanup commands have been removed.
Databases must already have album/image associations in ``album_images`` and
image attribution in ``image_cache``. For an older database, back it up and first
use commit ``119c089`` (or merge ``88850f1``) for legacy import, unlinked image
recovery, and link-attribution cleanup. Commit ``36f3f89`` provides the explicit
``music_image_ids`` retirement command. Complete these upgrades before running
this version. Existing numeric album/image IDs and cached records remain valid.

Usage
=====

Once you've setup a config file, you just need to call ``pmxbot config.yaml``
and it will join and connect. We recommend running pmxbot under
your favorite process supervisor to make it
automatically restart if it crashes (or terminates due to a planned
restart).

Custom Features
===============

Setuptools Entry Points Plugin
------------------------------

``pmxbot`` provides an extension mechanism for adding commands, and uses this
mechanism even for its own built-in commands.

To create a setuptools
entry point plugin, package your modules using
the setuptools tradition and install it alongside pmxbot. Your package
should define an entry point in the group ``pmxbot_handlers`` by including
something similar to the following in the package's setup.py::

    entry_points = {
        'pmxbot_handlers': [
            'plugin name = pmxbot.mymodule',
        ],
    },

During startup,
pmxbot will load ``pmxbot.mymodule``. ``plugin name`` can be anything, but should
be a name suitable to identify the plugin (and it will be displayed during
pmxbot startup).

Note that the ``pmxbot`` package is a namespace package, and you're welcome
to use that namespace for your plugin (e.g.
`pmxbot.nsfw <https://github.com/pmxbot/pmxbot.nsfw>`_).

If your plugin requires any initialization, specify an initialization function
(or class method) in the entry point. For example::

    'plugin name = pmxbot.mymodule:initialize_func'

On startup, pmxbot will call ``initialize_func`` with no parameters.

Within the script you'll want to import the decorator(s) you need to use with::

    from pmxbot.core import command, contains, regexp, execdelay, execat`.

You'll
then decorate each function with the appropriate line so pmxbot registers it.

A command (!g) gets the @command decorator::

  @command(aliases=('tt', 'tear', 'cry'))
  def tinytear(rest):
    "I cry a tiny tear for you."
    if rest:
      return "/me sheds a single tear for %s" % rest
    else:
      return "/me sits and cries as a single tear slowly trickles down its cheek"

A response (when someone says something) uses the @contains decorator::

  @contains("sqlonrails")
  def yay_sor():
    karma.Karma.store.change('sql on rails', 1)
    return "Only 76,417 lines..."

Each handler may solicit any of the following parameters:

 - channel (the channel in which the message occurred)
 - nick (the nickname that triggered the command or behavior)
 - rest (any text after the command)

A more complicated response (when you want to extract data from a message) uses
the @regexp decorator::

    @regexp("jira", r"(?<![a-zA-Z0-9/])(OPS|LIB|SALES|UX|GENERAL|SUPPORT)-\d\d+")
    def jira(client, event, channel, nick, match):
        return "https://jira.example.com/browse/%s" % match.group()

For an example of how to implement a setuptools-based plugin, see one of the
many examples in the pmxbot project itself or one of the popular third-party
projects:

 - `motivation <https://github.com/pmxbot/motivation>`_.
 - `wolframalpha <https://github.com/jaraco/wolframalpha>`_.
 - `jaraco.translate <https://github.com/jaraco/jaraco.translate>`_.
 - `excuses <https://github.com/pmxbot/excuses>`_.

Web Interface
=============

pmxbot includes a web server for allowing users to view the logs, read the
help, and check karma. You specify the host, port, base path, logo, title,
etc with the same YAML config file. Just run like ``pmxbotweb config.yaml``
and it will start up. Like pmxbot, use of a supervisor is recommended to
restart the process following termination.

pmxbot as a Slack bot (native)
==============================

To use pmxbot as a Slack bot, install with ``pmxbot[slack]``,
and set ``slack token`` in your config to the token from your
`Bot User <https://api.slack.com/bot-users>`_. Easy, peasy.

pmxbot as a Slack bot (IRC)
===========================

As Slack provides an IRC interface, it's easy to configure pmxbot for use
in Slack. Here's how:

0. Install with ``pmxbot[irc]``.
1. `Enable the IRC Gateway <https://slack.zendesk.com/hc/en-us/articles/201727913-Connecting-to-Slack-over-IRC-and-XMPP>`_.
2. Create an e-mail for the bot.
3. Create the account for the bot in Slack and activate its account.
4. Log into Slack using that new account and `get the IRC gateway
   password <https://my.slack.com/account/gateways>`_ for that
   account.
5. Configure the pmxbot as you would for an IRC server, but use these
   settings for the connection:

    message rate limit: 2.5
    password: <gateway password>
    server_host: <team name>.irc.slack.com
    server_port: 6667

   The rate limit is necessary because Slack will kick the bot if it issues more than 25 messages in 10 seconds, so throttling it to 2.5 messages per
   second avoids hitting the limit.
6. Consider leaving 'log_channels' and 'other_channels' empty, especially
   if relying on Slack logging. Slack will automatically re-join pmxbot to
   any channels to which it has been ``/invited``.

For Enterprise
==============

Available as part of the Tidelift Subscription.

This project and the maintainers of thousands of other packages are working with Tidelift to deliver one enterprise subscription that covers all of the open source you use.

`Learn more <https://tidelift.com/subscription/pkg/pypi-pmxbot?utm_source=pypi-pmxbot&utm_medium=referral&utm_campaign=github>`_.

Quote libraries
==============

``!quote`` and ``!q`` use the default ``pmx`` quote library. To add commands
for other libraries, configure a command-to-library mapping in YAML::

    quote_libraries:
        album: album
        band: band
        tunes: song
        robjob: robjob
        food: food
        tagline: tagline

Restart the bot after changing this mapping. Keys are command names without
``!``; values are library names stored in the existing quotes database. Multiple
commands may use the same library. Command names are case-insensitive; library
names retain their case. No additional commands are enabled by default.

For example, ``!tunes add: Blue Monday`` adds to the ``song`` library,
``!tunes`` returns a random entry, and ``!tunes Blue`` searches that library.
``!tunes Blue 2`` selects its second matching entry. ``!tunes del: Blue``
deletes only when exactly one entry matches; ``!tunes del: Blue 2`` deletes
the second matching entry. Both ``add`` and ``del`` also work without colons.
Empty additions and invalid or ambiguous deletions leave the library unchanged.

Configured commands appear in command help. Invalid entries and names that
conflict with existing commands (including ``quote`` and ``q``) are skipped
with an error in the startup log. Libraries need no separate creation step;
the first addition creates their first entry. Existing quote data needs no
migration. SQLite and MongoDB support the same library commands.

OpenAI spending
===============

``!openaiusage`` reports organization-wide spending recorded today in USD,
using midnight UTC as the day boundary. Set ``OPENAI_ADMIN_KEY`` in the bot's
environment, or ``openai_admin_key`` in its YAML configuration. This requires
an OpenAI admin key with access to organization costs; the regular
``openai_api_key`` used for image generation is separate.

The command uses the `OpenAI Costs API
<https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs>`_
and includes all projects and usage types. Costs may lag recent requests.
The documented API does not expose prepaid credit balances, so the command
reports credits left as unavailable and links to the billing dashboard.
It does not infer a balance from spending limits or token counts.
