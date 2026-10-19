"""Local configured-account collection and provider-independent imports."""

import argparse
import contextlib
import csv
import datetime
import html
import json
import os
import re
import sqlite3
import threading
import urllib.parse
import uuid
from time import sleep

import yaml

import requests

import pmxbot
from . import urls
from .core import command
from .dictlib import ConfigDict
from .storage import SQLiteStorage


UTC = datetime.timezone.utc
_sync_lock = threading.Lock()


def now():
    return datetime.datetime.now(UTC)


def timestamp(value):
    """Parse an explicitly timezone-aware ISO timestamp and normalize to UTC."""
    result = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return result.astimezone(UTC)


class SyncError(Exception):
    """Safe, public synchronization failure message."""


class CursorError(SyncError):
    """The saved cursor was rejected; recovery needs explicit administrator intent."""


def validated_page_limit(value):
    """Accept a bounded page budget from a form or trusted Python caller."""
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and len(value) <= 3
    ):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError('Choose a page limit from 1 to 100.')
    return value


def configured_author(value=None):
    value = (
        value or pmxbot.config.get('tendie_author') or os.environ.get('TENDIE_AUTHOR')
    )
    if not value:
        raise SyncError(
            'Configure tendie_author or TENDIE_AUTHOR with the source X username.'
        )
    if not isinstance(value, str):
        raise ValueError('Invalid source username')
    author = value.strip().lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', author):
        raise ValueError('Invalid source username')
    return author


