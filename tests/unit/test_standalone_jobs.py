import json
import os
import signal
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import Mock

import pytest
from cherrypy.process.wspbus import Bus

import pmxbot
from pmxbot import core, images, jobs
from pmxbot.music import MusicLibrary, generate_album_image
from tests.unit.test_album_page import page, variation  # noqa: F401
from tests.unit.test_images import config, post, r2, responses_post  # noqa: F401


def music_job(config):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album', genre='Jazz', description='ignored')
    store = jobs.JobStore(cache.database)
    identifier = store.enqueue_music(
        library.get_album(album['id']), cache, 'alice', '#music'
    )
    return cache, store, identifier


@pytest.mark.parametrize(
    'irc,web,expected',
    [
        (False, False, ()),
        (True, False, ('music',)),
        (False, True, ('variation',)),
        (True, True, ('music', 'variation')),
    ],
)
def test_switches(config, irc, web, expected):
    config.update(irc_generation_standalone=irc, web_generation_standalone=web)
    if expected:
        assert jobs.standalone_kinds(config) == expected
    else:
        with pytest.raises(ValueError):
            jobs.standalone_kinds(config)


def test_worker_requires_images(config):
    config.update(images_enabled=False, irc_generation_standalone=True)
    with pytest.raises(ValueError):
        jobs.standalone_kinds(config)


def test_music_routing_is_fast_and_does_not_use_local_queue(config, post, monkeypatch):
    config['irc_generation_standalone'] = True
    thread = Mock()
    monkeypatch.setattr(images.threading, 'Thread', thread)
    images._busy.acquire()  # Local !image activity does not block durable submission.
    result = images._start_image(
        None, '#music', 'alice', None, album_data={'artist': 'Band', 'title': 'Album'}
    )
    assert 'queued' in result
    post.assert_not_called()
    thread.assert_not_called()
    cache = images.ImageCache(config)
    store = jobs.JobStore(cache.database)
    claimed = store.claim(('music',))
    assert claimed['requested_by'] == 'alice'
    assert claimed['channel'] == '#music'
    assert claimed['source_image_id'] == 0
    assert store.visible() == []
    inputs = json.loads(claimed['inputs_json'])
    assert inputs['album']['genre'] is None
    assert 'openai-secret' not in claimed['settings_json']


@pytest.mark.parametrize('api', ['images', 'responses'])
def test_music_persistence_settings_and_restart_delivery(
    config, post, r2, request, api
):
    if api == 'responses':
        request.getfixturevalue('responses_post')
    config['image_api'] = api
    cache, store, identifier = music_job(config)
    frozen = json.loads(store.get(identifier)['inputs_json'])
    config['images_quality'] = 'high'
    # Later edits to album properties cannot change the queued IRC prompt.
    with sqlite3.connect(cache.database) as db:
        db.execute("UPDATE albums SET title = 'Changed' WHERE id = 1")
    jobs.process_job(store, store.claim(('music',)), config)
    done = jobs.JobStore(cache.database).get(identifier)
    assert done['status'] == 'succeeded'
    image = cache.get_image(done['result_image_id'])
    assert image['prompt'] == frozen['prompt']
    assert image['requested_by'] == 'alice' and image['channel'] == '#music'
    metadata = json.loads(image['generation_metadata_json'])
    assert metadata['generation_inputs'] == dict(
        album_id=1,
        artist_id=MusicLibrary(cache.database).get_album(1)['artist_id'],
        source_image_id=None,
        artist_name=frozen['album']['artist_name'],
        title=frozen['album']['title'],
        **dict.fromkeys(
            (
                'genre',
                'format',
                'format_description',
                'description',
                'artist_genre',
                'artist_description',
            ),
            '',
        ),
    )
    assert metadata['generation_request'] == post.call_args.kwargs['json']
    if api == 'responses':
        payload = post.call_args.kwargs['json']
        assert payload['input'] == frozen['prompt']
        assert 'previous_response_id' not in payload
        assert payload['tools'][0]['quality'] == 'low'
    else:
        assert post.call_args.kwargs['json']['quality'] == 'low'
    # Reconstructing the store simulates a bot restart; no in-memory tracking needed.
    output = list(images.durable_music_results())
    assert output == [
        '#music',
        f"alice: #1 {image['hosted_url']}",
        'https://bot.example.com/albums/1',
    ]
    assert list(images.durable_music_results()) == []
    assert jobs.JobStore(cache.database).get(identifier)['delivered_at']


