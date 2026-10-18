import contextlib
import datetime
import json
import sqlite3
import threading
from unittest.mock import Mock

import pytest
import requests

import pmxbot
from pmxbot import core, tendie


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 18, 12, tzinfo=UTC)


def post(post_id='1', created_at='2026-10-17T12:00:00Z', **kwargs):
    return dict(
        id=post_id,
        text='crispy tendies',
        author=tendie.AUTHOR,
        created_at=created_at,
        **kwargs,
    )


def provider(*posts):
    return Mock(posts=Mock(return_value=iter(posts)))


@pytest.fixture
def collection(tmp_path, monkeypatch):
    filename = tmp_path / 'tendie.sqlite'
    monkeypatch.setattr(pmxbot, 'config', {'tendie_database': str(filename)})
    monkeypatch.setattr(tendie, 'now', lambda: NOW)
    monkeypatch.delenv('X_BEARER_TOKEN', raising=False)
    # Every unexpected network request fails the test, including database reads.
    monkeypatch.setattr(
        tendie.requests, 'get', Mock(side_effect=AssertionError('Network'))
    )
    with contextlib.closing(tendie.Collection(filename)) as store:
        yield store


def allow_sync(store):
    with store.db:
        store.db.execute("DELETE FROM tendie_sync WHERE key='last_attempt'")


def test_registration_and_public_days(collection):
    handler = next(core.Handler.find_matching('!tendie days', '#test'))
    assert handler.func is tendie.tendie
    assert 'No tendies' in handler.func('days')


def test_initial_incremental_duplicates_and_persistence(collection):
    first = provider(post(), post('2'))
    assert collection.sync(first) == 2
    first.posts.assert_called_once_with(None)
    allow_sync(collection)
    second = provider(post('2'), post('3'))
    assert collection.sync(second) == 1
    second.posts.assert_called_once_with('2')
    assert collection.size() == 3
    with contextlib.closing(
        tendie.Collection(pmxbot.config['tendie_database'])
    ) as other:
        assert other.size() == 3
        assert other.meta('last_success') == NOW.isoformat()
        assert other.meta('last_attempt') == NOW.isoformat()
        assert other.select(True)['imported_at'] == NOW.isoformat()


def test_random_latest_and_no_network(collection):
    collection.sync(provider(post(), post('2', '2026-10-18T01:00:00+02:00')))
    # Random selection must reach both rows, while latest uses creation time.
    assert {collection.select()['id'] for _ in range(100)} == {'1', '2'}
    assert collection.select(True)['id'] == '2'
    assert tendie.tendie('').endswith(('/1', '/2'))
    assert tendie.tendie('latest').endswith('/2')
    assert '0 complete days' in tendie.tendie('days')
    assert '2 posts' in tendie.tendie('status')
    tendie.requests.get.assert_not_called()


@pytest.mark.parametrize(
    'created,days',
    [
        ('2026-10-18T00:00:00Z', 0),
        ('2026-10-17T12:00:00Z', 1),
        ('2026-10-14T11:00:00Z', 4),
        ('2026-10-17T14:00:00+02:00', 1),
        ('2026-10-17T12:00:01Z', 0),
    ],
)
def test_days_creation_time_not_import(collection, created, days):
    collection.sync(provider(post(created_at=created)))
    result = tendie.tendie('days')
    assert result.startswith(f'{days} complete day')
    assert result.endswith('https://x.com/Robert_McTender/status/1')


@pytest.mark.parametrize('created', ['broken', '2026-10-17T12:00:00', ''])
def test_days_malformed(collection, created):
    collection.sync(provider(post()))
    with collection.db:
        collection.db.execute('UPDATE tendie_posts SET created_at=?', (created,))
    assert 'invalid creation timestamp' in tendie.tendie('days')
    assert tendie.tendie('days').endswith('/1')


@pytest.mark.parametrize('action', ['', 'latest', 'days'])
def test_empty(collection, action):
    assert 'No tendies stored' in tendie.tendie(action)
    tendie.requests.get.assert_not_called()


def test_missing_credentials_status_and_usage(collection):
    assert 'Configure' in tendie.tendie('update')
    assert '0 posts' in tendie.tendie('status')
    assert 'never' in tendie.tendie('status')
    assert 'Usage:' in tendie.tendie('unknown')


