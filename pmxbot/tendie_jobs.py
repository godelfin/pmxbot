"""Bounded web synchronization using the viewer's background-plugin conventions."""

import contextlib
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from cherrypy.process.plugins import SimplePlugin

from . import tendie

log = logging.getLogger(__name__)
ACTIVE = ('queued', 'running')


def open_store(config):
    return tendie.Collection(
        config.get('database', 'sqlite:pmxbot.sqlite'), config.get('tendie_author')
    )


def active(job):
    return (
        job.get('status') in ACTIVE
        and (tendie.now() - tendie.timestamp(job['submitted_at'])).total_seconds()
        < 1800
    )


def leased(store):
    started = store.meta('sync_started')
    return bool(
        started and (tendie.now() - tendie.timestamp(started)).total_seconds() < 1800
    )


def status(config):
    """Only return public administration fields, never saved settings or tokens."""
    with contextlib.closing(open_store(config)) as store:
        job = json.loads(store.meta('web_job') or '{}')
        if job.get('status') in ACTIVE and not active(job) and not leased(store):
            job = dict(
                status='failed',
                message='Previous job stopped reporting progress. Replay saved pages locally before authorizing more X requests.',
            )
        return dict(
            source=store.author,
            count=store.size(),
            last_success=store.meta('last_success'),
            last_live_success=store.meta('last_live_success'),
            page_limit=job.get('page_limit', 1),
            progress=store.progress(),
            job=dict(
                status=job.get('status', 'idle'),
                message=job.get('message', 'No update requested.'),
            ),
        )


class TendieExecutor(SimplePlugin):
    """One provider call at a time; durable per-account claims cover other processes."""

    def __init__(self, bus):
        super().__init__(bus)
        self._lock = threading.Lock()
        self._pool = None
        self._active = False

    def start(self):
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix='pmxbot-tendie'
                )

    def stop(self):
        with self._lock:
            pool, self._pool = self._pool, None
        if pool:
            pool.shutdown(wait=True)

    def enqueue(self, config, operation='update', page_limit=1):
        page_limit = tendie.validated_page_limit(page_limit)
        if operation not in ('update', 'recover', 'recover_range'):
            raise ValueError('Invalid tendie operation')
        config = tendie.settings_snapshot(config)
        with self._lock:
            if self._pool is None:
                raise tendie.SyncError('Tendie background worker is unavailable.')
            if self._active:
                return False
            identifier = uuid.uuid4().hex
            with contextlib.closing(open_store(config)) as store:
                with store.db:
                    store.db.execute('BEGIN IMMEDIATE')
                    job = json.loads(store.meta('web_job') or '{}')
                    if active(job) or leased(store):
                        return False
                    store.set_meta(
                        'web_job',
                        json.dumps(
                            dict(
                                id=identifier,
                                page_limit=page_limit,
                                status='queued',
                                message=(
                                    'Local replay queued (no X requests).'
                                    if operation == 'recover'
                                    else 'Update queued.'
                                ),
                                submitted_at=tendie.now().isoformat(),
                            )
                        ),
                    )
            self._active = True
            try:
                self._pool.submit(self._run, config, identifier, operation, page_limit)
            except Exception:
                self._active = False
                self._record(
                    config,
                    identifier,
                    'failed',
                    'Could not start tendie update. Please try again.',
                )
                raise tendie.SyncError(
                    'Could not start tendie update. Please try again.'
                ) from None
        return True

    @staticmethod
    def _record(config, identifier, state, message):
        with contextlib.closing(open_store(config)) as store:
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                job = json.loads(store.meta('web_job') or '{}')
                if job.get('id') != identifier:
                    return False
                job.update(status=state, message=message)
                store.set_meta('web_job', json.dumps(job))
                return True

    def _run(self, config, identifier, operation, page_limit):
        try:
            message = (
                'Replaying saved pages locally (no X requests)…'
                if operation == 'recover'
                else 'Updating tendies…'
            )
            if not self._record(config, identifier, 'running', message):
                return
            try:
                count = tendie.synchronize(
                    config, operation=operation, page_limit=page_limit
                )
            except Exception as exc:
                state, message = 'failed', tendie.synchronization_error(exc)
                log.error('Tendie synchronization failed (%s)', type(exc).__name__)
            else:
                with contextlib.closing(open_store(config)) as store:
                    progress = store.progress()
                incomplete = progress and progress['status'] != 'complete'
                state = 'incomplete' if incomplete else 'succeeded'
                message = f'Imported {count} posts.'
                if incomplete:
                    message += f' Run incomplete; {progress["saved_responses"]} responses saved and {progress["imported"]} posts imported in this run.'
                    if operation != 'recover':
                        message += ' ' + progress['message']
                if operation == 'recover':
                    message += ' Local replay made no X requests.'
                    if incomplete:
                        message += ' Unresolved pages need an error-free API response.'
            self._record(config, identifier, state, message)
        except Exception as exc:
            log.error('Could not record tendie update (%s)', type(exc).__name__)
        finally:
            with self._lock:
                self._active = False