class Collection(SQLiteStorage):
    def __init__(self, uri=None, author=None):
        self.author = configured_author(author)
        uri = str(uri or pmxbot.config.get('database', 'sqlite:pmxbot.sqlite'))
        if self.uri_matches(uri) and not uri.startswith('sqlite:'):
            uri = 'sqlite:' + uri
        parsed = urllib.parse.urlparse(uri)
        if (
            parsed.scheme not in ('', 'sqlite')
            or not parsed.path
            or parsed.path == ':memory:'
        ):
            raise SyncError('Tendie requires a persistent SQLite primary database.')
        super().__init__(uri)

    def init_tables(self):
        from .tendie_staging import SCHEMA

        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            '''
            CREATE TABLE IF NOT EXISTS tendie_posts (
                id TEXT PRIMARY KEY, text TEXT NOT NULL, author TEXT NOT NULL,
                created_at TEXT NOT NULL, imported_at TEXT NOT NULL, url TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tendie_created ON tendie_posts(created_at);
            CREATE TABLE IF NOT EXISTS tendie_sync (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            '''
        )
        self.db.executescript(SCHEMA)

    def progress(self):
        from .tendie_staging import progress

        return progress(self)

    def meta(self, key):
        row = self.db.execute(
            'SELECT value FROM tendie_sync WHERE key=?',
            (self.author.lower() + ':' + key,),
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute(
            'INSERT OR REPLACE INTO tendie_sync VALUES (?, ?)',
            (self.author.lower() + ':' + key, value),
        )

    def select(self, latest=False):
        order = 'created_at DESC, length(id) DESC, id DESC' if latest else 'RANDOM()'
        return self.db.execute(
            'SELECT * FROM tendie_posts WHERE author=? COLLATE NOCASE ORDER BY '
            + order
            + ' LIMIT 1',
            (self.author,),
        ).fetchone()

    def size(self):
        return self.db.execute(
            'SELECT count(*) FROM tendie_posts WHERE author=? COLLATE NOCASE',
            (self.author,),
        ).fetchone()[0]

    def sync(
        self,
        provider,
        cooldown=300,
        include_replies=False,
        include_quotes=True,
        *,
        live=False,
        operation='update',
        page_limit=100,
    ):
        """Import iterator providers atomically; stage X pages and commit per page.

        Providers yield dictionaries with id, text, created_at, author (username),
        and optional referenced_tweets as in X v2. Offline providers may ignore
        since_id to backfill history. Duplicate IDs are never overwritten.
        Live runs advance the checkpoint only after their whole range completes.
        Generic live=True iterators include validated posts that
        filtering excludes. Offline backfills must leave this flag unset.
        """
        if not _sync_lock.acquire(blocking=False):
            raise SyncError('Tendie synchronization is already running.')
        try:
            return self._sync(
                provider,
                max(1, cooldown),
                include_replies,
                include_quotes,
                live,
                operation,
                validated_page_limit(page_limit),
            )
        finally:
            _sync_lock.release()

    def _sync(
        self,
        provider,
        cooldown,
        include_replies,
        include_quotes,
        live,
        operation,
        page_limit,
    ):
        if operation not in ('update', 'recover', 'recover_range'):
            raise ValueError('Unknown synchronization operation')
        if operation != 'update' and not live:
            raise ValueError('Recovery requires a live synchronization run')
        if operation == 'recover':
            # Local recovery cannot accidentally invoke an iterator provider.
            provider = None
        # Claim synchronization in a short transaction. No primary-database
        # write lock is held while requesting pages from a provider.
        owner = uuid.uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            started = self.meta('sync_started')
            if started and (now() - timestamp(started)).total_seconds() < 1800:
                raise SyncError('Tendie synchronization is already running.')
            attempted = self.meta('last_attempt')
            if (
                operation != 'recover'
                and attempted
                and (now() - timestamp(attempted)).total_seconds() < cooldown
            ):
                raise SyncError(
                    'Tendie synchronization is cooling down. Try again later.'
                )
            if operation != 'recover':
                self.set_meta('last_attempt', now().isoformat())
            self.set_meta('sync_started', now().isoformat())
            self.set_meta('sync_owner', owner)
            since_id = self.meta('live_since_id') if live else None
        try:
            if isinstance(provider, XProvider) or (live and provider is None):
                from .tendie_staging import LiveRun

                return LiveRun(self, owner).run(
                    provider, operation, include_replies, include_quotes, page_limit
                )
            posts = list(provider.posts(since_id))
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if self.meta('sync_owner') != owner:
                    raise SyncError(
                        'Tendie synchronization lease expired; try again later.'
                    )
                return self._import_posts(posts, include_replies, include_quotes, live)
        finally:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if self.meta('sync_owner') == owner:
                    self.set_meta('sync_started', '')
                    self.set_meta('sync_owner', '')

    def _import_posts(
        self, posts, include_replies, include_quotes, live, record_success=True
    ):
        count = 0
        checkpoint = self.meta('live_since_id') if live else None
        for post in posts:
            post_id = str(post['id'])
            if not re.fullmatch(r'[1-9][0-9]{0,29}', post_id):
                raise ValueError('Invalid post ID')
            if post['author'].lstrip('@').lower() != self.author.lower():
                raise ValueError('Unexpected author')
            created = timestamp(post['created_at']).isoformat()
            if not isinstance(post['text'], str):
                raise ValueError('Invalid text')
            kinds = {ref['type'] for ref in post.get('referenced_tweets', [])}
            if live and (checkpoint is None or int(post_id) > int(checkpoint)):
                checkpoint = post_id
            if (
                'retweeted' in kinds
                or (not include_replies and 'replied_to' in kinds)
                or (not include_quotes and 'quoted' in kinds)
            ):
                continue
            count += self.db.execute(
                'INSERT OR IGNORE INTO tendie_posts VALUES (?, ?, ?, ?, ?, ?)',
                (
                    post_id,
                    post['text'],
                    self.author,
                    created,
                    now().isoformat(),
                    f'https://x.com/{self.author}/status/{post_id}',
                ),
            ).rowcount
        if live and checkpoint is not None:
            self.set_meta('live_since_id', checkpoint)
        if record_success:
            self.set_meta('last_success', now().isoformat())
        return count


class XProvider:
    """Official X v2 user timeline, with bounded pagination and request timeouts."""

    def __init__(self, token, author=None):
        self.token = token
        self.author = configured_author(author)

    def get(self, path, params=None, on_response=None, before_request=None):
        if not self.token:
            raise SyncError(
                'Configure x_bearer_token or X_BEARER_TOKEN to synchronize tendies.'
            )
        for delay in (0, 1, 2, 4):
            if delay:
                sleep(delay)
            if before_request:
                before_request()
            response = requests.get(
                'https://api.x.com/2/' + path,
                headers={'Authorization': 'Bearer ' + self.token},
                params=params,
                timeout=15,
            )
            try:
                result = response.json()
            except ValueError:
                self.check_status(response, params)
                response.raise_for_status()
                raise
            if on_response:
                on_response(result, response.status_code)
            self.check_status(response, params)
            response.raise_for_status()
            errors = result.get('errors')
            if not errors:
                return result
            if not (
                isinstance(errors, list)
                and all(
                    isinstance(error, dict)
                    and error.get('type')
                    == 'https://api.x.com/2/problems/field-hydration-failure'
                    for error in errors
                )
            ):
                raise SyncError('X returned an incomplete response.')
        raise SyncError('X could not resolve referenced_tweets after retries.')

    @staticmethod
    def check_status(response, params):
        if response.status_code == 429:
            raise SyncError('X rate limit reached. Try updating later.')
        if response.status_code == 402:
            raise SyncError(
                'X payment or credits prevented this request. Saved progress is retained.'
            )
        if (
            response.status_code in (400, 410)
            and params
            and params.get('pagination_token')
        ):
            raise CursorError(
                'X rejected the saved pagination token. Use explicit range recovery; saved pages will not be refetched.'
            )

    def posts(self, since_id):
        user = self.get('users/by/username/' + self.author)['data']['id']
        if not re.fullmatch(r'[0-9]+', str(user)):
            raise ValueError('Invalid user ID')
        params = {
            'max_results': 100,
            'exclude': 'retweets',
            'tweet.fields': 'created_at,author_id,referenced_tweets',
        }
        if since_id:
            params['since_id'] = since_id
        seen = set()
        for _ in range(100):
            page = self.get(f'users/{user}/tweets', params=dict(params))
            posts = page.get('data', [])
            meta = page['meta']
            if not isinstance(posts, list) or meta.get(
                'result_count', len(posts)
            ) != len(posts):
                raise ValueError('Incomplete timeline page')
            for post in posts:
                if str(post['author_id']) != str(user):
                    raise ValueError('Unexpected author ID')
                yield dict(post, author=self.author)
            cursor = meta.get('next_token')
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError('Invalid pagination token')
            seen.add(cursor)
            params['pagination_token'] = cursor
        raise SyncError('X pagination limit reached.')


class FileProvider:
    def __init__(self, filename):
        self.filename = filename

    def posts(self, since_id):
        with open(self.filename, encoding='utf-8', newline='') as stream:
            if str(self.filename).lower().endswith('.csv'):
                for row in csv.DictReader(stream):
                    if row.get('referenced_tweets'):
                        row['referenced_tweets'] = json.loads(row['referenced_tweets'])
                    else:
                        row.pop('referenced_tweets', None)
                    yield row
            else:
                yield from json.load(stream)


def format_post(post):
    text = html.unescape(post['text'])
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text)
    text = ' '.join(text.split())
    suffix = ' ' + post['url']
    budget = 400 - len(suffix.encode('utf-8'))
    if len(text.encode('utf-8')) > budget:
        text = (
            text.encode('utf-8')[: budget - 3].decode('utf-8', errors='ignore') + '...'
        )
    return text + suffix