def test_cooldown_and_concurrent_writer(collection):
    collection.sync(provider(post()))
    with pytest.raises(tendie.SyncError, match='cooling down'):
        collection.sync(provider(post('2')))
    allow_sync(collection)
    with tendie._sync_lock:
        with pytest.raises(tendie.SyncError, match='already running'):
            collection.sync(provider())
    other = tendie.Collection(pmxbot.config['tendie_database'])
    try:
        collection.db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.sync(provider())
    finally:
        collection.db.rollback()
        other.close()


def test_cooldown_expires(collection, monkeypatch):
    collection.sync(provider(post()))
    monkeypatch.setattr(tendie, 'now', lambda: NOW + datetime.timedelta(seconds=300))
    assert collection.sync(provider(post('2'))) == 1


def test_actual_concurrent_sync_keeps_reads_available(collection):
    started, finish = threading.Event(), threading.Event()
    results = []

    def posts(since_id):
        started.set()
        assert finish.wait(5)
        yield post()

    def update():
        with contextlib.closing(
            tendie.Collection(pmxbot.config['tendie_database'])
        ) as store:
            results.append(store.sync(Mock(posts=posts)))

    thread = threading.Thread(target=update)
    thread.start()
    try:
        assert started.wait(5)
        pmxbot.config['x_bearer_token'] = 'test-secret'
        assert 'already running' in tendie.tendie('update')
        assert 'No tendies' in tendie.tendie('')
        assert collection.size() == 0
        with pytest.raises(tendie.SyncError, match='already running'):
            collection.sync(provider())
    finally:
        finish.set()
        thread.join(5)
    assert results == [1]


def api_response(data=None, **kwargs):
    return Mock(
        status_code=200, json=Mock(return_value=dict(data=data or [], **kwargs))
    )


@pytest.fixture
def api(collection):
    pmxbot.config['x_bearer_token'] = 'test-secret'
    tendie.requests.get.side_effect = None
    return tendie.requests.get


def xpost(post_id='1'):
    return dict(post(post_id), author_id='42')


def test_api_initial_and_pagination(api, collection):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('2')], meta={'next_token': 'next'}),
        api_response([xpost()], meta={}),
    ]
    assert tendie.tendie('update') == 'Tendie: imported 2 posts.'
    assert api.call_count == 3
    first = api.call_args_list[1]
    assert first.args[0] == 'https://api.x.com/2/users/42/tweets'
    assert first.kwargs['headers'] == {'Authorization': 'Bearer test-secret'}
    assert first.kwargs['timeout'] == 15
    assert first.kwargs['params']['exclude'] == 'retweets'
    assert 'since_id' not in first.kwargs['params']
    assert api.call_args.kwargs['params']['pagination_token'] == 'next'
    assert 'cooling down' in tendie.tendie('update')


def test_api_incremental_environment_key(api, collection, monkeypatch):
    collection.sync(provider(post()))
    allow_sync(collection)
    del pmxbot.config['x_bearer_token']
    monkeypatch.setenv('X_BEARER_TOKEN', 'env-secret')
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('2')], meta={})]
    assert 'imported 1' in tendie.tendie('update')
    assert api.call_args.kwargs['params']['since_id'] == '1'
    assert api.call_args.kwargs['headers']['Authorization'] == 'Bearer env-secret'


@pytest.mark.parametrize(
    'failure', [requests.Timeout('secret'), requests.ConnectionError('secret')]
)
def test_partial_failure_atomic_and_retry(api, collection, failure):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('3')], meta={'next_token': 'next'}),
        failure,
    ]
    assert 'no posts imported' in tendie.tendie('update')
    assert collection.size() == 0
    assert collection.meta('last_success') is None
    assert 'cooling down' in tendie.tendie('update')
    allow_sync(collection)
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('3'), xpost('2')], meta={}),
    ]
    assert 'imported 2' in tendie.tendie('update')
    assert 'since_id' not in api.call_args.kwargs['params']


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_api_http_failure(api, collection, status):
    response = api_response()
    response.status_code = status
    response.raise_for_status.side_effect = requests.HTTPError('secret')
    api.return_value = response
    result = tendie.tendie('update')
    assert ('rate limit' if status == 429 else 'failed') in result
    assert 'secret' not in result
    assert collection.meta('last_success') is None


