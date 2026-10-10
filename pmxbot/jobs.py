"""Durable web generation jobs and a separate, bounded worker process."""

import json
import logging
import re
import signal
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict

import pmxbot
from .images import ImageCache
from .music import GenerationInputs, MusicLibrary, generate_album_variation


log = logging.getLogger(__name__)
FAILURE = 'Generation failed. Check worker logs or contact the administrator.'
ABANDONED = 'Worker stopped before recording completion. No automatic retry was made.'


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
        return db

    def enqueue(self, inputs, cache, user=None, anonymous_owner=''):
        """Reuse exact pending requests within an owner; F34 image lookup is separate.

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
        inputs_json = json.dumps(asdict(inputs), sort_keys=True)
        settings_json = json.dumps(settings, sort_keys=True)
        user_id = user.id if user else None
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute(
                    '''SELECT id FROM generation_jobs
                    WHERE inputs_json = ? AND settings_json = ?
                    AND user_id IS ? AND anonymous_owner = ?
                    AND status IN ('queued', 'running') LIMIT 1''',
                    (inputs_json, settings_json, user_id, anonymous_owner),
                ).fetchone()
                identifier = row['id'] if row else uuid.uuid4().hex
                if not row:
                    db.execute(
                        '''INSERT INTO generation_jobs
                        (id, album_id, source_image_id, inputs_json, settings_json,
                         user_id, requested_by, anonymous_owner)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (
                            identifier,
                            inputs.album_id,
                            inputs.source_image_id,
                            inputs_json,
                            settings_json,
                            user_id,
                            user.username if user else '',
                            anonymous_owner,
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

    def claim(self):
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute(
                    '''SELECT * FROM generation_jobs
                    WHERE status = 'queued' ORDER BY created_at, id LIMIT 1'''
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

    def recover(self):
        """Only call while holding the exclusive worker lock."""
        with closing(self.connect()) as db:
            db.execute(
                '''UPDATE generation_jobs SET status = 'failed', error = ?,
                completed_at = CURRENT_TIMESTAMP WHERE status = 'running' ''',
                (ABANDONED,),
            )


def process_job(store, job, config):
    try:
        snapshot = dict(config, **json.loads(job['settings_json']))
        cache = ImageCache(snapshot)
        result = generate_album_variation(
            MusicLibrary(cache.database),
            cache,
            job['source_image_id'],
            GenerationInputs(**json.loads(job['inputs_json'])),
            job['requested_by'],
        )
    except Exception as exc:
        # Provider errors may include credentials, prompts or remote request details.
        log.error('Generation job %s failed (%s)', job['id'], type(exc).__name__)
        store.finish(job['id'], error=FAILURE)
    else:
        store.finish(job['id'], image_id=result['id'])


@contextmanager
def worker_lock(database):
    """POSIX process lock: released by the kernel on crash, never by a timeout.

    All worker processes must use this lock on the same local filesystem.
    The lock file must never be deleted while a worker could be alive.
    """
    import fcntl

    with open(str(database) + '.generation-worker.lock', 'a', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A generation worker is already running.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def serve(config, stop):
    concurrency = config.get('generation_worker_concurrency', 1)
    if type(concurrency) is not int or not 1 <= concurrency <= 4:
        raise ValueError(
            'generation_worker_concurrency must be an integer from 1 to 4.'
        )
    cache = ImageCache(config)
    cache.database.parent.mkdir(parents=True, exist_ok=True)
    store = JobStore(cache.database)
    with worker_lock(cache.database):
        store.recover()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            active = set()
            while not stop.is_set():
                for future in list(active):
                    if future.done():
                        future.result()  # Persistence failures stop the worker for recovery.
                        active.remove(future)
                while len(active) < concurrency and not stop.is_set():
                    job = store.claim()
                    if not job:
                        break
                    active.add(pool.submit(process_job, store, job, config))
                stop.wait(1)
            for future in active:
                future.result()


def run():
    import pmxbot.core

    config = pmxbot.core.init_config(pmxbot.core.get_args().config)
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    serve(config, stop)
