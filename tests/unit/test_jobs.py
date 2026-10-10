import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from unittest.mock import Mock

import pytest

import pmxbot
from pmxbot.jobs import ABANDONED, JobStore, process_job, serve, worker_lock
from pmxbot.music import GenerationInputs
from pmxbot.users import User
from tests.unit.test_album_page import page, variation  # noqa: F401


def draft(cache):
    return GenerationInputs.from_source(*cache.get_album_page(42, 42))


def test_pending_reuse_and_snapshot(variation):
    cache, request, post, client, values, source = variation
    store = JobStore(cache.database)
    first = request(method='POST', data=values)
    second = request(method='POST', data=values)
    assert first['headers']['Location'] == second['headers']['Location']
    job = store.get(first['headers']['Location'].rsplit('/', 1)[1])
    assert job['status'] == 'queued'
    assert job['created_at'] and job['started_at'] is None
    assert json.loads(job['inputs_json'])['description'] == values['description']
    assert 'openai_api_key' not in job['settings_json']
    pmxbot.config['images_quality'] = 'high'
    claimed = store.claim()
    assert store.get(job['id'])['status'] == 'running'
    process_job(store, claimed, pmxbot.config)
    assert post.call_args.kwargs['data']['quality'] == 'low'
    ready = store.get(job['id'])
    assert ready['status'] == 'succeeded'
    assert ready['started_at'] and ready['completed_at']
    status = request(first['headers']['Location'][4:])
    assert (
        f"/bot/albums/42?source_image_id={ready['result_image_id']}" in status['body']
    )
    assert 'http-equiv="refresh"' not in status['body']


def test_atomic_concurrent_claims_and_enqueue(variation):
    cache, *_ = variation
    inputs = draft(cache)
    store = JobStore(cache.database)
    with ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(
            pool.map(
                lambda _: store.enqueue(inputs, cache, anonymous_owner='owner'),
                range(8),
            )
        )
    assert len(set(identifiers)) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda _: store.claim(), range(8)))
    assert sum(job is not None for job in jobs) == 1
    assert JobStore(cache.database).get(identifiers[0])['status'] == 'running'


def test_recovery_never_retries(variation):
    cache, request, post, client, values, source = variation
    store = JobStore(cache.database)
    running = store.enqueue(draft(cache), cache)
    store.claim()
    queued = store.enqueue(replace(draft(cache), description='different'), cache)
    restarted = JobStore(cache.database)
    restarted.recover()
    assert restarted.get(running)['status'] == 'failed'
    assert restarted.get(running)['error'] == ABANDONED
    assert restarted.get(queued)['status'] == 'queued'
    assert restarted.claim()['id'] == queued
    assert restarted.claim() is None
    retry = restarted.enqueue(draft(cache), cache)
    assert retry != running
    post.assert_not_called()


