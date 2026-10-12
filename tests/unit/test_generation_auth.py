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


def login(cache, request):
    with closing(UserStore(pmxbot.config.database)) as store:
        user = store.create('Alice', password='correct password')
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
