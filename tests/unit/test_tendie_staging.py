"""Paid responses survive process boundaries; all X calls are mocked."""

import contextlib
import datetime
import json
import sqlite3

import pytest
import requests

import pmxbot
from pmxbot import tendie, tendie_staging
from tests.unit.test_tendie import (  # noqa: F401 - shared fixtures
    api,
    collection,
    retry_sleep,
    api_response,
    xpost,
    post,
    provider,
    allow_sync,
    live_update,
    hydration_failure,
    first_nine_pages,
)


class Crash(BaseException):
    """Emulate termination without normal exception reporting."""


def replay():
    return tendie.synchronize(
        tendie.settings_snapshot(pmxbot.config), operation='recover'
    )


def staged(store):
    return store.db.execute('SELECT * FROM tendie_pages ORDER BY id').fetchall()


def stop_after_staging(monkeypatch):
    original = tendie_staging.LiveRun.stage

    def stage(self, *args):
        original(self, *args)
        raise Crash()

    monkeypatch.setattr(tendie_staging.LiveRun, 'stage', stage)


def test_page_ten_failure_resume_and_offline_backfill_are_independent(api, collection):
    api.side_effect = (
        [api_response({'id': '42'})]
        + first_nine_pages()
        + [requests.Timeout('private')]
    )
    assert '9 posts imported' in live_update()
    assert collection.size() == 9
    assert len(staged(collection)) == 9
    assert collection.meta('live_since_id') is None
    assert collection.meta('last_live_success') is None
    # An offline backfill must neither close the interrupted range nor set its checkpoint.
    allow_sync(collection)
    collection.sync(provider(post('1000')))
    assert collection.meta('live_since_id') is None
    allow_sync(collection)
    api.reset_mock()
    api.side_effect = [api_response([xpost('1')], meta={})]
    assert 'imported 1' in live_update()
    assert api.call_count == 1
    assert api.call_args.kwargs['params']['pagination_token'] == 'page-10'
    assert collection.size() == 11
    assert collection.meta('live_since_id') == '10'
    assert collection.meta('last_live_success')
    assert collection.progress()['status'] == 'complete'


def test_partial_pages_preserved_but_never_replayed_as_originals(
    api, collection, retry_sleep
):
    api.side_effect = [api_response({'id': '42'})] + [
        hydration_failure() for _ in range(4)
    ]
    assert '0 posts imported' in live_update()
    assert len(staged(collection)) == 4
    for page in staged(collection):
        body = json.loads(page['body'])
        assert body['data'][0]['id'] == '999'
        assert body['errors'][0]['type'].endswith('/field-hydration-failure')
        assert 'referenced_tweets' in json.loads(page['params'])['tweet.fields']
    api.reset_mock()
    assert replay() == 0
    assert collection.size() == 0
    assert collection.meta('live_since_id') is None
    assert collection.progress()['unresolved_pages'] == 1
    api.assert_not_called()
    # An error-free replacement says it was a reply. The saved policy still excludes it.
    pmxbot.config['tendie_include_replies'] = True
    allow_sync(collection)
    api.side_effect = [
        api_response(
            [dict(xpost('999'), referenced_tweets=[{'type': 'replied_to'}])], meta={}
        )
    ]
    assert 'imported 0' in live_update()
    assert collection.size() == 0
    assert collection.meta('live_since_id') == '999'
    assert len(staged(collection)) == 5


@pytest.mark.parametrize('final', [False, True])
def test_crash_after_durable_stage_local_replay_no_token_no_provider(
    api, collection, monkeypatch, final
):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response(
            [dict(xpost('8'), url='https://attacker.example', text='line\n\x01 &amp;')],
            meta={} if final else {'next_token': 'resume-here'},
        ),
    ]
    with monkeypatch.context() as patch:
        stop_after_staging(patch)
        with pytest.raises(Crash):
            tendie.synchronize(tendie.settings_snapshot(pmxbot.config))
    assert collection.size() == 0 and len(staged(collection)) == 1
    attempt = collection.meta('last_attempt')
    pmxbot.config.pop('x_bearer_token')
    api.reset_mock()
    api.side_effect = AssertionError('Local replay must never access X')
    assert replay() == 1
    assert replay() == 0  # Applied-page state and insertion are idempotent.
    api.assert_not_called()
    assert collection.meta('last_attempt') == attempt
    assert collection.size() == 1
    assert collection.meta('live_since_id') == ('8' if final else None)
    assert collection.progress()['unresolved_pages'] == (0 if final else 1)
    assert tendie.tendie('latest') == 'line & https://x.com/ExampleAccount/status/8'
    with contextlib.closing(tendie.Collection(pmxbot.config.database)) as reopened:
        assert reopened.size() == 1 and len(staged(reopened)) == 1