@pytest.mark.skipif(os.name == 'nt', reason='Production worker uses POSIX file locks')
def test_worker_lock_prevents_live_recovery(variation):
    import subprocess
    import sys

    cache, *_ = variation
    with worker_lock(cache.database):
        code = 'from pathlib import Path; from pmxbot.jobs import worker_lock; import sys\nwith worker_lock(Path(sys.argv[1])): pass'
        result = subprocess.run(
            [sys.executable, '-c', code, str(cache.database)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert 'already running' in result.stderr
    with worker_lock(cache.database):
        pass

    # Kernel release also works when the process exits without context cleanup.
    code = (
        'from pathlib import Path; from pmxbot.jobs import worker_lock; import sys, os\n'
        'with worker_lock(Path(sys.argv[1])): os._exit(7)'
    )
    result = subprocess.run([sys.executable, '-c', code, str(cache.database)])
    assert result.returncode == 7
    with worker_lock(cache.database):
        pass


@pytest.mark.skipif(os.name == 'nt', reason='Production worker uses POSIX file locks')
def test_slow_worker_does_not_block_http(variation):
    cache, request, post, client, values, source = variation
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    response = post.return_value

    def slow_provider(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return response

    post.side_effect = slow_provider
    submitted = request(method='POST', data=values)
    assert submitted['status'] == 303
    post.assert_not_called()
    worker = threading.Thread(target=serve, args=(dict(pmxbot.config), stop))
    worker.start()
    try:
        assert entered.wait(10)
        target = submitted['headers']['Location'][4:]
        pending = request(target)
        assert 'Artwork is being generated' in pending['body']
        assert 'http-equiv="refresh"' in pending['body']
        assert request()['status'] == 200
        assert request('/jobs')['status'] == 200
    finally:
        stop.set()
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert 'Artwork is ready' in request(target)['body']


def test_ownership_visibility_and_capability(variation, monkeypatch):
    from pmxbot.web import auth

    cache, request, post, client, values, source = variation
    alice = User(1, 'Alice', 'alice', None, '2026', True, False)
    bob = replace(alice, id=2, username='Bob')
    monkeypatch.setattr(auth, 'current_user', Mock(return_value=alice))
    submitted = request(method='POST', data=values)
    target = submitted['headers']['Location'][4:]
    store = JobStore(cache.database)
    job = store.get(target.rsplit('/', 1)[1])
    assert job['user_id'] == alice.id
    assert job['requested_by'] == alice.username
    assert target in request('/jobs')['body']
    assert request(target)['status'] == 200
    monkeypatch.setattr(auth, 'current_user', Mock(return_value=bob))
    assert request(target)['status'] == 404
    assert target not in request('/jobs')['body']
    monkeypatch.setattr(auth, 'current_user', Mock(return_value=None))
    assert request(target)['status'] == 404
    anonymous = request(method='POST', data=values)
    status = request(anonymous['headers']['Location'][4:])
    assert status['status'] == 200
    assert 'Waiting for generation' in status['body']
    assert values['description'] not in status['body']
    assert status['headers']['Referrer-Policy'] == 'no-referrer'


@pytest.mark.parametrize('concurrency', [0, 5, True, '2'])
def test_invalid_concurrency(variation, concurrency):
    with pytest.raises(ValueError, match='concurrency'):
        serve(
            dict(pmxbot.config, generation_worker_concurrency=concurrency),
            threading.Event(),
        )


@pytest.mark.skipif(os.name == 'nt', reason='Production worker uses POSIX file locks')
def test_worker_bounds_active_jobs(variation, monkeypatch):
    from pmxbot import jobs

    cache, *_ = variation
    store = JobStore(cache.database)
    for description in ['one', 'two', 'three']:
        store.enqueue(replace(draft(cache), description=description), cache)
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    guard = threading.Lock()
    active = []

    def blocked(store, job, config):
        with guard:
            active.append(job['id'])
            if len(active) == 2:
                entered.set()
        assert release.wait(10)
        store.finish(job['id'], error='test failure')

    monkeypatch.setattr(jobs, 'process_job', blocked)
    worker = threading.Thread(
        target=serve, args=(dict(pmxbot.config, generation_worker_concurrency=2), stop)
    )
    worker.start()
    try:
        assert entered.wait(10)
        with closing(store.connect()) as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM generation_jobs WHERE status = 'running'"
                ).fetchone()[0]
                == 2
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM generation_jobs WHERE status = 'queued'"
                ).fetchone()[0]
                == 1
            )
        assert len(active) == 2
    finally:
        stop.set()
        release.set()
        worker.join(10)
    assert not worker.is_alive()


def test_invalid_status_requests(page):
    cache, request = page
    assert request('/jobs')['status'] == 200
    assert request('/jobs/no')['status'] == 404
    assert request('/jobs/' + 'a' * 32)['status'] == 404
    assert request('/jobs', method='POST')['status'] == 405
    assert not cache.database.exists()


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_mount_paths(variation):
    cache, request, post, client, values, source = variation
    base = pmxbot.config.web_base
    submitted = request(method='POST', data=values)
    target = submitted['headers']['Location']
    assert target.startswith(base + '/jobs/')
    status = request(target[len(base) :])
    assert status['status'] == 200
    assert f'{base}/albums/42?source_image_id=42' in status['body']
    assert f'{base}/jobs/' in request('/jobs')['body']
    post.assert_not_called()
