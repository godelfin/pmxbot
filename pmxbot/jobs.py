"""Durable generation jobs with optional standalone and CherryPy executors."""

import json
import logging
import re
import signal
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict

from cherrypy.process.plugins import SimplePlugin
from .images import ImageCache, ImageError
from .music import GenerationInputs, MusicLibrary, generate_album_variation


log = logging.getLogger(__name__)
FAILURE = 'Generation failed. Check generation logs or contact the administrator.'
ABANDONED = 'Generation executor stopped before recording completion. No automatic retry was made.'


class JobStore:
    def __init__(self, database):
        self.database = database

    def connect(self):
        db = sqlite3.connect(str(self.database), timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute(
            '''CREATE TABLE IF NOT EXISTS generation_jobs (
            id TEXT PRIMARY KEY,
            album_id INTEGER NOT NULL,
            source_image_id INTEGER NOT NULL,
            inputs_json TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            user_id INTEGER,
            requested_by TEXT NOT NULL,
            anonymous_owner TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued', 'running', 'succeeded', 'failed')),
            result_image_id INTEGER,
            error TEXT
        )'''
        )
        db.execute(
            '''CREATE INDEX IF NOT EXISTS generation_jobs_pending
            ON generation_jobs(status, created_at)'''
        )
        # Serialize additive migration against other producers on old databases.
        db.execute('BEGIN IMMEDIATE')
        try:
            columns = {
                row['name'] for row in db.execute('PRAGMA table_info(generation_jobs)')
            }
            for name, definition in (
                ('kind', "TEXT NOT NULL DEFAULT 'variation'"),
                ('channel', "TEXT NOT NULL DEFAULT ''"),
                ('delivered_at', 'TEXT'),
                ('operation', "TEXT NOT NULL DEFAULT 'edit'"),
                ('submission_token', 'TEXT'),
            ):
                if name not in columns:
                    db.execute(
                        f'ALTER TABLE generation_jobs ADD COLUMN {name} {definition}'
                    )
            db.execute(
                'CREATE INDEX IF NOT EXISTS generation_jobs_submission '
                'ON generation_jobs(submission_token) WHERE submission_token IS NOT NULL'
            )
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            db.close()
            raise
        return db

    def enqueue_music(self, album, cache, nick, channel):
        # Freeze the IRC-specific prompt, including its omission of album metadata.
        snapshot = {key: album[key] for key in ('id', 'artist_name', 'title')}
        snapshot.update(
            genre=None, description=None, format=None, format_description=None
        )
        from .music import album_prompt

        return self.enqueue(
            snapshot,
            cache,
            requested_by=nick,
            anonymous_owner=f'irc:{channel}:{nick}',
            kind='music',
            channel=channel,
            prompt=album_prompt(snapshot),
        )

    def enqueue(
        self,
        inputs,
        cache,
        user=None,
        anonymous_owner='',
        *,
        kind='variation',
        channel='',
        prompt=None,
        requested_by='',
        operation='edit',
        submission_token=None,
    ):
        """Reuse submission tokens; older internal callers reuse exact pending inputs.

        Snapshots contain generation settings only, never credentials or paths.
        """
        settings = dict(
            image_api=cache.image_api,
            images_model=cache.settings['model'],
            images_size=cache.settings['size'],
            images_quality=cache.settings['quality'],
            responses_model=cache.responses_model,
            responses_store=cache.responses_store,
        )
        inputs_json = json.dumps(
            (
                asdict(inputs)
                if kind == 'variation'
                else {'album': inputs, 'prompt': prompt}
            ),
            sort_keys=True,
        )
        settings_json = json.dumps(settings, sort_keys=True)
        user_id = user.id if user else None
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if submission_token is not None:
                    row = db.execute(
                        'SELECT * FROM generation_jobs WHERE submission_token = ? '
                        'AND user_id IS ? AND anonymous_owner = ?',
                        (submission_token, user_id, anonymous_owner),
                    ).fetchone()
                    if row and (
                        row['inputs_json'] != inputs_json
                        or row['settings_json'] != settings_json
                        or row['operation'] != operation
                        or row['kind'] != kind
                        or row['channel'] != channel
                    ):
                        raise ValueError(
                            'Submission token already used with different inputs.'
                        )
                else:
                    row = db.execute(
                        '''SELECT id FROM generation_jobs
                        WHERE inputs_json = ? AND settings_json = ?
                        AND user_id IS ? AND anonymous_owner = ?
                        AND kind = ? AND channel = ?
                        AND status IN ('queued', 'running') LIMIT 1''',
                        (
                            inputs_json,
                            settings_json,
                            user_id,
                            anonymous_owner,
                            kind,
                            channel,
                        ),
                    ).fetchone()
                identifier = row['id'] if row else uuid.uuid4().hex
                if not row:
                    db.execute(
                        '''INSERT INTO generation_jobs
                        (id, album_id, source_image_id, inputs_json, settings_json,
                         user_id, requested_by, anonymous_owner, kind, channel, operation, submission_token)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (
                            identifier,
                            inputs.album_id if kind == 'variation' else inputs['id'],
                            inputs.source_image_id if kind == 'variation' else 0,
                            inputs_json,
                            settings_json,
                            user_id,
                            user.username if user else requested_by,
                            anonymous_owner,
                            kind,
                            channel,
                            operation,
                            submission_token,
                        ),
                    )
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK')
                raise
        return identifier

    def read_connection(self):
        if not self.database.is_file():
            raise LookupError('Unknown job')
        db = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=20)
        db.row_factory = sqlite3.Row
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'generation_jobs'"
        ).fetchone():
            db.close()
            raise LookupError('Unknown job')
        return db

    def get(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(
            '[0-9a-f]{32}', identifier
        ):
            raise LookupError('Unknown job')
        with closing(self.read_connection()) as db:
            row = db.execute(
                'SELECT * FROM generation_jobs WHERE id = ?', (identifier,)
            ).fetchone()
        if not row:
            raise LookupError('Unknown job')
        return dict(row)

    def visible(self, user=None, anonymous_owner=''):
        with closing(self.read_connection()) as db:
            return [
                dict(row)
                for row in db.execute(
                    '''SELECT * FROM generation_jobs
                WHERE (user_id = ? OR (user_id IS NULL AND anonymous_owner = ?))
                ORDER BY created_at DESC, id DESC LIMIT 100''',
                    (user.id if user else None, anonymous_owner),
                )
            ]

    def claim(self, kinds=('variation', 'music')):
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute(
                    '''SELECT * FROM generation_jobs
                    WHERE status = 'queued' AND kind IN (%s)
                    ORDER BY created_at, id LIMIT 1'''
                    % ','.join('?' for _ in kinds),
                    kinds,
                ).fetchone()
                if row:
                    db.execute(
                        '''UPDATE generation_jobs SET status = 'running',
                        started_at = CURRENT_TIMESTAMP WHERE id = ?''',
                        (row['id'],),
                    )
                    row = db.execute(
                        'SELECT * FROM generation_jobs WHERE id = ?', (row['id'],)
                    ).fetchone()
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK')
                raise
        return dict(row) if row else None

    def finish(self, identifier, image_id=None, error=None):
        with closing(self.connect()) as db:
            db.execute(
                '''UPDATE generation_jobs SET status = ?, result_image_id = ?,
                error = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'running' ''',
                ('failed' if error else 'succeeded', image_id, error, identifier),
            )

    def recover(self, kinds=('variation', 'music')):
        """Only call while holding the exclusive worker lock."""
        with closing(self.connect()) as db:
            db.execute(
                '''UPDATE generation_jobs SET status = 'failed', error = ?,
                completed_at = CURRENT_TIMESTAMP WHERE status = 'running'
                AND kind IN (%s) '''
                % ','.join('?' for _ in kinds),
                (ABANDONED, *kinds),
            )

    def take_irc_result(self):
        """Claim a terminal IRC notification once across bot restarts/processes."""
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute(
                    "SELECT * FROM generation_jobs WHERE kind = 'music' "
                    "AND status IN ('succeeded', 'failed') AND delivered_at IS NULL "
                    "ORDER BY created_at, id LIMIT 1"
                ).fetchone()
                if row:
                    db.execute(
                        'UPDATE generation_jobs SET delivered_at = CURRENT_TIMESTAMP WHERE id = ?',
                        (row['id'],),
                    )
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK')
                raise
        return dict(row) if row else None


def process_job(store, job, config):
    try:
        snapshot = dict(config, **json.loads(job['settings_json']))
        cache = ImageCache(snapshot)
        if job['kind'] == 'music':
            from .music import generate_album_image

            inputs = json.loads(job['inputs_json'])
            try:
                result = cache.latest_album_image_record(job['album_id'])
            except ImageError:
                generate_album_image(
                    MusicLibrary(cache.database),
                    cache,
                    job['album_id'],
                    job['requested_by'],
                    job['channel'],
                    include_metadata=False,
                    snapshot=inputs['album'],
                    exact_prompt=inputs['prompt'],
                )
                with closing(cache.read_connection()) as db:
                    result = db.execute(
                        'SELECT id FROM image_cache WHERE cache_key = ?',
                        (cache.cache_key(inputs['prompt']),),
                    ).fetchone()
        else:
            inputs = GenerationInputs(**json.loads(job['inputs_json']))
            if job.get('operation') == 'create_new':
                from .music import generate_album_new

                result = generate_album_new(
                    MusicLibrary(cache.database), cache, inputs, job['requested_by']
                )
            else:
                result = generate_album_variation(
                    MusicLibrary(cache.database),
                    cache,
                    job['source_image_id'],
                    inputs,
                    job['requested_by'],
                )
    except Exception as exc:
        # Provider errors may include credentials, prompts or remote request details.
        log.error('Generation job %s failed (%s)', job['id'], type(exc).__name__)
        store.finish(job['id'], error=FAILURE)
    else:
        store.finish(job['id'], image_id=result['id'])
        log.info('Generation job %s succeeded (image %s)', job['id'], result['id'])


@contextmanager
def worker_lock(database):
    """POSIX process lock: released by the kernel on crash, never by a timeout.

    All executors must use this lock on the same local filesystem.
    The lock file must never be deleted while a worker could be alive.
    """
    import fcntl

    with open(str(database) + '.generation-worker.lock', 'a', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A generation executor is already running.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def executor_session(config, kinds=('variation',)):
    concurrency = config.get('generation_worker_concurrency', 1)
    if type(concurrency) is not int or not 1 <= concurrency <= 4:
        raise ValueError(
            'generation_worker_concurrency must be an integer from 1 to 4.'
        )
    cache = ImageCache(config)
    cache.database.parent.mkdir(parents=True, exist_ok=True)
    store = JobStore(cache.database)
    with ExitStack() as locks:
        # Acquire in a fixed order. Web keeps the historical lock filename.
        for kind in sorted(kinds):
            database = (
                cache.database if kind == 'variation' else str(cache.database) + '.irc'
            )
            locks.enter_context(worker_lock(database))
        store.recover(kinds)
        log.info(
            'Generation executor started for %s (concurrency %s)',
            ', '.join(kinds),
            concurrency,
        )
        yield store, concurrency


def process_queue(config, stop, store, concurrency):
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        active = set()
        while not stop.is_set():
            for future in list(active):
                if future.done():
                    future.result()  # Persistence failures stop the executor for recovery.
                    active.remove(future)
            while len(active) < concurrency and not stop.is_set():
                job = store.claim(config.get('_generation_job_kinds', ('variation',)))
                if not job:
                    break
                active.add(pool.submit(process_job, store, job, config))
            stop.wait(1)
        for future in active:
            future.result()


def serve(config, stop):
    """Run the same bounded executor outside the CherryPy engine."""
    with executor_session(
        config, config.get('_generation_job_kinds', ('variation',))
    ) as (store, concurrency):
        process_queue(config, stop, store, concurrency)


class GenerationExecutor(SimplePlugin):
    """One executor per CherryPy engine, protected by a database process lock.

    Startup recovery runs synchronously before HTTP serving. The scheduler and
    provider pool are separate from CherryPy's HTTP request threads. Retain the
    lock even if the scheduler fails; only a deliberate web restart recovers it.
    """

    def __init__(self, bus, config):
        super().__init__(bus)
        self.config = dict(config)
        self._lifecycle_lock = threading.Lock()
        self._thread = None
        self._session = None
        self._stop = threading.Event()

    def subscribe(self):
        self.bus.subscribe('start', self.start, priority=30)
        self.bus.subscribe('stop', self.stop, priority=70)

    def start(self):
        with self._lifecycle_lock:
            if (
                self._thread is not None
                or not self.config.get('images_enabled', False)
                or self.config.get('web_generation_standalone', False)
            ):
                return
            session = ExitStack()
            try:
                store, concurrency = session.enter_context(
                    executor_session(self.config)
                )
                self._stop.clear()
                thread = threading.Thread(
                    target=self._run,
                    args=(store, concurrency),
                    name='pmxbot-generation-scheduler',
                )
                thread.start()
            except Exception:
                session.close()
                raise
            self._session = session
            self._thread = thread

    def _run(self, store, concurrency):
        try:
            process_queue(self.config, self._stop, store, concurrency)
        except Exception as exc:
            self.bus.log(
                'Background generation stopped (%s); restart the viewer to recover.'
                % type(exc).__name__,
                level=40,
            )

    def stop(self):
        with self._lifecycle_lock:
            if self._thread is None:
                return
            self._stop.set()
            self._thread.join()
            self._session.close()
            self._thread = None
            self._session = None


def standalone_kinds(config):
    kinds = tuple(
        kind
        for flag, kind in (
            ('irc_generation_standalone', 'music'),
            ('web_generation_standalone', 'variation'),
        )
        if config.get(flag, False)
    )
    if not config.get('images_enabled', False) or not kinds:
        raise ValueError(
            'Enable images_enabled and at least one standalone generation switch.'
        )
    return kinds


def run():
    """Console entry point; SIGTERM/SIGINT stop claims and drain active calls."""
    from . import core

    config = dict(core.get_args().config)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    config['_generation_job_kinds'] = standalone_kinds(config)
    stop = threading.Event()
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda signum, frame: stop.set())
        serve(config, stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