def days_since(post, current=None):
    try:
        days = max(
            0, ((current or now()).astimezone(UTC) - timestamp(post['created_at'])).days
        )
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 'The latest tendie has an invalid creation timestamp. ' + post['url']
    unit = 'day' if days == 1 else 'days'
    return f'{days} complete {unit} since the latest tendie. ' + post['url']


def settings_snapshot(config):
    result = dict(config)
    result['tendie_author'] = result.get('tendie_author') or os.environ.get(
        'TENDIE_AUTHOR'
    )
    result['x_bearer_token'] = result.get('x_bearer_token') or os.environ.get(
        'X_BEARER_TOKEN'
    )
    return result


def synchronize(config, operation='update', page_limit=100):
    """Trusted operator entry point; the web worker owns its SQLite connection."""
    if not config.get('tendie_author'):
        raise SyncError(
            'Configure tendie_author or TENDIE_AUTHOR with the source X username.'
        )
    author = configured_author(config['tendie_author'])
    with contextlib.closing(
        Collection(config.get('database', 'sqlite:pmxbot.sqlite'), author)
    ) as store:
        return store.sync(
            (
                None
                if operation == 'recover'
                else XProvider(config.get('x_bearer_token'), store.author)
            ),
            live=True,
            cooldown=float(config.get('tendie_sync_cooldown', 300)),
            include_replies=config.get('tendie_include_replies', False),
            include_quotes=config.get('tendie_include_quotes', True),
            operation=operation,
            page_limit=page_limit,
        )