@pytest.mark.parametrize(
    'page',
    [
        {'data': [xpost()], 'errors': [{'detail': 'secret'}], 'meta': {}},
        {'data': [dict(xpost(), author_id='wrong')], 'meta': {}},
        {'data': [dict(xpost(), created_at='bad')], 'meta': {}},
        {'data': [xpost()], 'meta': {'next_token': 'repeated'}},
        {'data': None, 'meta': {}},
    ],
)
def test_api_invalid_and_partial_data(api, collection, page):
    api.side_effect = [
        api_response({'id': '42'}),
        Mock(status_code=200, json=Mock(return_value=page)),
        Mock(status_code=200, json=Mock(return_value=page)),
    ]
    assert 'no posts imported' in tendie.tendie('update')
    assert collection.size() == 0


def test_api_empty_success(api, collection):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response(meta={'result_count': 0}),
    ]
    assert 'imported 0' in tendie.tendie('update')
    assert collection.meta('last_success')


@pytest.mark.parametrize(
    'replies,quotes,expected', [(False, True, 2), (True, True, 3), (True, False, 2)]
)
def test_filtering(collection, replies, quotes, expected):
    posts = [
        post(),
        post('2', referenced_tweets=[{'type': 'retweeted'}]),
        post('3', referenced_tweets=[{'type': 'replied_to'}]),
        post('4', referenced_tweets=[{'type': 'quoted'}]),
    ]
    assert (
        collection.sync(
            provider(*posts), include_replies=replies, include_quotes=quotes
        )
        == expected
    )


@pytest.mark.parametrize('extension', ['json', 'csv'])
def test_offline_import(collection, tmp_path, extension):
    filename = tmp_path / ('posts.' + extension)
    if extension == 'json':
        filename.write_text(json.dumps([post(), post('2')]), encoding='utf-8')
    else:
        filename.write_text(
            'id,text,author,created_at\n1,crispy,Robert_McTender,2026-10-17T12:00:00Z\n2,tasty,@Robert_McTender,2026-10-17T12:00:00Z\n',
            encoding='utf-8',
        )
    source = tendie.FileProvider(filename)
    assert collection.sync(source) == 2
    allow_sync(collection)
    assert collection.sync(source) == 0
    tendie.requests.get.assert_not_called()


def test_bad_offline_import_rolls_back(collection):
    source = provider(post(), dict(post('2'), author='someone_else'))
    with pytest.raises(ValueError):
        collection.sync(source)
    assert collection.size() == 0


def test_irc_formatting(collection):
    dirty = dict(post(), text='\x01ACTION hi\x01\n\r\x00\x03 &amp; ' + '🍗' * 500)
    collection.sync(provider(dirty))
    result = tendie.tendie('')
    assert len(result.encode('utf-8')) <= 400
    assert result.endswith('... https://x.com/Robert_McTender/status/1')
    assert not any(ord(char) < 32 for char in result)
    assert '&amp;' not in result


def test_failed_incremental_preserves_existing_rows_and_success(api, collection):
    collection.sync(provider(post()))
    success = collection.meta('last_success')
    allow_sync(collection)
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('3')], meta={'next_token': 'next'}),
        requests.Timeout('secret'),
    ]
    assert 'no posts imported' in tendie.tendie('update')
    assert collection.size() == 1
    assert collection.select(True)['id'] == '1'
    assert collection.meta('last_success') == success
    allow_sync(collection)
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('2')], meta={})]
    assert 'imported 1' in tendie.tendie('update')
    assert api.call_args.kwargs['params']['since_id'] == '1'


def test_cli_import(collection, tmp_path, monkeypatch, capsys):
    filename = tmp_path / 'posts.json'
    filename.write_text(json.dumps([post()]), encoding='utf-8')
    monkeypatch.setattr(
        'sys.argv',
        ['tendie', str(filename), '--database', pmxbot.config['tendie_database']],
    )
    tendie.main()
    assert 'Imported 1 posts.' in capsys.readouterr().out
    assert collection.size() == 1
    allow_sync(collection)
    filename.write_text('malformed JSON', encoding='utf-8')
    with pytest.raises(SystemExit) as exc:
        tendie.main()
    assert exc.value.code == 1
    assert 'no posts imported' in capsys.readouterr().err
    assert collection.size() == 1


def test_missing_page_data_is_not_success(api, collection):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response(meta={'result_count': 2}),
    ]
    assert 'no posts imported' in tendie.tendie('update')
    assert collection.meta('last_success') is None


def test_pagination_bound_rolls_back(api, collection):
    api.side_effect = [api_response({'id': '42'})] + [
        api_response([xpost(str(index + 1))], meta={'next_token': str(index)})
        for index in range(100)
    ]
    assert 'pagination limit' in tendie.tendie('update')
    assert collection.size() == 0
    assert collection.meta('last_success') is None