@pytest.mark.parametrize('failure', ['provider', 'hosting'])
def test_music_failure_safe_notification(config, post, r2, failure):
    cache, store, identifier = music_job(config)
    (post if failure == 'provider' else r2.put_object).side_effect = RuntimeError(
        'secret'
    )
    jobs.process_job(store, store.claim(('music',)), config)
    done = store.get(identifier)
    assert done['status'] == 'failed' and done['result_image_id'] is None
    assert 'secret' not in done['error']
    assert list(images.durable_music_results()) == ['#music', 'alice: ' + jobs.FAILURE]


def test_existing_music_returns_artwork_without_queueing(config, post, r2):
    cache, store, identifier = music_job(config)
    library = MusicLibrary(cache.database)
    url = generate_album_image(library, cache, 1, 'original', '#music')
    # A different prompt and backend settings must not regenerate this release.
    config.update(irc_generation_standalone=True, images_quality='high')
    post.reset_mock()
    r2.put_object.reset_mock()
    post.side_effect = RuntimeError('provider unavailable')
    result = images._start_image(
        None, '#music', 'alice', None, album_data={'artist': 'Band', 'title': 'Album'}
    )
    assert result == (
        f'Band: Band; Album: Album; #1 {url}\nhttps://bot.example.com/albums/1'
    )
    assert store.claim(('music',))['id'] == identifier
    assert store.claim(('music',)) is None
    post.assert_not_called()
    r2.put_object.assert_not_called()


def test_queued_music_reuses_artwork_saved_before_execution(config, post, r2):
    cache, store, identifier = music_job(config)
    url = generate_album_image(MusicLibrary(cache.database), cache, 1)
    image = cache.latest_album_image_record(1)
    post.reset_mock()
    r2.put_object.reset_mock()
    post.side_effect = RuntimeError('provider unavailable')
    jobs.process_job(store, store.claim(('music',)), config)
    done = store.get(identifier)
    assert done['status'] == 'succeeded'
    assert done['result_image_id'] == image['id']
    assert list(images.durable_music_results()) == [
        '#music',
        f'alice: #1 {url}',
        'https://bot.example.com/albums/1',
    ]
    post.assert_not_called()
    r2.put_object.assert_not_called()


def test_pending_music_dedup_and_concurrent_delivery(config):
    cache, store, identifier = music_job(config)
    album = MusicLibrary(cache.database).get_album(1)
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(
            pool.map(
                lambda _: store.enqueue_music(album, cache, 'alice', '#music'), range(6)
            )
        )
    assert set(ids) == {identifier}
    store.claim(('music',))
    store.finish(identifier, error=jobs.FAILURE)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: store.take_irc_result(), range(6)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executor locking')
def test_mixed_modes_and_scoped_recovery(config):
    cache, store, identifier = music_job(config)
    music = store.claim(('music',))
    # A variation executor must not recover or claim IRC work.
    with jobs.executor_session(config, ('variation',)):
        assert store.get(identifier)['status'] == 'running'
        with jobs.executor_session(config, ('music',)):
            assert store.get(identifier)['status'] == 'failed'
            assert store.get(identifier)['error'] == jobs.ABANDONED
            with pytest.raises(RuntimeError, match='already running'):
                with jobs.executor_session(config, ('music', 'variation')):
                    pass
    assert music['kind'] == 'music'
    # Failed acquisition of the shared owner must not retain its first lock.
    with jobs.executor_session(config, ('music', 'variation')):
        pass


def test_standalone_web_disables_cherrypy_executor(config, monkeypatch):
    config['web_generation_standalone'] = True
    session = Mock()
    monkeypatch.setattr(jobs, 'executor_session', session)
    executor = jobs.GenerationExecutor(Bus(), config)
    executor.start()
    executor.stop()
    session.assert_not_called()


def test_web_standalone_keeps_inline_ui_and_hides_irc_jobs(variation):
    cache, request, post, client, values, source = variation
    pmxbot.config['web_generation_standalone'] = True
    submitted = request(method='POST', data=values, accept='application/json')
    assert submitted['status'] == 202
    post.assert_not_called()
    store = jobs.JobStore(cache.database)
    album = MusicLibrary(cache.database).get_album(42)
    irc_id = store.enqueue_music(album, cache, 'alice', '#private')
    assert request('/jobs/' + irc_id)['status'] == 404
    jobs.process_job(store, store.claim(('variation',)), pmxbot.config)
    web_id = json.loads(submitted['body'])['id']
    assert store.get(web_id)['status'] == 'succeeded'
    assert store.get(irc_id)['status'] == 'queued'