@pytest.mark.parametrize('boundary', ['posts', 'position', 'checkpoint'])
def test_crashes_during_page_transaction_roll_back_only_that_page(
    api, collection, boundary
):
    triggers = {
        'posts': "BEFORE INSERT ON tendie_posts WHEN NEW.id='7'",
        'position': "BEFORE UPDATE ON tendie_runs WHEN NEW.complete=1",
        'checkpoint': "BEFORE INSERT ON tendie_sync WHEN NEW.key LIKE '%:live_since_id'",
    }
    collection.db.execute(
        'CREATE TRIGGER crash '
        + triggers[boundary]
        + " BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"
    )
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('10')], meta={'next_token': 'next'}),
        api_response([xpost('8'), xpost('7')], meta={}),
    ]
    assert 'Incomplete run' in live_update()
    assert collection.size() == 1
    assert collection.progress()['processed_pages'] == 1
    assert len(staged(collection)) == 2
    assert collection.meta('live_since_id') is None
    collection.db.execute('DROP TRIGGER crash')
    api.reset_mock()
    assert replay() == 2
    api.assert_not_called()
    assert collection.size() == 3
    assert collection.progress()['processed_pages'] == 2
    assert collection.meta('live_since_id') == '10'


def test_failed_stage_commit_never_advances_or_requests_next_page(api, collection):
    collection.db.execute(
        "CREATE TRIGGER crash BEFORE INSERT ON tendie_pages BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('8')], meta={'next_token': 'must-not-use'}),
    ]
    assert 'Incomplete run' in live_update()
    assert api.call_count == 2
    assert collection.progress()['processed_pages'] == 0
    assert not staged(collection)
    assert collection.meta('live_since_id') is None
    collection.db.execute('DROP TRIGGER crash')
    assert replay() == 0  # A response that never committed cannot be recovered.
    allow_sync(collection)
    api.reset_mock()
    api.side_effect = [api_response([xpost('8')], meta={})]
    assert 'imported 1' in live_update()
    assert api.call_count == 1
    assert 'pagination_token' not in api.call_args.kwargs['params']


def test_crash_after_page_commit_resumes_next_position(api, collection):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('8')], meta={'next_token': 'next'}),
        Crash(),
    ]
    with pytest.raises(Crash):
        tendie.synchronize(tendie.settings_snapshot(pmxbot.config))
    assert collection.size() == 1
    assert collection.progress()['processed_pages'] == 1
    api.reset_mock()
    assert replay() == 0
    api.assert_not_called()
    allow_sync(collection)
    api.side_effect = [api_response([xpost('7')], meta={})]
    assert 'imported 1' in live_update()
    assert api.call_args.kwargs['params']['pagination_token'] == 'next'
    assert collection.meta('live_since_id') == '8'


