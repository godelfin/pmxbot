"""Local Robert_McTender collection and provider-independent imports."""

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

import requests

import pmxbot
from .core import command


AUTHOR = 'Robert_McTender'
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


class Collection:
    def __init__(self, filename):
        self.db = sqlite3.connect(str(filename), timeout=0)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
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

    def close(self):
        self.db.close()

    def meta(self, key):
        row = self.db.execute(
            'SELECT value FROM tendie_sync WHERE key=?', (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute(
            'INSERT OR REPLACE INTO tendie_sync VALUES (?, ?)', (key, value)
        )

    def select(self, latest=False):
        order = 'created_at DESC, length(id) DESC, id DESC' if latest else 'RANDOM()'
        return self.db.execute(
            'SELECT * FROM tendie_posts ORDER BY ' + order + ' LIMIT 1'
        ).fetchone()

    def size(self):
        return self.db.execute('SELECT count(*) FROM tendie_posts').fetchone()[0]

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
        # A committed attempt timestamp protects against rapid retries, including
        # failed requests and restarts. SQLite serializes writers across processes.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            attempted = self.meta('last_attempt')
            if attempted and (now() - timestamp(attempted)).total_seconds() < cooldown:
                raise SyncError(
                    'Tendie synchronization is cooling down. Try again later.'
                )
            self.set_meta('last_attempt', now().isoformat())
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT id FROM tendie_posts ORDER BY length(id) DESC, id DESC LIMIT 1'
            ).fetchone()
            since_id = row[0] if row else None
            count = 0
            for post in provider.posts(since_id):
                post_id = str(post['id'])
                if not re.fullmatch(r'[1-9][0-9]{0,29}', post_id):
                    raise ValueError('Invalid post ID')
                if post['author'].lstrip('@').lower() != AUTHOR.lower():
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
                        AUTHOR,
                        created,
                        now().isoformat(),
                        f'https://x.com/{AUTHOR}/status/{post_id}',
                    ),
                ).rowcount
            self.set_meta('last_success', now().isoformat())
        return count


class XProvider:
    """Official X v2 user timeline, with bounded pagination and request timeouts."""

    def __init__(self, token):
        self.token = token

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
        user = self.get('users/by/username/' + AUTHOR)['data']['id']
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
                yield dict(post, author=AUTHOR)
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


@command()
def tendie(rest):
    """Random stored tendie; subcommands: latest, days, update, status."""
    action = rest.strip().lower()
    if action not in ('', 'latest', 'days', 'update', 'status'):
        return 'Usage: !tendie [latest|days|update|status]'
    try:
        with contextlib.closing(
            Collection(pmxbot.config.get('tendie_database', 'tendie.sqlite'))
        ) as store:
            if action == 'status':
                return f'Tendie: {store.size()} posts; last successful synchronization: {store.meta("last_success") or "never"}.'
            if action == 'update':
                token = pmxbot.config.get('x_bearer_token') or os.environ.get(
                    'X_BEARER_TOKEN'
                )
                if not token:
                    return 'Configure x_bearer_token or X_BEARER_TOKEN to synchronize tendies.'
                count = store.sync(
                    XProvider(token),
                    cooldown=float(pmxbot.config.get('tendie_sync_cooldown', 300)),
                    include_replies=pmxbot.config.get('tendie_include_replies', False),
                    include_quotes=pmxbot.config.get('tendie_include_quotes', True),
                )
                return f'Tendie: imported {count} posts.'
            post = store.select(latest=bool(action))
            if post is None:
                return (
                    'No tendies stored. Use !tendie update or import a JSON/CSV file.'
                )
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
    parser.add_argument('--database', default='tendie.sqlite')
    parser.add_argument('--include-replies', action='store_true')
    parser.add_argument('--exclude-quotes', action='store_true')
    args = parser.parse_args()
    try:
        with contextlib.closing(Collection(args.database)) as store:
            count = store.sync(
                FileProvider(args.filename),
                include_replies=args.include_replies,
                include_quotes=not args.exclude_quotes,
            )
        print(f'Imported {count} posts.')
    except (SyncError, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'Import failed ({type(exc).__name__}); no posts imported.\n')


if __name__ == '__main__':
    main()