def synchronization_error(exc):
    if isinstance(exc, SyncError):
        return str(exc)
    if isinstance(exc, requests.RequestException):
        return 'Tendie synchronization failed. Try again later.'
    if isinstance(exc, sqlite3.Error):
        return 'Tendie storage is unavailable; please try again later.'
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        return 'Invalid tendie data or configuration.'
    return 'Tendie update failed; please try again later.'


def administrator_url(config):
    return urls.public_application_url(config.get('albums_url'), 'admin/tendies')


@command()
def tendie(rest):
    """Random stored tendie; latest, days, status; administrators update on the website."""
    action = rest.strip().lower()
    if action not in ('', 'latest', 'days', 'update', 'status'):
        return 'Usage: !tendie [latest|days|update|status]'
    if action == 'update':
        message = 'Administrators must use the website to update tendies.'
        url = administrator_url(pmxbot.config)
        return message + (' ' + url if url else '')
    return _response(action, pmxbot.config)


def _response(action, config):
    try:
        with contextlib.closing(
            Collection(
                config.get('database', 'sqlite:pmxbot.sqlite'),
                config.get('tendie_author'),
            )
        ) as store:
            if action == 'status':
                return f'Tendie: {store.size()} posts; last successful synchronization: {store.meta("last_success") or "never"}.'
            post = store.select(latest=bool(action))
            if post is None:
                return 'No tendies stored. Ask an administrator to synchronize or import posts.'
            return days_since(post) if action == 'days' else format_post(post)
    except SyncError as exc:
        return str(exc)
    except requests.RequestException:
        return 'Tendie synchronization failed. Try again later.'
    except sqlite3.OperationalError:
        return 'Tendie storage is unavailable or synchronization is already running.'
    except (ValueError, KeyError, TypeError, AttributeError):
        return 'Invalid tendie data or configuration.'


def main():
    parser = argparse.ArgumentParser(
        description='Import local tendie JSON/CSV posts or replay staged X pages without network access'
    )
    parser.add_argument('filename', nargs='?')
    parser.add_argument(
        '--recover',
        action='store_true',
        help='Replay saved clean pages locally; makes zero X requests',
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        '--config', help='Bot configuration YAML (uses its primary database)'
    )
    source.add_argument('--database', help='Primary SQLite URI or filename')
    parser.add_argument('--author', help='Source X username (or set TENDIE_AUTHOR)')
    parser.add_argument('--include-replies', action='store_true', default=None)
    parser.add_argument('--exclude-quotes', action='store_true', default=None)
    args = parser.parse_args()
    if bool(args.filename) == args.recover:
        parser.error('Supply a filename or --recover, exclusively.')
    try:
        config = ConfigDict.from_yaml(args.config) if args.config else {}
        uri = args.database or config.get('database', 'sqlite:pmxbot.sqlite')
        author = args.author or config.get('tendie_author')
        with contextlib.closing(Collection(uri, author)) as store:
            count = store.sync(
                None if args.recover else FileProvider(args.filename),
                cooldown=float(config.get('tendie_sync_cooldown', 300)),
                include_replies=(
                    args.include_replies
                    if args.include_replies is not None
                    else config.get('tendie_include_replies', False)
                ),
                include_quotes=(
                    not args.exclude_quotes
                    if args.exclude_quotes is not None
                    else config.get('tendie_include_quotes', True)
                ),
                live=args.recover,
                operation='recover' if args.recover else 'update',
            )
            progress = store.progress() if args.recover else None
        print(f'Imported {count} posts.')
        if progress:
            print(
                f'Local replay: {progress["status"]}; {progress["unresolved_pages"]} unresolved page positions. No X requests made.'
            )
        elif args.recover:
            print('Local replay: no unfinished run. No X requests made.')
    except (
        SyncError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        sqlite3.Error,
        yaml.YAMLError,
    ) as exc:
        message = (
            synchronization_error(exc)
            if args.recover
            else f'Import failed ({type(exc).__name__}); no posts imported.'
        )
        parser.exit(1, message + '\n')


if __name__ == '__main__':
    main()
