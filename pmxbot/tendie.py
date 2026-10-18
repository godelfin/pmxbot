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
from collections.abc import Mapping

import yaml

import requests

import pmxbot
from .core import command
from .dictlib import ConfigDict
from .storage import SQLiteStorage
from .users import UserStore


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

    def sync(self, provider, cooldown=300, include_replies=False, include_quotes=True):
        """Import provider.posts(since_id) atomically; retain cooldown on failure.

        Providers yield dictionaries with id, text, created_at, author (username),
        and optional referenced_tweets as in X v2. Offline providers may ignore
        since_id to backfill history. Duplicate IDs are never overwritten.
        """
        if not _sync_lock.acquire(blocking=False):
            raise SyncError('Tendie synchronization is already running.')
        try:
            return self._sync(
                provider, max(1, cooldown), include_replies, include_quotes
            )
        finally:
            _sync_lock.release()

    def _sync(self, provider, cooldown, include_replies, include_quotes):
        # Claim synchronization in a short transaction. No primary-database
        # write lock is held while requesting pages from a provider.
        owner = uuid.uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            started = self.meta('sync_started')
            if started and (now() - timestamp(started)).total_seconds() < 1800:
                raise SyncError('Tendie synchronization is already running.')
            attempted = self.meta('last_attempt')
            if attempted and (now() - timestamp(attempted)).total_seconds() < cooldown:
                raise SyncError(
                    'Tendie synchronization is cooling down. Try again later.'
                )
            self.set_meta('last_attempt', now().isoformat())
            self.set_meta('sync_started', now().isoformat())
            self.set_meta('sync_owner', owner)
            row = self.db.execute(
                'SELECT id FROM tendie_posts WHERE author=? COLLATE NOCASE ORDER BY length(id) DESC, id DESC LIMIT 1',
                (self.author,),
            ).fetchone()
            since_id = row[0] if row else None
        try:
            posts = list(provider.posts(since_id))
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if self.meta('sync_owner') != owner:
                    raise SyncError(
                        'Tendie synchronization lease expired; try again later.'
                    )
                return self._import_posts(posts, include_replies, include_quotes)
        finally:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if self.meta('sync_owner') == owner:
                    self.set_meta('sync_started', '')
                    self.set_meta('sync_owner', '')

    def _import_posts(self, posts, include_replies, include_quotes):
        count = 0
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
        self.set_meta('last_success', now().isoformat())
        return count


class XProvider:
    """Official X v2 user timeline, with bounded pagination and request timeouts."""

    def __init__(self, token, author=None):
        self.token = token
        self.author = configured_author(author)

    def get(self, path, params=None):
        response = requests.get(
            'https://api.x.com/2/' + path,
            headers={'Authorization': 'Bearer ' + self.token},
            params=params,
            timeout=15,
        )
        if response.status_code == 429:
            raise SyncError('X rate limit reached. Try updating later.')
        response.raise_for_status()
        result = response.json()
        if result.get('errors'):
            raise SyncError('X returned an incomplete response; no posts imported.')
        return result

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
        raise SyncError('X pagination limit reached; no posts imported.')


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


def authenticated_admin(event):
    """Trust only a server account tag mapped to a currently active local admin.

    IRC nicknames and client-only +account tags are not authentication. A
    missing event (including other transports) or malformed mapping fails closed.
    """
    if getattr(event, 'type', None) not in ('pubmsg', 'privmsg'):
        return False
    try:
        accounts = [tag['value'] for tag in event.tags if tag['key'] == 'account']
        if (
            len(accounts) != 1
            or not isinstance(accounts[0], str)
            or not accounts[0]
            or accounts[0] == '*'
        ):
            return False
        mapping = pmxbot.config.get('tendie_admin_accounts', {})
        if not isinstance(mapping, Mapping):
            return False
        usernames = [
            username
            for account, username in mapping.items()
            if isinstance(account, str) and account.casefold() == accounts[0].casefold()
        ]
        if len(usernames) != 1 or not isinstance(usernames[0], str):
            return False
        with contextlib.closing(
            UserStore(pmxbot.config.get('database', 'sqlite:pmxbot.sqlite'))
        ) as store:
            return store.get_by_username(usernames[0]).is_admin
    except (ValueError, sqlite3.Error, OSError, KeyError, TypeError, AttributeError):
        return False


@command()
def tendie(rest, event=None):
    """Random stored tendie; latest, days, status; update requires an authenticated administrator."""
    action = rest.strip().lower()
    if action not in ('', 'latest', 'days', 'update', 'status'):
        return 'Usage: !tendie [latest|days|update|status]'
    if action == 'update' and not authenticated_admin(event):
        return 'Tendie update requires an authenticated administrator.'
    try:
        with contextlib.closing(Collection()) as store:
            if action == 'status':
                return f'Tendie: {store.size()} posts; last successful synchronization: {store.meta("last_success") or "never"}.'
            if action == 'update':
                token = pmxbot.config.get('x_bearer_token') or os.environ.get(
                    'X_BEARER_TOKEN'
                )
                if not token:
                    return 'Configure x_bearer_token or X_BEARER_TOKEN to synchronize tendies.'
                count = store.sync(
                    XProvider(token, store.author),
                    cooldown=float(pmxbot.config.get('tendie_sync_cooldown', 300)),
                    include_replies=pmxbot.config.get('tendie_include_replies', False),
                    include_quotes=pmxbot.config.get('tendie_include_quotes', True),
                )
                return f'Tendie: imported {count} posts.'
            post = store.select(latest=bool(action))
            if post is None:
                return 'No tendies stored. Ask an administrator to synchronize or import posts.'
            return days_since(post) if action == 'days' else format_post(post)
    except SyncError as exc:
        return str(exc)
    except requests.RequestException:
        return 'Tendie synchronization failed; no posts imported. Try again later.'
    except sqlite3.OperationalError:
        return 'Tendie storage is unavailable or synchronization is already running.'
    except (ValueError, KeyError, TypeError, AttributeError):
        return 'Invalid tendie data or configuration; no posts imported.'


def main():
    parser = argparse.ArgumentParser(description='Import local tendie JSON/CSV posts')
    parser.add_argument('filename')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        '--config', help='Bot configuration YAML (uses its primary database)'
    )
    source.add_argument('--database', help='Primary SQLite URI or filename')
    parser.add_argument('--author', help='Source X username (or set TENDIE_AUTHOR)')
    parser.add_argument('--include-replies', action='store_true', default=None)
    parser.add_argument('--exclude-quotes', action='store_true', default=None)
    args = parser.parse_args()
    try:
        config = ConfigDict.from_yaml(args.config) if args.config else {}
        uri = args.database or config.get('database', 'sqlite:pmxbot.sqlite')
        author = args.author or config.get('tendie_author')
        with contextlib.closing(Collection(uri, author)) as store:
            count = store.sync(
                FileProvider(args.filename),
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
            )
        print(f'Imported {count} posts.')
    except (
        SyncError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        sqlite3.Error,
        yaml.YAMLError,
    ) as exc:
        parser.exit(1, f'Import failed ({type(exc).__name__}); no posts imported.\n')


if __name__ == '__main__':
    main()
