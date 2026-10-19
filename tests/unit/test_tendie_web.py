"""Authenticated website boundaries and real background tendie execution."""

import datetime
import json
import threading
from contextlib import closing
from unittest.mock import Mock

import cherrypy
import pytest
import requests

import pmxbot
from pmxbot import tendie, tendie_jobs
from pmxbot.users import UserStore
from pmxbot.web import viewer
from tests.unit.test_web_auth import web  # noqa: F401


@pytest.fixture
def site(web, monkeypatch):
    pmxbot.config.update(tendie_author='ExampleAccount', x_bearer_token='private-token')
    monkeypatch.setattr(
        tendie.requests, 'get', Mock(side_effect=AssertionError('Unexpected network'))
    )
    executor = tendie_jobs.TendieExecutor(cherrypy.engine)
    executor.start()
    monkeypatch.setattr(viewer, 'tendie_executor', executor)
    yield web, executor
    executor.stop()


def administrator(web):
    with closing(UserStore(pmxbot.config.database)) as store:
        store.bootstrap_admin('Alice')
    assert web.login()['status'] == 303


def state(web):
    response = web.request('/admin/tendie_status')
    assert response['status'] == 200
    return json.loads(response['body'])


def submit(web, **overrides):
    values = {'csrf_token': web.token('/admin/tendies')}
    values.update(overrides)
    return web.request('/admin/tendies', 'POST', values)


def metadata():
    with closing(tendie.Collection()) as store:
        return [
            tuple(row)
            for row in store.db.execute('SELECT * FROM tendie_sync ORDER BY key')
        ]


@pytest.mark.parametrize(
    'path,method',
    [
        ('/admin/tendies', 'GET'),
        ('/admin/tendies', 'POST'),
        ('/admin/tendie_status', 'GET'),
    ],
)
@pytest.mark.parametrize('identity', ['anonymous', 'regular'])
def test_administrator_required_for_every_endpoint(site, path, method, identity):
    web, executor = site
    if identity == 'regular':
        web.login()
    before = metadata()
    response = web.request(path, method, {'csrf_token': 'bad'})
    assert response['status'] == (401 if identity == 'anonymous' else 403)
    assert 'ExampleAccount' not in response['body']
    assert metadata() == before
    tendie.requests.get.assert_not_called()
    assert not executor._active


@pytest.mark.parametrize(
    'path,method',
    [
        ('/admin/tendies', 'GET'),
        ('/admin/tendies', 'POST'),
        ('/admin/tendie_status', 'GET'),
    ],
)
@pytest.mark.parametrize(
    'change,expected',
    [('enabled=0', 401), ('must_change_password=1', 303), ("role='user'", 403)],
)
def test_admin_revocation_and_password_change_block_every_endpoint(
    site, path, method, change, expected
):
    web, executor = site
    administrator(web)
    token = web.token('/admin/tendies')
    with closing(UserStore(pmxbot.config.database)) as store:
        store.db.execute('UPDATE users SET ' + change + ' WHERE id=?', (web.user_id,))
    before = metadata()
    response = web.request(path, method, {'csrf_token': token})
    assert response['status'] == expected
    assert metadata() == before
    tendie.requests.get.assert_not_called()
    assert not executor._active


def test_page_status_and_get_requests_never_start_updates(site, monkeypatch):
    web, executor = site
    administrator(web)
    sync = Mock()
    monkeypatch.setattr(tendie, 'synchronize', sync)
    before = metadata()
    page = web.request('/admin/tendies')
    assert page['status'] == 200
    assert 'ExampleAccount' in page['body'] and 'Update tendies' in page['body']
    assert web.base + '/admin/tendies' in web.request('/')['body']
    assert state(web) == dict(
        source='ExampleAccount',
        count=0,
        last_success=None,
        job=dict(status='idle', message='No update requested.'),
    )
    assert web.request('/admin/tendies?operation=update')['status'] == 400
    assert web.request('/admin/tendies', 'HEAD')['status'] == 200
    assert web.request('/admin/tendie_status', 'POST')['status'] == 405
    assert web.request('/admin/tendies', 'PUT')['status'] == 405
    assert metadata() == before
    sync.assert_not_called()
    assert not executor._active


@pytest.mark.parametrize('token', ['', 'wrong'])
def test_csrf_rejection_leaves_state_unchanged(site, token):
    web, executor = site
    administrator(web)
    before = metadata()
    assert submit(web, csrf_token=token)['status'] == 403
    assert metadata() == before
    assert not executor._active
    tendie.requests.get.assert_not_called()


def test_other_session_csrf_and_query_post_rejected(site):
    web, _ = site
    token = web.token()
    administrator(web)
    assert submit(web, csrf_token=token)['status'] == 403
    token = web.token('/admin/tendies')
    before = metadata()
    assert web.request('/admin/tendies?csrf_token=' + token, 'POST')['status'] == 400
    assert metadata() == before
    tendie.requests.get.assert_not_called()


