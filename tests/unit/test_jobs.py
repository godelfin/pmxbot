import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from unittest.mock import Mock

import pytest
from cherrypy.process.wspbus import Bus

import pmxbot
from pmxbot.jobs import (
    ABANDONED,
    GenerationExecutor,
    JobStore,
    process_job,
    serve,
    worker_lock,
)
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


@pytest.mark.skipif(os.name == 'nt', reason='Enabled generation uses POSIX file locks')
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


@pytest.mark.skipif(os.name == 'nt', reason='Enabled generation uses POSIX file locks')
def test_slow_worker_does_not_block_http(variation):
    cache, request, post, client, values, source = variation
    entered, release = threading.Event(), threading.Event()
    response = post.return_value

    def slow_provider(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return response

    post.side_effect = slow_provider
    submitted = request(method='POST', data=values)
    assert submitted['status'] == 303
    post.assert_not_called()
    bus = Bus()
    executor = GenerationExecutor(bus, pmxbot.config)
    executor.subscribe()
    bus.publish('start')
    try:
        assert entered.wait(10)
        target = submitted['headers']['Location'][4:]
        pending = request(target)
        assert 'Artwork is being generated' in pending['body']
        assert 'http-equiv="refresh"' in pending['body']
        assert request()['status'] == 200
        assert request('/jobs')['status'] == 200
    finally:
        release.set()
        bus.publish('stop')
        executor.unsubscribe()
    assert executor._thread is None
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
    executor = GenerationExecutor(
        Bus(), dict(pmxbot.config, generation_worker_concurrency=concurrency)
    )
    with pytest.raises(ValueError, match='concurrency'):
        executor.start()
    assert executor._thread is None


@pytest.mark.skipif(os.name == 'nt', reason='Enabled generation uses POSIX file locks')
def test_worker_bounds_active_jobs(variation, monkeypatch):
    from pmxbot import jobs

    cache, *_ = variation
    store = JobStore(cache.database)
    for description in ['one', 'two', 'three']:
        store.enqueue(replace(draft(cache), description=description), cache)
    entered, release = threading.Event(), threading.Event()
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
    bus = Bus()
    executor = GenerationExecutor(
        bus, dict(pmxbot.config, generation_worker_concurrency=2)
    )
    executor.subscribe()
    bus.publish('start')
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
        release.set()
        bus.publish('stop')
        executor.unsubscribe()
    assert executor._thread is None


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


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_inline_submission_and_polling(variation):
    cache, request, post, client, values, source = variation
    base = pmxbot.config.web_base
    submitted = request(method='POST', data=values, accept='application/json')
    assert submitted['status'] == 202
    assert 'Location' not in submitted['headers']
    job = json.loads(submitted['body'])
    assert job['status_url'] == f"{base}/jobs/{job['id']}"
    post.assert_not_called()
    client.put_object.assert_not_called()
    target = job['status_url'][len(base) :]
    queued = json.loads(request(target, accept='application/json')['body'])
    assert queued['status'] == 'queued'
    assert queued['result_url'] is None
    assert 'inputs_json' not in queued
    # Returning to the album resumes the same placeholder/polling job.
    body = request()['body']
    assert f'data-status-url="{job["status_url"]}"' in body
    assert 'generation-spinner' in body
    store = JobStore(cache.database)
    claimed = store.claim()
    assert (
        json.loads(request(target, accept='application/json')['body'])['status']
        == 'running'
    )
    process_job(store, claimed, pmxbot.config)
    ready = json.loads(request(target, accept='application/json')['body'])
    assert ready['status'] == 'succeeded'
    assert (
        ready['result_url']
        == f"{base}/albums/42?source_image_id={store.get(job['id'])['result_image_id']}"
    )


def test_inline_validation_and_failure(variation):
    cache, request, post, client, values, source = variation
    invalid = request(
        method='POST', data=dict(values, genre='invalid'), accept='application/json'
    )
    assert invalid['status'] == 400
    assert json.loads(invalid['body'])['error']
    assert request('/jobs')['body'].count('/bot/jobs/') == 0
    post.side_effect = RuntimeError('secret provider detail')
    submitted = request(method='POST', data=values, accept='application/json')
    target = json.loads(submitted['body'])['status_url'][4:]
    store = JobStore(cache.database)
    process_job(store, store.claim(), pmxbot.config)
    failed = request(target, accept='application/json')
    assert json.loads(failed['body'])['status'] == 'failed'
    assert json.loads(failed['body'])['result_url'] is None
    assert 'secret' not in failed['body']


@pytest.mark.skipif(os.name == 'nt', reason='Enabled generation uses POSIX file locks')
def test_engine_start_once_and_graceful_stop(variation):
    cache, request, post, client, values, source = variation
    entered, release = threading.Event(), threading.Event()
    response = post.return_value

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return response

    post.side_effect = blocked
    store = JobStore(cache.database)
    first = store.enqueue(draft(cache), cache)
    bus = Bus()
    executor = GenerationExecutor(bus, pmxbot.config)
    executor.subscribe()
    shutdown = None
    try:
        bus.publish('start')
        assert entered.wait(10)
        thread = executor._thread
        bus.publish('start')
        assert executor._thread is thread
        assert post.call_count == 1
        queued = store.enqueue(replace(draft(cache), description='waiting'), cache)
        shutdown = threading.Thread(target=lambda: bus.publish('stop'))
        shutdown.start()
        assert executor._stop.wait(10)
        assert shutdown.is_alive()  # Shutdown must wait for active generation.
        assert store.get(queued)['status'] == 'queued'
        other = GenerationExecutor(Bus(), pmxbot.config)
        with pytest.raises(RuntimeError, match='already running'):
            other.start()
        assert store.get(first)['status'] == 'running'
    finally:
        release.set()
        if shutdown is not None:
            shutdown.join(10)
        executor.stop()
        executor.unsubscribe()
    assert store.get(first)['status'] == 'succeeded'
    assert store.get(queued)['status'] == 'queued'
    assert executor._thread is None
    assert executor.start not in bus.listeners['start']
    # A subsequent engine start processes the queued job, without retrying first.
    entered.clear()
    executor.start()
    try:
        assert entered.wait(10)
    finally:
        executor.stop()
    assert store.get(queued)['status'] == 'succeeded'
    assert post.call_count == 2


@pytest.mark.skipif(os.name == 'nt', reason='Enabled generation uses POSIX file locks')
def test_scheduler_failure_requires_explicit_restart(variation, monkeypatch):
    from pmxbot import jobs

    cache, request, post, client, values, source = variation
    store = JobStore(cache.database)
    first = store.enqueue(draft(cache), cache)
    queued = store.enqueue(replace(draft(cache), description='later'), cache)
    # Claim a known job, then simulate loss of completion persistence.
    entered = threading.Event()

    def broken(config, stop, store, concurrency):
        with closing(store.connect()) as db:
            db.execute(
                "UPDATE generation_jobs SET status = 'running' WHERE id = ?", (first,)
            )
        raise RuntimeError('secret database detail')

    monkeypatch.setattr(jobs, 'process_queue', broken)
    bus = Bus()
    bus.log = Mock()
    executor = GenerationExecutor(bus, pmxbot.config)
    executor.start()
    try:
        executor._thread.join(10)
        assert not executor._thread.is_alive()
        assert store.get(first)['status'] == 'running'
        assert 'secret' not in str(bus.log.call_args)
        executor.start()  # Repeated engine notifications do not retry/recover.
        assert store.get(first)['status'] == 'running'
        other = GenerationExecutor(Bus(), pmxbot.config)
        with pytest.raises(RuntimeError, match='already running'):
            other.start()
    finally:
        executor.stop()

    def resumed(config, stop, store, concurrency):
        entered.set()
        stop.wait(10)

    monkeypatch.setattr(jobs, 'process_queue', resumed)
    executor.start()
    try:
        assert entered.wait(10)
        assert store.get(first)['status'] == 'failed'
        assert store.get(first)['error'] == ABANDONED
        assert store.get(queued)['status'] == 'queued'
    finally:
        executor.stop()
    post.assert_not_called()


def test_disabled_engine_does_not_create_storage(page):
    cache, request = page
    bus = Bus()
    executor = GenerationExecutor(bus, pmxbot.config)
    executor.subscribe()
    try:
        bus.publish('start')
        bus.publish('start')
        bus.publish('stop')
        assert executor._thread is None
        assert not cache.database.exists()
    finally:
        executor.unsubscribe()


@pytest.mark.skipif(os.name == 'nt', reason='Enabled generation uses POSIX file locks')
def test_startup_recovery_failure_releases_lock(variation, monkeypatch):
    original = JobStore.recover
    monkeypatch.setattr(
        JobStore, 'recover', Mock(side_effect=RuntimeError('recovery failed'))
    )
    executor = GenerationExecutor(Bus(), pmxbot.config)
    with pytest.raises(RuntimeError, match='recovery failed'):
        executor.start()
    assert executor._thread is None
    monkeypatch.setattr(JobStore, 'recover', original)
    executor.start()
    executor.stop()


@pytest.mark.parametrize('failure', [False, True])
def test_viewer_owns_executor_hooks(monkeypatch, failure):
    import cherrypy
    from pmxbot.web import viewer

    monkeypatch.setattr(pmxbot, 'config', {})
    monkeypatch.setattr(viewer, '_setup_logging', lambda: None)
    monkeypatch.setattr(pmxbot.core, '_load_library_extensions', lambda: None)
    executor = Mock()
    factory = Mock(return_value=executor)
    monkeypatch.setattr(viewer, 'GenerationExecutor', factory)

    def quickstart(*args, **kwargs):
        executor.subscribe.assert_called_once_with()
        executor.stop.assert_not_called()
        if failure:
            raise RuntimeError('startup failed')

    monkeypatch.setattr(cherrypy, 'quickstart', quickstart)
    if failure:
        with pytest.raises(RuntimeError, match='startup failed'):
            viewer.startup({'web_base': '/bot'})
    else:
        viewer.startup({'web_base': '/bot'})
    factory.assert_called_once_with(cherrypy.engine, pmxbot.config)
    executor.stop.assert_called_once_with()
    executor.unsubscribe.assert_called_once_with()
