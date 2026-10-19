import contextlib
import copy
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


def live_update():
    try:
        count = tendie.synchronize(tendie.settings_snapshot(pmxbot.config))
        return f'Tendie: imported {count} posts.'
    except Exception as exc:
        return tendie.synchronization_error(exc)


def post(post_id='1', created_at='2026-10-17T12:00:00Z', **kwargs):
    return dict(
        id=post_id,
        text='crispy tendies',
        author='ExampleAccount',
        created_at=created_at,
        **kwargs,
    )


def provider(*posts):
    return Mock(posts=Mock(return_value=iter(posts)))


@pytest.fixture
def collection(tmp_path, monkeypatch):
    filename = tmp_path / 'pmxbot.sqlite'
    monkeypatch.setattr(
        pmxbot,
        'config',
        core.ConfigDict(
            database='sqlite:' + str(filename),
            tendie_author='ExampleAccount',
            log_channels=[],
            other_channels=[],
        ),
    )
    monkeypatch.setattr(tendie, 'now', lambda: NOW)
    monkeypatch.delenv('X_BEARER_TOKEN', raising=False)
    monkeypatch.delenv('TENDIE_AUTHOR', raising=False)
    # Every unexpected network request fails the test, including database reads.
    monkeypatch.setattr(
        tendie.requests, 'get', Mock(side_effect=AssertionError('Network'))
    )
    with contextlib.closing(tendie.Collection(filename)) as store:
        yield store


def allow_sync(store):
    with store.db:
        store.db.execute(
            'DELETE FROM tendie_sync WHERE key=?',
            (store.author.lower() + ':last_attempt',),
        )


def test_registration_and_public_days(collection):
    handler = next(core.CommandHandler.find_matching('!tendie days', '#test'))
    assert handler.func is tendie.tendie
    assert 'No tendies' in handler.func('days')


def test_initial_incremental_duplicates_and_persistence(collection):
    first = provider(post(), post('2'))
    assert collection.sync(first, live=True) == 2
    first.posts.assert_called_once_with(None)
    allow_sync(collection)
    second = provider(post('2'), post('3'))
    assert collection.sync(second, live=True) == 1
    second.posts.assert_called_once_with('2')
    assert collection.size() == 3
    with contextlib.closing(tendie.Collection(pmxbot.config['database'])) as other:
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
    assert result.endswith('https://x.com/ExampleAccount/status/1')


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
    assert 'Configure' in live_update()
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
    other = tendie.Collection(pmxbot.config['database'])
    try:
        other.db.execute('PRAGMA busy_timeout=0')
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
        with contextlib.closing(tendie.Collection(pmxbot.config['database'])) as store:
            results.append(store.sync(Mock(posts=posts)))

    thread = threading.Thread(target=update)
    thread.start()
    try:
        assert started.wait(5)
        pmxbot.config['x_bearer_token'] = 'test-secret'
        assert 'already running' in live_update()
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
    assert live_update() == 'Tendie: imported 2 posts.'
    assert api.call_count == 3
    first = api.call_args_list[1]
    assert first.args[0] == 'https://api.x.com/2/users/42/tweets'
    assert first.kwargs['headers'] == {'Authorization': 'Bearer test-secret'}
    assert first.kwargs['timeout'] == 15
    assert first.kwargs['params']['exclude'] == 'retweets'
    assert 'since_id' not in first.kwargs['params']
    assert api.call_args.kwargs['params']['pagination_token'] == 'next'
    assert 'cooling down' in live_update()


def test_api_incremental_environment_key(api, collection, monkeypatch):
    collection.sync(provider(post()), live=True)
    allow_sync(collection)
    del pmxbot.config['x_bearer_token']
    monkeypatch.setenv('X_BEARER_TOKEN', 'env-secret')
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('2')], meta={})]
    assert 'imported 1' in live_update()
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
    assert 'no posts imported' in live_update()
    assert collection.size() == 0
    assert collection.meta('last_success') is None
    assert 'cooling down' in live_update()
    allow_sync(collection)
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('3'), xpost('2')], meta={}),
    ]
    assert 'imported 2' in live_update()
    assert 'since_id' not in api.call_args.kwargs['params']


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_api_http_failure(api, collection, status):
    response = api_response()
    response.status_code = status
    response.raise_for_status.side_effect = requests.HTTPError('secret')
    api.return_value = response
    result = live_update()
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
    assert 'no posts imported' in live_update()
    assert collection.size() == 0