def test_cli_configuration_and_signal_cleanup(config, monkeypatch):
    config.update(irc_generation_standalone=True, web_generation_standalone=True)
    monkeypatch.setattr(core, 'get_args', lambda: Mock(config=config))
    handlers = {}
    registrations = []

    def register(sig, handler):
        registrations.append((sig, handler))
        old = handlers.get(sig, 'original')
        handlers[sig] = handler
        return old

    monkeypatch.setattr(signal, 'signal', register)

    def serve(snapshot, stop):
        assert snapshot['_generation_job_kinds'] == ('music', 'variation')
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert stop.is_set()

    monkeypatch.setattr(jobs, 'serve', serve)
    jobs.run()
    assert handlers == {signal.SIGTERM: 'original', signal.SIGINT: 'original'}


def test_additive_schema_migration(config):
    cache = images.ImageCache(config)
    store = jobs.JobStore(cache.database)
    with sqlite3.connect(cache.database) as db:
        db.execute(
            """CREATE TABLE generation_jobs (
            id TEXT PRIMARY KEY, album_id INTEGER, source_image_id INTEGER,
            inputs_json TEXT, settings_json TEXT, user_id INTEGER,
            requested_by TEXT, anonymous_owner TEXT, created_at TEXT,
            started_at TEXT, completed_at TEXT, status TEXT,
            result_image_id INTEGER, error TEXT)"""
        )
        db.execute(
            "INSERT INTO generation_jobs (id, status, created_at) VALUES (?, 'queued', 'original')",
            ('a' * 32,),
        )
    with ThreadPoolExecutor(max_workers=4) as pool:

        def migrate(_):
            with closing(store.connect()):
                pass

        list(pool.map(migrate, range(4)))
    row = store.get('a' * 32)
    assert row['kind'] == 'variation' and row['channel'] == ''
    assert row['status'] == 'queued' and row['created_at'] == 'original'
    assert row['delivered_at'] is None


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executor locking')
def test_shared_standalone_pool_drains_and_preserves_queue(variation, monkeypatch):
    cache, request, post, client, values, source = variation
    store = jobs.JobStore(cache.database)
    # Submit one web and one IRC job, then a second IRC job that must stay queued.
    submitted = request(method='POST', data=values, accept='application/json')
    web_id = json.loads(submitted['body'])['id']
    album = MusicLibrary(cache.database).get_album(42)
    irc_id = store.enqueue_music(album, cache, 'alice', '#music')
    queued_id = store.enqueue_music(album, cache, 'bob', '#music')
    # Deterministically order the jobs irrespective of UUIDs and clock resolution.
    with closing(store.connect()) as db:
        for index, identifier in enumerate((web_id, irc_id, queued_id)):
            db.execute(
                'UPDATE generation_jobs SET created_at = ? WHERE id = ?',
                (str(index), identifier),
            )
    stop, both, release = threading.Event(), threading.Event(), threading.Event()
    seen = []
    guard = threading.Lock()

    def blocked(store, job, config):
        with guard:
            seen.append(job['kind'])
            if len(seen) == 2:
                both.set()
        assert release.wait(10)
        store.finish(job['id'], error=jobs.FAILURE)

    monkeypatch.setattr(jobs, 'process_job', blocked)
    config = dict(
        pmxbot.config,
        generation_worker_concurrency=2,
        _generation_job_kinds=('music', 'variation'),
    )
    worker = threading.Thread(target=jobs.serve, args=(config, stop))
    worker.start()
    try:
        assert both.wait(10)
        assert sorted(seen) == ['music', 'variation']
        assert store.get(queued_id)['status'] == 'queued'
        stop.set()
        assert worker.is_alive()  # Shutdown waits for the active jobs.
        with pytest.raises(RuntimeError, match='already running'):
            with jobs.executor_session(config, ('music', 'variation')):
                pass
    finally:
        stop.set()
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert store.get(web_id)['status'] == 'failed'
    assert store.get(irc_id)['status'] == 'failed'
    assert store.get(queued_id)['status'] == 'queued'
    with jobs.executor_session(config, ('music', 'variation')):
        assert store.get(queued_id)['status'] == 'queued'