def test_background_fetch_responsive_duplicate_safe_and_checkpointed(site, monkeypatch):
    web, executor = site
    administrator(web)
    entered, release = threading.Event(), threading.Event()
    provider_threads, requests_seen = [], []
    caller = threading.get_ident()

    def get(url, **kwargs):
        provider_threads.append(threading.get_ident())
        requests_seen.append((url, kwargs))
        if 'by/username' in url:
            return Mock(status_code=200, json=Mock(return_value={'data': {'id': '42'}}))
        entered.set()
        assert release.wait(5)
        return Mock(
            status_code=200,
            json=Mock(
                return_value={
                    'data': [
                        {
                            'id': '2',
                            'text': 'reply',
                            'author_id': '42',
                            'created_at': '2026-10-18T12:00:00Z',
                            'referenced_tweets': [{'type': 'replied_to', 'id': '1'}],
                        }
                    ],
                    'meta': {},
                }
            ),
        )

    monkeypatch.setattr(tendie.requests, 'get', Mock(side_effect=get))
    other = tendie_jobs.TendieExecutor(cherrypy.engine)
    other.start()
    try:
        response = submit(web)
        assert response['status'] == 303
        assert response['headers']['Location'] == web.base + '/admin/tendies'
        assert entered.wait(5) and not release.is_set()
        assert set(provider_threads).isdisjoint({caller})
        assert web.request('/admin/tendies')['status'] == 200
        assert web.request('/protected')['status'] == 200
        assert state(web)['job']['status'] == 'running'
        assert submit(web)['status'] == 409
        assert other.enqueue(pmxbot.config) is False
        assert len(requests_seen) == 2
    finally:
        release.set()
        executor.stop()
        other.stop()
    result = state(web)
    assert result['job'] == {'status': 'succeeded', 'message': 'Imported 0 posts.'}
    assert result['last_success'] and result['count'] == 0
    assert 'private-token' not in json.dumps(result)
    with closing(tendie.Collection()) as store:
        assert store.meta('live_since_id') == '2'
        assert store.meta('sync_owner') == ''
    assert requests_seen[1][1]['timeout'] == 15


@pytest.mark.parametrize(
    'failure',
    [
        requests.Timeout('private-provider-detail'),
        RuntimeError('private-provider-detail'),
    ],
)
def test_worker_errors_are_private_and_visible_safely(
    site, monkeypatch, caplog, failure
):
    web, executor = site
    administrator(web)
    monkeypatch.setattr(tendie.requests, 'get', Mock(side_effect=failure))
    assert submit(web)['status'] == 303
    executor.stop()
    result = state(web)
    assert result['job']['status'] == 'failed'
    assert 'failed' in result['job']['message']
    assert 'private-provider-detail' not in json.dumps(result) + caplog.text
    assert 'private-token' not in web.request('/admin/tendies')['body']
    with closing(tendie.Collection()) as store:
        assert store.meta('live_since_id') is None
        assert store.meta('last_success') is None


def test_durable_cooldown_and_stale_job_recovery(site):
    web, executor = site
    administrator(web)
    with closing(tendie.Collection()) as store:
        store.set_meta('last_attempt', tendie.now().isoformat())
    assert submit(web)['status'] == 303
    executor.stop()
    assert 'cooling down' in state(web)['job']['message']
    tendie.requests.get.assert_not_called()
    old = (tendie.now() - datetime.timedelta(minutes=31)).isoformat()
    with closing(tendie.Collection()) as store:
        store.set_meta(
            'web_job', json.dumps(dict(id='old', status='running', submitted_at=old))
        )
        before = store.meta('web_job')
    assert state(web)['job']['status'] == 'failed'
    with closing(tendie.Collection()) as store:
        assert store.meta('web_job') == before
    executor.start()
    assert submit(web)['status'] == 303
    executor.stop()
    assert state(web)['job']['status'] == 'failed'


def test_summary_and_job_status_are_account_scoped(site):
    web, _ = site
    administrator(web)
    with closing(tendie.Collection()) as store:
        source = Mock(
            posts=Mock(
                return_value=iter(
                    [
                        dict(
                            id='1',
                            text='post',
                            author='ExampleAccount',
                            created_at='2026-10-18T12:00:00Z',
                        )
                    ]
                )
            )
        )
        store.sync(source)
        store.set_meta(
            'web_job', json.dumps(dict(status='succeeded', message='Imported 1 posts.'))
        )
    summary = state(web)
    assert summary['source'] == 'ExampleAccount'
    assert summary['count'] == 1 and summary['last_success']
    assert summary['job']['message'] == 'Imported 1 posts.'
    pmxbot.config['tendie_author'] = 'AnotherAccount'
    summary = state(web)
    assert summary['source'] == 'AnotherAccount'
    assert summary['count'] == 0 and summary['last_success'] is None
    assert summary['job']['status'] == 'idle'
    tendie.requests.get.assert_not_called()