def test_api_empty_success(api, collection):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response(meta={'result_count': 0}),
    ]
    assert 'imported 0' in live_update()
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
            'id,text,author,created_at\n1,crispy,ExampleAccount,2026-10-17T12:00:00Z\n2,tasty,@ExampleAccount,2026-10-17T12:00:00Z\n',
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
    assert result.endswith('... https://x.com/ExampleAccount/status/1')
    assert not any(ord(char) < 32 for char in result)
    assert '&amp;' not in result


def test_failed_incremental_preserves_existing_rows_and_success(api, collection):
    collection.sync(provider(post()), live=True)
    success = collection.meta('last_success')
    allow_sync(collection)
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('3')], meta={'next_token': 'next'}),
        requests.Timeout('secret'),
    ]
    assert 'no posts imported' in live_update()
    assert collection.size() == 1
    assert collection.select(True)['id'] == '1'
    assert collection.meta('last_success') == success
    allow_sync(collection)
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('2')], meta={})]
    assert 'imported 1' in live_update()
    assert api.call_args.kwargs['params']['since_id'] == '1'


def test_cli_import(collection, tmp_path, monkeypatch, capsys):
    filename = tmp_path / 'posts.json'
    filename.write_text(json.dumps([post()]), encoding='utf-8')
    monkeypatch.setattr(
        'sys.argv',
        [
            'tendie',
            str(filename),
            '--database',
            pmxbot.config['database'],
            '--author',
            'ExampleAccount',
        ],
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
    assert 'no posts imported' in live_update()
    assert collection.meta('last_success') is None


def test_pagination_bound_rolls_back(api, collection):
    api.side_effect = [api_response({'id': '42'})] + [
        api_response([xpost(str(index + 1))], meta={'next_token': str(index)})
        for index in range(100)
    ]
    assert 'pagination limit' in live_update()
    assert collection.size() == 0
    assert collection.meta('last_success') is None


def test_configured_source_controls_api_and_urls(api, collection):
    pmxbot.config['tendie_author'] = '@AnotherAccount'
    api.side_effect = [api_response({'id': '42'}), api_response([xpost()], meta={})]
    assert 'imported 1' in live_update()
    assert (
        api.call_args_list[0].args[0]
        == 'https://api.x.com/2/users/by/username/AnotherAccount'
    )
    assert tendie.tendie('latest').endswith('https://x.com/AnotherAccount/status/1')
    assert collection.size() == 0


def test_switching_accounts_isolates_history_and_sync_state(collection, monkeypatch):
    collection.sync(provider(post('99')))
    monkeypatch.setitem(pmxbot.config, 'tendie_author', 'AnotherAccount')
    assert 'No tendies' in tendie.tendie('days')
    assert '0 posts' in tendie.tendie('status')
    assert 'never' in tendie.tendie('status')
    with contextlib.closing(tendie.Collection(pmxbot.config['database'])) as other:
        source = provider(dict(post('1'), author='@anotheraccount'))
        assert other.sync(source) == 1
        source.posts.assert_called_once_with(None)
        assert other.size() == 1
        assert other.meta('last_success')
    monkeypatch.setitem(pmxbot.config, 'tendie_author', 'exampleaccount')
    assert '1 posts' in tendie.tendie('status')
    assert tendie.tendie('latest').endswith('/99')
    pmxbot.config['x_bearer_token'] = 'test-secret'
    assert 'cooling down' in live_update()


def test_source_environment_fallback_and_configuration_precedence(
    collection, monkeypatch
):
    monkeypatch.setenv('TENDIE_AUTHOR', '@EnvAccount')
    assert tendie.configured_author() == 'ExampleAccount'
    del pmxbot.config['tendie_author']
    assert tendie.configured_author() == 'EnvAccount'


@pytest.mark.parametrize('author', [None, '', 'bad/name', 'a' * 16, 'bad name'])
def test_missing_or_invalid_source_is_graceful(collection, author):
    pmxbot.config['tendie_author'] = author
    result = live_update()
    assert (
        'Configure tendie_author'
        if not author
        else 'Invalid tendie data or configuration'
    ) in result
    tendie.requests.get.assert_not_called()


def test_primary_database_storage_and_additive_schema(tmp_path, monkeypatch):
    from pmxbot.storage import SQLiteStorage

    filename = tmp_path / 'primary.sqlite'
    with contextlib.closing(sqlite3.connect(str(filename))) as db:
        db.execute('CREATE TABLE unrelated (value TEXT)')
        db.execute("INSERT INTO unrelated VALUES ('preserved')")
        db.commit()
    monkeypatch.setattr(
        pmxbot,
        'config',
        {'database': 'sqlite:' + str(filename), 'tendie_author': 'ExampleAccount'},
    )
    with contextlib.closing(tendie.Collection()) as store:
        assert isinstance(store, SQLiteStorage)
        assert store.filename == str(filename)
        assert store.db.isolation_level is None
        assert store.db.execute('PRAGMA busy_timeout').fetchone()[0] == 20000
        store.sync(provider(post()))
    with contextlib.closing(tendie.Collection()) as store:
        assert store.size() == 1
        assert (
            store.db.execute('SELECT value FROM unrelated').fetchone()[0] == 'preserved'
        )
        assert {
            row[0]
            for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        } == {'unrelated', 'tendie_posts', 'tendie_sync'}
    assert {path.name for path in tmp_path.iterdir()} == {'primary.sqlite'}


def test_cli_and_command_share_configured_primary_database(
    collection, tmp_path, monkeypatch, capsys
):
    config = tmp_path / 'bot.yaml'
    config.write_text(
        'database: '
        + pmxbot.config['database']
        + '\ntendie_author: !env TENDIE_AUTHOR\ntendie_include_replies: true\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('TENDIE_AUTHOR', 'ExampleAccount')
    filename = tmp_path / 'posts.json'
    filename.write_text(
        json.dumps([post(referenced_tweets=[{'type': 'replied_to'}])]), encoding='utf-8'
    )
    monkeypatch.setattr('sys.argv', ['tendie', str(filename), '--config', str(config)])
    tendie.main()
    assert 'Imported 1 posts.' in capsys.readouterr().out
    assert tendie.tendie('latest').endswith('/1')
    assert '1 posts' in tendie.tendie('status')
    assert collection.meta('last_success')
    allow_sync(collection)
    tendie.main()
    assert 'Imported 0 posts.' in capsys.readouterr().out


def test_provider_fetch_does_not_lock_primary_database(collection):
    with collection.db:
        collection.db.execute('CREATE TABLE unrelated (value TEXT)')

    def posts(since_id):
        with contextlib.closing(sqlite3.connect(collection.filename, timeout=0)) as db:
            db.execute("INSERT INTO unrelated VALUES ('during fetch')")
            db.commit()
        yield post()

    assert collection.sync(Mock(posts=posts)) == 1
    assert (
        collection.db.execute('SELECT value FROM unrelated').fetchone()[0]
        == 'during fetch'
    )
    assert collection.meta('sync_owner') == ''
    assert collection.meta('sync_started') == ''


def test_durable_lease_blocks_other_process_and_recovers(collection, monkeypatch):
    # Model a different process, which has its own Python synchronization lock.
    monkeypatch.setattr(tendie, '_sync_lock', threading.Lock())
    with collection.db:
        collection.db.execute('BEGIN IMMEDIATE')
        collection.set_meta('sync_started', NOW.isoformat())
        collection.set_meta('sync_owner', 'another-process')
    with pytest.raises(tendie.SyncError, match='already running'):
        collection.sync(provider(post()))
    monkeypatch.setattr(tendie, 'now', lambda: NOW + datetime.timedelta(minutes=30))
    assert collection.sync(provider(post())) == 1
    assert collection.meta('sync_owner') == ''


@pytest.mark.parametrize('uri', ['mongodb://localhost/pmxbot', 'sqlite::memory:'])
def test_unsupported_primary_database_does_not_create_fallback(
    collection, monkeypatch, uri
):
    monkeypatch.setitem(pmxbot.config, 'database', uri)
    assert 'persistent SQLite primary database' in tendie.tendie('')


@pytest.mark.parametrize('kind', ['replied_to', 'quoted'])
@pytest.mark.parametrize('bootstrap', [False, True])
def test_live_checkpoint_includes_filtered_posts(api, collection, kind, bootstrap):
    if not bootstrap:
        collection.sync(provider(post()), live=True)
        allow_sync(collection)
    if kind == 'quoted':
        pmxbot.config['tendie_include_quotes'] = False
    filtered = dict(xpost('2'), referenced_tweets=[{'type': kind}])
    api.side_effect = [api_response({'id': '42'}), api_response([filtered], meta={})]
    assert live_update() == 'Tendie: imported 0 posts.'
    assert collection.meta('live_since_id') == '2'
    assert collection.size() == (0 if bootstrap else 1)
    assert api.call_args.kwargs['params'].get('since_id') == (
        None if bootstrap else '1'
    )
    allow_sync(collection)
    api.side_effect = [
        api_response({'id': '42'}),
        api_response(meta={'result_count': 0}),
    ]
    assert live_update() == 'Tendie: imported 0 posts.'
    assert api.call_args.kwargs['params']['since_id'] == '2'
    assert collection.meta('live_since_id') == '2'
    with contextlib.closing(tendie.Collection()) as reopened:
        assert reopened.meta('live_since_id') == '2'


@pytest.mark.parametrize('failure', ['timeout', 'invalid_post', 'partial_response'])
def test_failed_live_update_preserves_checkpoint_and_rows(api, collection, failure):
    collection.sync(provider(post()), live=True)
    before = collection.meta('last_success')
    allow_sync(collection)
    first_page = api_response(
        [dict(xpost('10'), referenced_tweets=[{'type': 'replied_to'}])],
        meta={'next_token': 'next'},
    )
    last_page = {
        'timeout': requests.Timeout('secret'),
        'invalid_post': api_response(
            [dict(xpost('11'), created_at='invalid')], meta={}
        ),
        'partial_response': api_response(
            [xpost('11')], errors=[{'detail': 'secret'}], meta={}
        ),
    }[failure]
    api.side_effect = [api_response({'id': '42'}), first_page, last_page]
    assert 'no posts imported' in live_update()
    assert collection.meta('live_since_id') == '1'
    assert collection.meta('last_success') == before
    assert collection.size() == 1
    allow_sync(collection)
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('2')], meta={})]
    assert 'imported 1' in live_update()
    assert api.call_args.kwargs['params']['since_id'] == '1'
    assert collection.meta('live_since_id') == '2'


