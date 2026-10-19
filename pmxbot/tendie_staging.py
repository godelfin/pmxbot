"""Durable, account-scoped X timeline runs. No credentials belong in this journal."""

import json
import re
import uuid

from . import tendie


SCHEMA = '''
CREATE TABLE IF NOT EXISTS tendie_runs (
    id TEXT PRIMARY KEY, author TEXT NOT NULL, state TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tendie_active_run
    ON tendie_runs(author) WHERE complete=0;
CREATE TABLE IF NOT EXISTS tendie_pages (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, position INTEGER NOT NULL,
    params TEXT NOT NULL, body TEXT NOT NULL, http_status INTEGER NOT NULL,
    saved_at TEXT NOT NULL, post_count INTEGER NOT NULL,
    invalid INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tendie_page_position ON tendie_pages(run_id, position);
'''


def redacted(value, secret):
    """Keep response data/errors, excluding reflected credentials and headers."""
    if isinstance(value, dict):
        return {
            redacted(key, secret): redacted(item, secret)
            for key, item in value.items()
            if key.lower() not in ('authorization', 'headers', 'bearer_token')
        }
    if isinstance(value, list):
        return [redacted(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, '[redacted]')
    return value


def latest(store, incomplete=False):
    return store.db.execute(
        'SELECT * FROM tendie_runs WHERE author=?'
        + (' AND complete=0' if incomplete else '')
        + ' ORDER BY rowid DESC LIMIT 1',
        (store.author.lower(),),
    ).fetchone()


def progress(store):
    """Allowlisted summary; raw data, errors, request parameters and cursors stay private."""
    row = latest(store)
    if row is None:
        return None
    state = json.loads(row['state'])
    saved = store.db.execute(
        'SELECT count(*), coalesce(sum(post_count), 0) FROM tendie_pages WHERE run_id=?',
        (row['id'],),
    ).fetchone()
    return dict(
        status=state['status'],
        saved_responses=saved[0],
        saved_posts=saved[1],
        imported=state['imported'],
        processed_pages=state['position'],
        unresolved_pages=0 if row['complete'] else 1,
        last_progress=row['updated_at'],
        completed_at=state.get('completed_at'),
        message=state['message'],
        can_recover_range=state['status'] == 'token_rejected' and bool(state['oldest']),
    )


class LiveRun:
    def __init__(self, store, owner):
        self.store = store
        self.owner = owner
        self.db = store.db

    def owned(self):
        if self.store.meta('sync_owner') != self.owner:
            raise tendie.SyncError(
                'Tendie synchronization lease expired; try again later.'
            )
        self.store.set_meta('sync_started', tendie.now().isoformat())

    def save(self):
        self.db.execute(
            'UPDATE tendie_runs SET state=?, complete=?, updated_at=? WHERE id=? AND author=?',
            (
                json.dumps(self.state),
                int(self.state['status'] == 'complete'),
                tendie.now().isoformat(),
                self.identifier,
                self.store.author.lower(),
            ),
        )

    def heartbeat(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.owned()

    def reload(self):
        row = self.db.execute(
            'SELECT state FROM tendie_runs WHERE id=? AND author=?',
            (self.identifier, self.store.author.lower()),
        ).fetchone()
        self.state = json.loads(row['state'])

    def stage(self, body, status, params, secret):
        # This transaction deliberately precedes validation and the next request.
        # Even if our lease was replaced during a paid call, preserve its response.
        body = redacted(body, secret)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute(
                'INSERT INTO tendie_pages (run_id, position, params, body, http_status, saved_at, post_count) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    self.identifier,
                    self.state['position'],
                    json.dumps(params),
                    json.dumps(body),
                    status,
                    tendie.now().isoformat(),
                    (
                        len(body['data'])
                        if isinstance(body, dict) and isinstance(body.get('data'), list)
                        else 0
                    ),
                ),
            )
            self.db.execute(
                'UPDATE tendie_runs SET updated_at=? WHERE id=? AND author=?',
                (tendie.now().isoformat(), self.identifier, self.store.author.lower()),
            )
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.owned()

    def saved_clean(self, local):
        pages = self.db.execute(
            'SELECT * FROM tendie_pages WHERE run_id=? AND position=?'
            + ('' if local else ' AND invalid=0')
            + ' ORDER BY id DESC',
            (self.identifier, self.state['position']),
        )
        for page in pages:
            body = json.loads(page['body'])
            if (
                200 <= page['http_status'] < 300
                and isinstance(body, dict)
                and not body.get('errors')
            ):
                return page
        return None

    def process(self, page):
        body = json.loads(page['body'])
        posts, meta = body.get('data', []), body['meta']
        if (
            not isinstance(posts, list)
            or not isinstance(meta, dict)
            or meta.get('result_count', len(posts)) != len(posts)
        ):
            raise ValueError('Incomplete timeline page')
        cursor = meta.get('next_token')
        if cursor is not None and (
            not isinstance(cursor, str) or not cursor or cursor in self.state['seen']
        ):
            raise ValueError('Invalid pagination token')
        validated = []
        ids = []
        for post in posts:
            if str(post['author_id']) != self.state['user_id']:
                raise ValueError('Unexpected author ID')
            identifier = str(post['id'])
            if not re_full_id(identifier):
                raise ValueError('Invalid post ID')
            if self.state['since_id'] and int(identifier) <= int(
                self.state['since_id']
            ):
                raise ValueError('Post outside requested range')
            upper = self.state['params'].get('until_id')
            if upper and int(identifier) >= int(upper):
                raise ValueError('Post outside requested range')
            references = post.get('referenced_tweets', [])
            if not isinstance(references, list) or any(
                not isinstance(ref, dict)
                or ref.get('type') not in ('retweeted', 'replied_to', 'quoted')
                for ref in references
            ):
                raise ValueError('Invalid reference metadata')
            ids.append(int(identifier))
            validated.append(dict(post, author=self.store.author))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.owned()
            count = self.store._import_posts(
                validated,
                self.state['include_replies'],
                self.state['include_quotes'],
                live=False,
                record_success=False,
            )
            if ids:
                self.state['highest'] = str(
                    max(ids + [int(self.state['highest'] or 0)])
                )
                self.state['oldest'] = str(
                    min(
                        ids
                        + ([int(self.state['oldest'])] if self.state['oldest'] else [])
                    )
                )
            self.state['imported'] += count
            self.state['position'] += 1
            self.state['status'] = 'incomplete' if cursor else 'complete'
            self.state['message'] = (
                'Saved pages processed. More X requests are needed.'
                if cursor
                else 'The requested timeline range is complete.'
            )
            if cursor:
                self.state['seen'].append(cursor)
                self.state['params']['pagination_token'] = cursor
            else:
                completed = tendie.now().isoformat()
                self.state['completed_at'] = completed
                if self.state['highest']:
                    self.store.set_meta('live_since_id', self.state['highest'])
                self.store.set_meta('last_success', completed)
                self.store.set_meta('last_live_success', completed)
            # Posts, applied-page position and checkpoint (only on completion)
            # commit together. A crash can never leave an applied page pending.
            self.save()
        return count

    def run(self, provider, operation, include_replies, include_quotes):
        local = operation == 'recover'
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.owned()
            row = latest(self.store, incomplete=True)
            if row is None:
                if local:
                    return 0
                if operation == 'recover_range':
                    raise tendie.SyncError('There is no interrupted range to recover.')
                self.identifier = uuid.uuid4().hex
                since = self.store.meta('live_since_id')
                self.state = dict(
                    status='incomplete',
                    position=0,
                    imported=0,
                    user_id=None,
                    since_id=since,
                    highest=since,
                    oldest=None,
                    seen=[],
                    include_replies=include_replies,
                    include_quotes=include_quotes,
                    params=dict(
                        max_results=100,
                        exclude='retweets',
                        **{
                            'tweet.fields': 'created_at,author_id,referenced_tweets',
                            **({'since_id': since} if since else {}),
                        },
                    ),
                    message='Run saved. Replay clean pages locally or update using the X API.',
                )
                self.db.execute(
                    'INSERT INTO tendie_runs VALUES (?, ?, ?, 0, ?, ?)',
                    (
                        self.identifier,
                        self.store.author.lower(),
                        json.dumps(self.state),
                        tendie.now().isoformat(),
                        tendie.now().isoformat(),
                    ),
                )
            else:
                self.identifier = row['id']
                self.state = json.loads(row['state'])
            if operation == 'recover_range':
                if self.state['status'] != 'token_rejected' or not self.state['oldest']:
                    raise tendie.SyncError(
                        'Range recovery requires a rejected cursor and a validated boundary.'
                    )
                self.state['params'].pop('pagination_token', None)
                self.state['params']['until_id'] = self.state['oldest']
                self.state['seen'] = []
                self.state['status'] = 'incomplete'
                self.save()
        count = 0
        try:
            for _ in range(100):
                page = self.saved_clean(local)
                if page is None:
                    if local:
                        return count
                    if self.state['status'] == 'token_rejected':
                        raise tendie.CursorError(
                            'X rejected the saved pagination token. Use explicit range recovery; saved pages will not be refetched.'
                        )
                    if not self.state['user_id']:
                        user = str(
                            provider.get(
                                'users/by/username/' + self.store.author,
                                before_request=self.heartbeat,
                            )['data']['id']
                        )
                        if not re_full_id(user):
                            raise ValueError('Invalid user ID')
                        with self.db:
                            self.db.execute('BEGIN IMMEDIATE')
                            self.owned()
                            self.state['user_id'] = user
                            self.save()
                    params = dict(self.state['params'])
                    provider.get(
                        f'users/{self.state["user_id"]}/tweets',
                        params=params,
                        on_response=lambda body, status: self.stage(
                            body, status, params, provider.token
                        ),
                        before_request=self.heartbeat,
                    )
                    page = self.saved_clean(local)
                try:
                    count += self.process(page)
                except (ValueError, KeyError, TypeError, AttributeError):
                    with self.db:
                        self.db.execute('BEGIN IMMEDIATE')
                        self.owned()
                        self.db.execute(
                            'UPDATE tendie_pages SET invalid=1 WHERE id=?',
                            (page['id'],),
                        )
                    raise
                if self.state['status'] == 'complete':
                    return count
            raise tendie.SyncError(
                'Page limit reached. Resume this saved run with another update.'
            )
        except Exception as exc:
            self.reload()
            reason = tendie.synchronization_error(exc).replace(
                '; no posts imported', ''
            )
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.owned()
                if isinstance(exc, tendie.CursorError):
                    self.state['status'] = 'token_rejected'
                self.state['message'] = reason
                self.save()
            summary = progress(self.store)
            raise tendie.SyncError(
                f'Incomplete run: {summary["saved_responses"]} responses saved; '
                f'{summary["imported"]} posts imported in this run. {reason} '
                'Local replay uses no X requests; Update resumes unresolved work using the X API.'
            ) from None


def re_full_id(value):
    # Same ID policy as the import boundary; also safe for recovery bounds.
    return re.fullmatch(r'[1-9][0-9]{0,29}', value)