@pytest.mark.parametrize('status', [400, 410])
def test_rejected_cursor_requires_explicit_bounded_range_recovery(
    api, collection, status
):
    collection.sync(provider(post()), live=True)
    allow_sync(collection)
    rejected = api_response([xpost('7')], errors=[{'detail': 'private cursor error'}])
    rejected.status_code = status
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('9'), xpost('8')], meta={'next_token': 'expired'}),
        rejected,
    ]
    assert 'rejected the saved pagination token' in live_update()
    assert collection.size() == 3
    assert collection.meta('live_since_id') == '1'
    assert collection.progress()['can_recover_range']
    assert len(staged(collection)) == 2
    allow_sync(collection)
    api.reset_mock()
    assert 'rejected' in live_update()
    assert replay() == 0
    api.assert_not_called()
    allow_sync(collection)
    pmxbot.config['tendie_include_replies'] = True
    api.side_effect = [
        api_response(
            [dict(xpost('7'), referenced_tweets=[{'type': 'replied_to'}]), xpost('6')],
            meta={'next_token': 'older'},
        ),
        api_response([xpost('2')], meta={}),
    ]
    assert (
        tendie.synchronize(
            tendie.settings_snapshot(pmxbot.config), operation='recover_range'
        )
        == 2
    )
    assert api.call_count == 2
    first, second = api.call_args_list
    assert first.kwargs['params']['until_id'] == '8'
    assert first.kwargs['params']['since_id'] == '1'
    assert 'pagination_token' not in first.kwargs['params']
    assert second.kwargs['params']['pagination_token'] == 'older'
    assert second.kwargs['params']['until_id'] == '8'
    assert collection.size() == 5
    assert collection.meta('live_since_id') == '9'
    assert collection.progress()['status'] == 'complete'


@pytest.mark.parametrize('status', [402, 403, 429, 500])
def test_paid_access_or_rate_failures_keep_progress_and_stop(api, collection, status):
    failed = api_response(
        errors=[{'detail': 'test-secret', 'Authorization': 'Bearer test-secret'}]
    )
    failed.status_code = status
    failed.raise_for_status.side_effect = requests.HTTPError('private credit detail')
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('8')], meta={'next_token': 'next'}),
        failed,
    ]
    result = live_update()
    assert '1 posts imported in this run' in result
    assert 'no posts imported' not in result
    assert api.call_count == 3
    assert collection.size() == 1
    assert collection.meta('live_since_id') is None
    assert staged(collection)[-1]['http_status'] == status
    raw = ' '.join(row['body'] + row['params'] for row in staged(collection))
    assert 'test-secret' not in raw
    assert 'Authorization' not in raw
    assert 'private credit detail' not in result


def test_replay_and_resume_are_source_isolated(api, collection, monkeypatch):
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('8')], meta={})]
    with monkeypatch.context() as patch:
        stop_after_staging(patch)
        with pytest.raises(Crash):
            tendie.synchronize(tendie.settings_snapshot(pmxbot.config))
    api.reset_mock()
    with contextlib.closing(tendie.Collection(author='AnotherAccount')) as other:
        assert other.sync(None, live=True, operation='recover') == 0
        assert other.size() == 0 and other.progress() is None
        assert other.meta('live_since_id') is None
    assert replay() == 1
    api.assert_not_called()
    assert collection.meta('live_since_id') == '8'


def test_cli_local_replay_uses_saved_page_without_credentials(
    api, collection, monkeypatch, capsys
):
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('8')], meta={})]
    with monkeypatch.context() as patch:
        stop_after_staging(patch)
        with pytest.raises(Crash):
            tendie.synchronize(tendie.settings_snapshot(pmxbot.config))
    monkeypatch.setattr(
        'sys.argv',
        [
            'tendie',
            '--recover',
            '--database',
            pmxbot.config.database,
            '--author',
            'ExampleAccount',
        ],
    )
    pmxbot.config.pop('x_bearer_token')
    api.reset_mock()
    tendie.main()
    assert 'Imported 1 posts.' in capsys.readouterr().out
    api.assert_not_called()


def test_lease_loss_after_paid_response_still_saves_response(
    api, collection, monkeypatch
):
    def replace_owner(url, **kwargs):
        if 'by/username' in url:
            return api_response({'id': '42'})
        with contextlib.closing(tendie.Collection()) as other:
            other.set_meta('sync_owner', 'replacement-owner')
        return api_response([xpost('8')], meta={})

    api.side_effect = replace_owner
    assert 'lease expired' in live_update()
    assert len(staged(collection)) == 1
    assert collection.size() == 0
    assert collection.meta('sync_owner') == 'replacement-owner'