def test_checkpoint_uses_numeric_maximum_and_never_regresses(collection):
    posts = [
        post('100000000000000000000', referenced_tweets=[{'type': 'replied_to'}]),
        post('99999999999999999999'),
    ]
    assert collection.sync(provider(*posts), live=True) == 1
    assert collection.meta('live_since_id') == '100000000000000000000'
    allow_sync(collection)
    older = provider(post('2'))
    assert collection.sync(older, live=True) == 1
    older.posts.assert_called_once_with('100000000000000000000')
    assert collection.meta('live_since_id') == '100000000000000000000'


@pytest.mark.parametrize('extension', ['json', 'csv'])
@pytest.mark.parametrize('existing_checkpoint', [False, True])
def test_offline_backfill_does_not_advance_live_checkpoint(
    collection, tmp_path, extension, existing_checkpoint
):
    if existing_checkpoint:
        collection.sync(provider(post()), live=True)
        allow_sync(collection)
    filename = tmp_path / ('backfill.' + extension)
    if extension == 'json':
        filename.write_text(json.dumps([post('100')]), encoding='utf-8')
    else:
        filename.write_text(
            'id,text,author,created_at\n100,backfill,ExampleAccount,2026-10-17T12:00:00Z\n',
            encoding='utf-8',
        )
    backfill = tendie.FileProvider(filename)
    backfill.posts = Mock(wraps=backfill.posts)
    assert collection.sync(backfill) == 1
    backfill.posts.assert_called_once_with(None)
    assert collection.meta('live_since_id') == ('1' if existing_checkpoint else None)
    allow_sync(collection)
    live = provider(post('2'))
    assert collection.sync(live, live=True) == 1
    live.posts.assert_called_once_with('1' if existing_checkpoint else None)
    assert collection.meta('live_since_id') == '2'
    assert collection.size() == (3 if existing_checkpoint else 2)


