"""Manual creation uses real HTTP sessions, mocked providers and durable jobs."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

import pmxbot
from pmxbot import creations, images
from pmxbot.jobs import JobStore, process_job
from pmxbot.music import GenerationInputs, MusicLibrary
from pmxbot.users import UserStore
from pmxbot.web import auth
from tests.unit.test_album_page import page, variation  # noqa: F401
from tests.unit.test_generation_auth import CURRENT_USER, login


@pytest.fixture
def creation(variation, monkeypatch):
    cache, request, provider, hosting, values, _ = variation
    monkeypatch.setattr(auth, 'current_user', CURRENT_USER)
    user, _ = login(cache, request)
    pmxbot.config.update(
        albums_url='https://gallery.example/bot', inane_channel='#music'
    )
    values.pop('source_image_id')
    values.update(
        artist_name='New Band',
        title='New Album',
        submission_token='a' * 32,
        format='Vinyl',
        genre='Rock',
    )
    return cache, request, provider, hosting, values, user


def submit(creation, **overrides):
    cache, request, provider, hosting, values, user = creation
    response = request(
        '/create_album',
        method='POST',
        data=dict(values, **overrides),
        accept='application/json',
    )
    assert response['status'] == 202, response['body']
    return JobStore(cache.database), json.loads(response['body'])['id']


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_complete_creation(creation):
    cache, request, provider, hosting, values, user = creation
    form = BeautifulSoup(request('/create_album')['body'], 'html.parser')
    album_form = BeautifulSoup(request()['body'], 'html.parser')
    for field in ('format', 'format_description', 'genre', 'artist_genre', 'style'):
        assert [x['value'] for x in form.select(f'[name={field}] option')] == [
            x['value'] for x in album_form.select(f'[name={field}] option')
        ]
    assert request('/gallery')['body'].count('Create Album') == 1
    store, identifier = submit(creation)
    assert submit(creation)[1] == identifier
    assert request('/create_album')['status'] == 200
    assert request('/jobs/' + identifier)['status'] == 200
    provider.assert_not_called()
    with closing(cache.read_connection()) as db:
        assert not db.execute(
            "SELECT 1 FROM artists WHERE name = 'New Band'"
        ).fetchone()
    process_job(store, store.claim(), pmxbot.config)
    job = store.get(identifier)
    assert job['status'] == 'succeeded'
    assert provider.call_count == 1
    assert 'Artwork style:' in provider.call_args.kwargs['json']['prompt']
    album, image = cache.get_album_page(job['album_id'])
    assert album['artist_name'] == 'New Band'
    assert album['title'] == 'New Album'
    metadata = images.generation_metadata(image)
    assert metadata['generation_inputs']['style'] == values['style']
    assert metadata['generation_inputs']['album_id'] == album['id']
    assert metadata['operation'] == 'create_new'
    assert len(cache.album_image_history(album['id'])) == 1
    assert 'New Album' in request('/gallery')['body']
    assert request('/jobs/' + identifier)['headers']['Location'].endswith(
        f"/albums/{album['id']}"
    )
    assert submit(creation)[1] == identifier
    result = list(images.durable_music_results())
    assert result[0] == '#music'
    assert 'New album created by Alice: New Band — New Album' in result[1]
    assert result[2] == f"https://gallery.example/bot/albums/{album['id']}"
    assert list(images.durable_music_results()) == []


@pytest.mark.parametrize('state', ['anonymous', 'unprivileged', 'admin', 'disabled'])
def test_permissions(creation, monkeypatch, state):
    cache, request, provider, hosting, values, user = creation
    if state == 'anonymous':
        monkeypatch.setattr(auth, 'current_user', lambda: None)
    else:
        with closing(UserStore(pmxbot.config.database)) as users:
            if state == 'admin':
                users.bootstrap_admin(user.username)
            users.update(
                user.id,
                can_generate_images=state == 'disabled',
                enabled=state != 'disabled',
            )
        request(
            '/login',
            method='POST',
            data=dict(username='Alice', password='correct password'),
        )
    for method in ('GET', 'POST'):
        assert request('/create_album', method=method, data=values)['status'] in (
            401,
            403,
        )
    assert 'Create Album' not in request('/gallery')['body']
    provider.assert_not_called()


@pytest.mark.parametrize(
    'field,value',
    [
        ('title', ''),
        ('artist_name', ' '),
        ('title', 'x' * 256),
        ('artist_name', 'bad\x02name'),
        ('genre', 'invalid'),
        ('format', 'invalid'),
        ('style', 'invalid'),
    ],
)
def test_validation(creation, field, value):
    cache, request, provider, hosting, values, user = creation
    response = request(
        '/create_album', method='POST', data=dict(values, **{field: value})
    )
    assert response['status'] == 400
    assert 'New Album' in response['body'] or field == 'title'
    provider.assert_not_called()


def test_csrf_and_ownership(creation):
    cache, request, provider, hosting, values, user = creation
    assert (
        request('/create_album', method='POST', data=dict(values, csrf_token='bad'))[
            'status'
        ]
        == 403
    )
    store, identifier = submit(creation)
    with closing(UserStore(pmxbot.config.database)) as users:
        other = users.create('Bob', password='correct password')
        users.update(other.id, can_generate_images=True)
    request(
        '/login', method='POST', data=dict(username='Bob', password='correct password')
    )
    assert request('/jobs/' + identifier)['status'] == 404
    assert (
        request('/create_album', method='POST', data=dict(retry=identifier))['status']
        == 404
    )
    provider.assert_not_called()


def test_failure_retry_and_permission_recheck(creation):
    cache, request, provider, hosting, values, user = creation
    store, identifier = submit(creation)
    provider.side_effect = RuntimeError('private provider details')
    process_job(store, store.claim(), pmxbot.config)
    assert store.get(identifier)['status'] == 'failed'
    assert list(images.durable_music_results()) == []
    with closing(cache.read_connection()) as db:
        assert db.execute('SELECT COUNT(*) FROM albums').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM artists').fetchone()[0] == 1
    assert 'Retry album creation' in request('/jobs/' + identifier)['body']
    provider.side_effect = None
    assert (
        request('/create_album', method='POST', data=dict(retry=identifier))['status']
        == 303
    )
    with closing(UserStore(pmxbot.config.database)) as users:
        users.update(user.id, can_generate_images=False)
    process_job(store, store.claim(), pmxbot.config)
    assert store.get(identifier)['status'] == 'failed'
    assert provider.call_count == 1


def test_saved_image_reused_after_database_failure(creation, monkeypatch):
    cache, request, provider, hosting, values, user = creation
    store, identifier = submit(creation)
    original = creations.conflicting_album
    monkeypatch.setattr(
        creations, 'conflicting_album', Mock(side_effect=sqlite3.OperationalError('db'))
    )
    process_job(store, store.claim(), pmxbot.config)
    assert store.get(identifier)['status'] == 'failed'
    with closing(cache.read_connection()) as db:
        assert db.execute('SELECT COUNT(*) FROM albums').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM artists').fetchone()[0] == 1
    monkeypatch.setattr(creations, 'conflicting_album', original)
    assert (
        request('/create_album', method='POST', data=dict(retry=identifier))['status']
        == 303
    )
    process_job(store, store.claim(), pmxbot.config)
    assert store.get(identifier)['status'] == 'succeeded'
    assert provider.call_count == 1


def test_normalization_conflicts_and_concurrent_tokens(creation):
    cache, request, provider, hosting, values, user = creation
    store = JobStore(cache.database)
    draft, errors = creations.prepare(
        {k: v for k, v in values.items() if k != 'submission_token'}
    )
    assert not errors

    def enqueue(_):
        return store.enqueue(
            draft,
            images.ImageCache(pmxbot.config),
            user,
            operation='create_album',
            submission_token='a' * 32,
            channel='#music',
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(enqueue, range(8)))) == 1
    assert (
        request(
            '/create_album', method='POST', data=dict(values, submission_token='b' * 32)
        )['status']
        == 400
    )
    process_job(store, store.claim(), pmxbot.config)
    assert (
        request(
            '/create_album',
            method='POST',
            data=dict(
                values,
                artist_name=' NEW  BAND ',
                title='new album',
                submission_token='c' * 32,
            ),
        )['status']
        == 400
    )
    store, identifier = submit(
        creation,
        title='Second album',
        artist_name=' new BAND ',
        submission_token='d' * 32,
    )
    process_job(store, store.claim(), pmxbot.config)
    with closing(cache.read_connection()) as db:
        assert db.execute('SELECT COUNT(*) FROM artists').fetchone()[0] == 2
    assert store.get(identifier)['status'] == 'succeeded'


def test_announcement_sanitizing_and_delivery_failure(creation, monkeypatch):
    cache, request, provider, hosting, values, user = creation
    store, identifier = submit(creation)
    process_job(store, store.claim(), pmxbot.config)
    job = store.get(identifier)
    job['inputs_json'] = json.dumps(
        dict(json.loads(job['inputs_json']), title='bad\r\n\x02title')
    )
    assert 'badtitle' in creations.announcement(job, pmxbot.config)
    monkeypatch.setattr(
        creations, 'announcement', Mock(side_effect=RuntimeError('IRC failure'))
    )
    assert list(images.durable_music_results()) == []
    assert store.get(identifier)['status'] == 'succeeded'
    assert (
        request('/create_album', method='POST', data=dict(retry=identifier))['status']
        == 303
    )
    assert provider.call_count == 1


def test_creation_with_empty_library(creation):
    cache, request, provider, hosting, values, user = creation
    with sqlite3.connect(cache.database) as db:
        db.execute('DROP TABLE album_images')
        db.execute('DROP TABLE album_image_failures')
        db.execute('DROP TABLE albums')
        db.execute('DROP TABLE artists')
    store, identifier = submit(creation)
    process_job(store, store.claim(), pmxbot.config)
    job = store.get(identifier)
    assert job['status'] == 'succeeded'
    album, image = cache.get_album_page(job['album_id'])
    assert album['title'] == values['title']
    assert image['hosted_url']


def test_missing_token_and_settings_frozen_for_resubmission(creation):
    cache, request, provider, hosting, values, user = creation
    no_token = {k: v for k, v in values.items() if k != 'submission_token'}
    assert request('/create_album', method='POST', data=no_token)['status'] == 400
    store, identifier = submit(creation)
    pmxbot.config['images_quality'] = 'high'
    assert submit(creation)[1] == identifier
    process_job(store, store.claim(), pmxbot.config)
    assert provider.call_args.kwargs['json']['quality'] == 'low'


def test_public_url_required_and_not_from_host(creation):
    cache, request, provider, hosting, values, user = creation
    pmxbot.config.pop('albums_url')
    assert request('/create_album', method='POST', data=values)['status'] == 503
    pmxbot.config['albums_url'] = 'javascript:bad'
    assert request('/create_album', method='POST', data=values)['status'] == 503
    provider.assert_not_called()


@pytest.mark.parametrize('stage', ['association', 'completion'])
def test_transaction_rolls_back_all_release_writes(creation, stage):
    cache, request, provider, hosting, values, user = creation
    store, identifier = submit(creation)
    with sqlite3.connect(cache.database) as db:
        if stage == 'association':
            db.execute(
                "CREATE TRIGGER fail_release BEFORE INSERT ON album_images "
                "BEGIN SELECT RAISE(ABORT, 'simulated association failure'); END"
            )
        else:
            db.execute(
                "CREATE TRIGGER fail_release BEFORE UPDATE ON generation_jobs "
                "WHEN NEW.status = 'succeeded' "
                "BEGIN SELECT RAISE(ABORT, 'simulated completion failure'); END"
            )
    process_job(store, store.claim(), pmxbot.config)
    assert store.get(identifier)['status'] == 'failed'
    with sqlite3.connect(cache.database) as db:
        assert db.execute('SELECT COUNT(*) FROM albums').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM artists').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM album_images').fetchone()[0] == 1
        db.execute('DROP TRIGGER fail_release')
    assert list(images.durable_music_results()) == []
    store.retry_creation(identifier, user)
    process_job(store, store.claim(), pmxbot.config)
    assert store.get(identifier)['status'] == 'succeeded'
    assert provider.call_count == 1