def test_duplicate_posts_across_saved_pages_and_new_runs_are_idempotent(
    api, collection
):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('9'), xpost('8')], meta={'next_token': 'next'}),
        requests.Timeout('private'),
    ]
    assert '2 posts imported' in live_update()
    assert collection.meta('live_since_id') is None
    allow_sync(collection)
    api.reset_mock()
    api.side_effect = [api_response([xpost('8'), xpost('7')], meta={})]
    assert 'imported 1' in live_update()
    assert api.call_count == 1
    assert collection.size() == 3
    assert collection.meta('live_since_id') == '9'
    assert collection.progress()['imported'] == 3
    allow_sync(collection)
    api.reset_mock()
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('12')], meta={})]
    assert 'imported 1' in live_update()
    assert api.call_args.kwargs['params']['since_id'] == '9'
    assert collection.size() == 4
    assert collection.db.execute('SELECT count(*) FROM tendie_runs').fetchone()[0] == 2


def test_lease_loss_during_backoff_prevents_another_paid_request(
    api, collection, retry_sleep
):
    def takeover(delay):
        with contextlib.closing(tendie.Collection()) as other:
            other.set_meta('sync_owner', 'replacement-owner')

    retry_sleep.side_effect = takeover
    api.side_effect = [api_response({'id': '42'}), hydration_failure()]
    assert 'lease expired' in live_update()
    assert api.call_count == 2
    assert len(staged(collection)) == 1
    assert collection.size() == 0


def test_cursor_recovery_without_validated_boundary_never_restarts_timeline(
    api, collection
):
    rejected = api_response(errors=[{'detail': 'expired'}])
    rejected.status_code = 400
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([], meta={'next_token': 'expired'}),
        rejected,
    ]
    assert 'rejected' in live_update()
    assert not collection.progress()['can_recover_range']
    allow_sync(collection)
    api.reset_mock()
    with pytest.raises(tendie.SyncError, match='validated boundary'):
        tendie.synchronize(
            tendie.settings_snapshot(pmxbot.config), operation='recover_range'
        )
    api.assert_not_called()
    assert collection.meta('live_since_id') is None


def test_failed_range_recovery_resumes_its_saved_bounds(api, collection):
    rejected = api_response(errors=[{'detail': 'expired'}])
    rejected.status_code = 400
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('9'), xpost('8')], meta={'next_token': 'expired'}),
        rejected,
    ]
    assert 'rejected' in live_update()
    allow_sync(collection)
    api.reset_mock()
    api.side_effect = requests.Timeout('private')
    with pytest.raises(tendie.SyncError, match='Incomplete run'):
        tendie.synchronize(
            tendie.settings_snapshot(pmxbot.config), operation='recover_range'
        )
    assert api.call_args.kwargs['params']['until_id'] == '8'
    assert collection.meta('live_since_id') is None
    assert collection.size() == 2
    allow_sync(collection)
    api.reset_mock()
    api.side_effect = [api_response([xpost('7')], meta={})]
    assert 'imported 1' in live_update()
    assert api.call_count == 1
    assert api.call_args.kwargs['params']['until_id'] == '8'
    assert 'pagination_token' not in api.call_args.kwargs['params']
    assert collection.meta('live_since_id') == '9'


def test_replay_reclaims_abandoned_lease_only_after_expiry(
    api, collection, monkeypatch
):
    api.side_effect = [api_response({'id': '42'}), api_response([xpost('8')], meta={})]
    with monkeypatch.context() as patch:
        stop_after_staging(patch)
        with pytest.raises(Crash):
            tendie.synchronize(tendie.settings_snapshot(pmxbot.config))
    current = tendie.now()
    collection.set_meta('sync_owner', 'dead-process')
    collection.set_meta('sync_started', current.isoformat())
    api.reset_mock()
    with pytest.raises(tendie.SyncError, match='already running'):
        replay()
    monkeypatch.setattr(tendie, 'now', lambda: current + datetime.timedelta(minutes=31))
    assert replay() == 1
    assert collection.meta('sync_owner') == ''
    assert collection.meta('live_since_id') == '8'
    api.assert_not_called()
