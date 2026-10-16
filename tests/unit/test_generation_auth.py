import datetime
import json
from contextlib import closing
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from cherrypy.lib.sessions import RamSession

import pmxbot
from pmxbot.jobs import JobStore
from pmxbot.users import UserStore
from pmxbot.web import auth
from tests.unit.test_album_page import page, variation  # noqa: F401

CURRENT_USER = auth.current_user


def login(cache, request, *, approved=True):
    with closing(UserStore(pmxbot.config.database)) as store:
        user = store.create('Alice', password='correct password')
        if approved:
            user = store.update(user.id, can_generate_images=True)
    response = request(
        '/login',
        method='POST',
        data={
            'username': 'Alice',
            'password': 'correct password',
        },
    )
    assert response['status'] == 303
    return user, response


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
@pytest.mark.parametrize('standalone', [False, True])
@pytest.mark.parametrize('state', ['anonymous', 'logged_out', 'disabled', 'expired'])
def test_unsigned_variations_rejected_without_jobs(
    variation, monkeypatch, standalone, state
):
    cache, request, provider, hosting, values, source = variation
    monkeypatch.setattr(auth, 'current_user', CURRENT_USER)
    pmxbot.config['web_generation_standalone'] = standalone
    if state != 'anonymous':
        user, response = login(cache, request)
        token = BeautifulSoup(request()['body'], 'html.parser').select_one(
            '#variation-form [name=csrf_token]'
        )['value']
        if state == 'logged_out':
            assert (
                request('/logout', method='POST', data={'csrf_token': token})['status']
                == 303
            )
        elif state == 'disabled':
            with closing(UserStore(pmxbot.config.database)) as store:
                store.update(user.id, enabled=False)
        else:
            cookie = SimpleCookie(response['headers']['Set-Cookie'])[auth.COOKIE_NAME]
            session_id = cookie.value
            data, expires = RamSession.cache[session_id]
            RamSession.cache[session_id] = (data, expires - datetime.timedelta(days=1))
    body = BeautifulSoup(request()['body'], 'html.parser')
    assert body.select_one('#variation-form button')['disabled'] == ''
    link = body.select_one('#variation-sign-in')['href']
    assert urlsplit(link).path == pmxbot.config.web_base + '/login'
    assert parse_qs(urlsplit(link).query)['return_to'] == [
        f'{pmxbot.config.web_base}/albums/42?source_image_id=42'
    ]
    # Even a correctly supplied CSRF token cannot authorize signed-out generation.
    assert (
        request(method='POST', data=values, accept='application/json')['status'] == 401
    )
    assert request(method='POST', data=values)['status'] == 401
    assert not body.select('nav[aria-label=Account]')
    provider.assert_not_called()
    hosting.put_object.assert_not_called()
    with closing(cache.read_connection()) as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'generation_jobs'"
        ).fetchone()


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_real_signed_in_user_owns_variation(variation, monkeypatch):
    cache, request, provider, hosting, values, source = variation
    monkeypatch.setattr(auth, 'current_user', CURRENT_USER)
    user, _ = login(cache, request)
    body = BeautifulSoup(request()['body'], 'html.parser')
    assert body.select_one('#variation-sign-in') is None
    assert not body.select_one('#variation-form button').has_attr('disabled')
    response = request(method='POST', data=values, accept='application/json')
    assert response['status'] == 202
    job = JobStore(cache.database).get(json.loads(response['body'])['id'])
    assert job['user_id'] == user.id
    assert job['requested_by'] == user.username
    assert job['anonymous_owner'] == ''
    provider.assert_not_called()
    hosting.put_object.assert_not_called()


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_unsigned_post_does_not_create_storage(page):
    cache, request = page
    assert request(method='POST', data={'source_image_id': '42'})['status'] == 401
    assert not cache.database.exists()


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
@pytest.mark.parametrize('role', ['user', 'admin'])
def test_image_permission_denies_regular_users_and_administrators(
    variation, monkeypatch, role
):
    cache, request, provider, hosting, values, source = variation
    monkeypatch.setattr(auth, 'current_user', CURRENT_USER)
    user, _ = login(cache, request, approved=False)
    with closing(UserStore(pmxbot.config.database)) as store:
        if role == 'admin':
            store.bootstrap_admin(user.username)
    # Establish a fresh session after the permission change.
    response = request(
        '/login',
        method='POST',
        data={
            'username': 'Alice',
            'password': 'correct password',
        },
    )
    assert response['status'] == 303
    body = BeautifulSoup(request()['body'], 'html.parser')
    assert body.select_one('#variation-form button').has_attr('disabled')
    assert request(method='POST', data=values)['status'] == 403
    provider.assert_not_called()
    hosting.put_object.assert_not_called()


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_registered_user_can_use_preferences_but_image_tools_require_grant(
    variation, monkeypatch
):
    from pmxbot.music import MusicLibrary

    cache, request, provider, hosting, values, _source = variation
    monkeypatch.setattr(auth, 'current_user', CURRENT_USER)
    password = 'NewAccountPassword123'
    assert (
        request(
            '/register',
            method='POST',
            data={
                'username': 'NewUser',
                'password': password,
                'password_confirmation': password,
            },
        )['status']
        == 303
    )
    assert (
        request(
            '/login',
            method='POST',
            data={
                'username': 'NewUser',
                'password': password,
            },
        )['status']
        == 303
    )
    with closing(UserStore(pmxbot.config.database)) as store:
        user = store.get_by_username('NewUser')
        assert user.enabled and not user.can_generate_images and not user.is_admin
    # Two artwork versions allow choosing a personal favorite without approval.
    with closing(cache.connect()) as db:
        db.execute(
            """INSERT INTO image_cache
            (id, cache_key, prompt, normalized_prompt, settings_json, local_filename,
             hosted_url, generation_metadata_json)
            VALUES (43, 'second', '', '', '{}', '/unused', 'https://art.example/second', '{}')"""
        )
    MusicLibrary(cache.database).record_image(42, 'second')
    csrf = BeautifulSoup(request()['body'], 'html.parser').select_one(
        '[name=csrf_token]'
    )['value']
    favorite = request(
        '/favorites',
        method='POST',
        data={
            'album_id': '42',
            'image_id': '42',
            'csrf_token': csrf,
        },
    )
    assert favorite['status'] == 200
    assert json.loads(favorite['body']) == {'favorite_image_id': '42'}
    assert (
        request(
            '/torta_mode',
            method='POST',
            data={
                'enabled': '1',
                'csrf_token': csrf,
            },
        )['status']
        == 303
    )
    assert request()['status'] == 200
    with closing(UserStore(pmxbot.config.database)) as store:
        assert store.get_by_id(user.id).nsfw
        assert not store.get_by_id(user.id).can_generate_images
    for operation in ('edit', 'create_new'):
        assert (
            request(
                method='POST',
                data={
                    **values,
                    'operation': operation,
                    'submission_token': 'a' * 32,
                },
                accept='application/json',
            )['status']
            == 403
        )
    provider.assert_not_called()
    hosting.put_object.assert_not_called()
    # Granting image permission still revokes the old session, then allows tools.
    with closing(UserStore(pmxbot.config.database)) as store:
        store.update(user.id, can_generate_images=True)
    assert request(method='POST', data=values)['status'] == 401
    assert (
        request(
            '/login',
            method='POST',
            data={
                'username': 'NewUser',
                'password': password,
            },
        )['status']
        == 303
    )
    for operation, token in (('edit', 'b' * 32), ('create_new', 'c' * 32)):
        response = request(
            method='POST',
            data={
                **values,
                'operation': operation,
                'submission_token': token,
            },
            accept='application/json',
        )
        assert response['status'] == 202
    assert (
        json.loads(request('/favorites?album_id=42')['body'])['favorite_image_id']
        == '42'
    )
    provider.assert_not_called()
    hosting.put_object.assert_not_called()
