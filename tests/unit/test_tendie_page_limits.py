"""Administrator page budgets bound paid work while preserving durable progress."""

from contextlib import closing

import pytest

import pmxbot
from pmxbot import tendie
from tests.unit.test_tendie import (  # noqa: F401 - shared fixtures
    api,
    collection,
    retry_sleep,
    api_response,
    xpost,
    allow_sync,
    live_update,
    hydration_failure,
)
from tests.unit.test_tendie_staging import Crash, stop_after_staging
from tests.unit.test_tendie_web import (  # noqa: F401 - shared fixture
    site,
    administrator,
    submit,
    state,
    metadata,
)
from tests.unit.test_web_auth import web  # noqa: F401 - used by site


def test_page_limit_defaults_to_one_and_can_increase_on_resume(site):
    web, executor = site
    administrator(web)
    assert 'required value="1"' in web.request('/admin/tendies')['body']
    tendie.requests.get.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('9')], meta={'next_token': 'second'}),
    ]
    assert submit(web)['status'] == 303
    executor.stop()
    result = state(web)
    assert tendie.requests.get.call_count == 2  # One lookup and one timeline page.
    assert result['count'] == 1 and result['page_limit'] == 1
    assert result['job']['status'] == 'incomplete'
    assert 'Paused after requesting 1 new timeline page.' in result['job']['message']
    assert result['last_live_success'] is None
    executor.start()
    assert submit(web, page_limit='2')['status'] == 303
    executor.stop()
    assert 'cooling down' in state(web)['job']['message']
    assert tendie.requests.get.call_count == 2
    with closing(tendie.Collection()) as store:
        assert store.meta('live_since_id') is None
        allow_sync(store)
    tendie.requests.get.reset_mock()
    tendie.requests.get.side_effect = [
        api_response([xpost('8')], meta={'next_token': 'third'}),
        api_response([xpost('7')], meta={'next_token': 'fourth'}),
    ]
    executor.start()
    assert submit(web, page_limit='2')['status'] == 303
    executor.stop()
    result = state(web)
    assert tendie.requests.get.call_count == 2
    assert result['count'] == 3 and result['page_limit'] == 2
    assert result['progress']['processed_pages'] == 3
    assert result['job']['status'] == 'incomplete'
    assert 'Paused after requesting 2 new timeline pages' in result['job']['message']
    assert 'required value="2"' in web.request('/admin/tendies')['body']
    first, second = tendie.requests.get.call_args_list
    assert first.kwargs['params']['pagination_token'] == 'second'
    assert second.kwargs['params']['pagination_token'] == 'third'
    assert 'page_limit' not in first.kwargs['params']
    with closing(tendie.Collection()) as store:
        assert store.meta('live_since_id') is None
        allow_sync(store)
    tendie.requests.get.reset_mock()
    tendie.requests.get.side_effect = [api_response([xpost('6')], meta={})]
    executor.start()
    assert submit(web, page_limit='1')['status'] == 303
    executor.stop()
    result = state(web)
    assert tendie.requests.get.call_count == 1
    assert (
        tendie.requests.get.call_args.kwargs['params']['pagination_token'] == 'fourth'
    )
    assert result['count'] == 4 and result['job']['status'] == 'succeeded'
    assert result['last_live_success']
    with closing(tendie.Collection()) as store:
        assert store.meta('live_since_id') == '9'


@pytest.mark.parametrize('value', ['0', '101', '-1', '1.5', '', 'abc', '１'])
def test_invalid_page_budget_never_queues_or_consumes_cooldown(site, value):
    web, executor = site
    administrator(web)
    before = metadata()
    assert submit(web, page_limit=value)['status'] == 400
    assert metadata() == before
    assert not executor._active
    tendie.requests.get.assert_not_called()


def test_saved_page_replay_does_not_spend_new_page_budget(api, collection, monkeypatch):
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('9')], meta={'next_token': 'second'}),
    ]
    with monkeypatch.context() as patch:
        stop_after_staging(patch)
        with pytest.raises(Crash):
            tendie.synchronize(tendie.settings_snapshot(pmxbot.config))
    allow_sync(collection)
    api.reset_mock()
    api.side_effect = [api_response([xpost('8')], meta={'next_token': 'third'})]
    assert (
        tendie.synchronize(tendie.settings_snapshot(pmxbot.config), page_limit=1) == 2
    )
    assert api.call_count == 1
    assert api.call_args.kwargs['params']['pagination_token'] == 'second'
    assert collection.size() == 2
    assert collection.progress()['processed_pages'] == 2
    assert collection.meta('live_since_id') is None
    assert 'Paused after requesting 1' in collection.progress()['message']


def test_hydration_retries_stay_within_one_page_position(api, collection, retry_sleep):
    api.side_effect = (
        [api_response({'id': '42'})]
        + [hydration_failure() for _ in range(3)]
        + [api_response([xpost('9')], meta={'next_token': 'second'})]
    )
    assert (
        tendie.synchronize(tendie.settings_snapshot(pmxbot.config), page_limit=1) == 1
    )
    assert api.call_count == 5
    assert retry_sleep.call_count == 3
    assert collection.progress()['saved_responses'] == 4
    assert collection.progress()['processed_pages'] == 1
    assert collection.meta('live_since_id') is None


@pytest.mark.parametrize('value', [0, 101, True, 1.5])
def test_invalid_backend_budget_cannot_start_paid_work(api, collection, value):
    with pytest.raises(ValueError, match='page limit'):
        tendie.synchronize(tendie.settings_snapshot(pmxbot.config), page_limit=value)
    api.assert_not_called()
    assert collection.meta('last_attempt') is None
    assert collection.meta('sync_owner') is None


def test_expired_cursor_range_recovery_uses_selected_page_limit(api, collection):
    rejected = api_response(errors=[{'detail': 'expired'}])
    rejected.status_code = 400
    api.side_effect = [
        api_response({'id': '42'}),
        api_response([xpost('9')], meta={'next_token': 'expired'}),
        rejected,
    ]
    assert 'rejected' in live_update()
    allow_sync(collection)
    api.reset_mock()
    api.side_effect = [api_response([xpost('8')], meta={'next_token': 'older'})]
    assert (
        tendie.synchronize(
            tendie.settings_snapshot(pmxbot.config),
            operation='recover_range',
            page_limit=1,
        )
        == 1
    )
    assert api.call_count == 1
    assert api.call_args.kwargs['params']['until_id'] == '9'
    assert collection.progress()['status'] == 'incomplete'
    assert collection.meta('live_since_id') is None