def test_live_checkpoint_is_account_specific(collection):
    collection.sync(provider(post()), live=True)
    with contextlib.closing(tendie.Collection(author='AnotherAccount')) as other:
        source = provider(
            dict(
                post('9', referenced_tweets=[{'type': 'replied_to'}]),
                author='AnotherAccount',
            )
        )
        assert other.sync(source, live=True) == 0
        source.posts.assert_called_once_with(None)
        assert other.meta('live_since_id') == '9'
    assert collection.meta('live_since_id') == '1'


def test_checkpoint_and_import_commit_atomically(collection):
    collection.sync(provider(post()), live=True)
    before = collection.meta('last_success')
    allow_sync(collection)
    collection.db.execute(
        "CREATE TRIGGER fail_checkpoint BEFORE INSERT ON tendie_sync WHEN NEW.key LIKE '%:live_since_id' BEGIN SELECT RAISE(ABORT, 'checkpoint write failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match='checkpoint write failed'):
        collection.sync(provider(post('2')), live=True)
    assert collection.size() == 1
    assert collection.meta('live_since_id') == '1'
    assert collection.meta('last_success') == before
    assert collection.meta('sync_owner') == ''


@pytest.mark.parametrize(
    'url,expected',
    [
        (None, None),
        ('https://bot.example/bot', 'https://bot.example/bot/admin/tendies'),
        ('https://bot.example/bot/', 'https://bot.example/bot/admin/tendies'),
        ('http://bot.example:8080/bot', 'http://bot.example:8080/bot/admin/tendies'),
        ('https://bot.example', 'https://bot.example/admin/tendies'),
        ('', None),
        ('/relative', None),
        ('//bot.example/bot', None),
        ('ftp://bot.example/bot', None),
        ('https:///bot', None),
        ('https://[invalid', None),
        ('https://bot.example:invalid', None),
        ('https://bot.example:65536', None),
        ('https://user:secret@bot.example', None),
        ('https://user@bot.example', None),
        ('https://@bot.example', None),
        (' https://bot.example', None),
        ('https://bot.example/a b', None),
        ('https://bot.\nexample', None),
        ('https://bot.example/\x00', None),
        ('https://bot.example/?redirect=attacker', None),
        ('https://bot.example/?', None),
        ('https://bot.example/#fragment', None),
        ('https://bot.example/#', None),
        ('https://bot.example\\@attacker.example', None),
        (['https://bot.example'], None),
    ],
)
def test_irc_update_only_links_to_website(collection, monkeypatch, url, expected):
    pmxbot.config.update(
        albums_url=url, web_base='/bot', **{'logs URL': 'https://logs.example/bot'}
    )
    sync = Mock()
    monkeypatch.setattr(tendie, 'synchronize', sync)
    before = [tuple(row) for row in collection.db.execute('SELECT * FROM tendie_sync')]
    result = tendie.tendie('update')
    message = 'Administrators must use the website to update tendies.'
    assert result == message + (' ' + expected if expected else '')
    sync.assert_not_called()
    tendie.requests.get.assert_not_called()
    assert [
        tuple(row) for row in collection.db.execute('SELECT * FROM tendie_sync')
    ] == before


@pytest.mark.parametrize('operator', ['=', '+=', '-='])
@pytest.mark.parametrize('url', [None, 'https://bot.example/bot'])
@pytest.mark.parametrize('key', ['albums_url', 'database', 'ordinary_setting'])
def test_public_config_cannot_redirect_administrator_link(
    collection, monkeypatch, operator, url, key
):
    pmxbot.config['albums_url'] = url
    pmxbot.config['ordinary_setting'] = ['original']
    before = copy.deepcopy(pmxbot.config)
    message = f'!config {key} {operator} https://attacker.example'
    assert not list(core.CommandHandler.find_matching(message, '#test'))
    handler = next(h for h in core.Handler._registry if h.func is tendie.tendie)
    monkeypatch.setattr(core.Handler, '_registry', [handler])

    class Bot(core.Bot):
        _nickname = 'bot'
        result = None

        def transmit(self, channel, message):
            self.result = str(message)
            return message

    bot = Bot()
    bot.handle_action('#test', 'attacker', message)
    assert bot.result is None
    assert pmxbot.config == before
    bot.handle_action('#test', 'anyone', '!tendie update')
    assert 'attacker.example' not in bot.result
    message = 'Administrators must use the website to update tendies.'
    assert bot.result == message + (f' {url}/admin/tendies' if url else '')
    tendie.requests.get.assert_not_called()


@pytest.mark.parametrize('action', ['', 'latest', 'days', 'status'])
def test_public_reads_through_normal_irc_dispatch(collection, monkeypatch, action):
    collection.sync(provider(post()))
    handler = next(h for h in core.Handler._registry if h.func is tendie.tendie)
    monkeypatch.setattr(core.Handler, '_registry', [handler])

    class Bot(core.Bot):
        _nickname = 'bot'

        def transmit(self, channel, message):
            self.result = str(message)
            return message

    bot = Bot()
    bot.handle_action('#test', 'anyone', '!tendie ' + action)
    expected = '1 posts' if action == 'status' else '/1'
    assert expected in bot.result
    tendie.requests.get.assert_not_called()
