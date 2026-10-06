import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
import requests

import pmxbot
from pmxbot import core, openai_usage


def test_command_registration():
    assert (
        next(core.Handler.find_matching('!openaiusage', '#test')).func
        is openai_usage.openaiusage
    )


@pytest.fixture
def api(monkeypatch):
    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 29, 12, tzinfo=datetime.timezone.utc).astimezone(tz)

    monkeypatch.setattr(openai_usage.datetime, 'datetime', FixedDatetime)
    monkeypatch.setattr(pmxbot, 'config', {'openai_admin_key': 'test-secret'})
    monkeypatch.delenv('OPENAI_ADMIN_KEY', raising=False)
    get = Mock()
    monkeypatch.setattr(openai_usage.requests, 'get', get)
    return get


def response(values=(), **kwargs):
    page = {
        'data': [
            {
                'results': [
                    {'amount': {'value': value, 'currency': 'usd'}} for value in values
                ]
            }
        ],
        'has_more': False,
        'next_page': None,
    }
    page.update(kwargs)
    return Mock(json=Mock(return_value=page))


@pytest.mark.parametrize('offset', [0, 2, -7])
def test_cost_pagination_and_utc_range(api, offset):
    calls = []
    pages = iter(
        [response([0.1], has_more=True, next_page='next'), response([0.2, -0.01])]
    )

    def get(url, **kwargs):
        calls.append((url, {**kwargs, 'params': dict(kwargs['params'])}))
        return next(pages)

    api.side_effect = get
    utc_now = datetime.datetime(
        2026, 9, 29, 12, 34, 56, 123456, tzinfo=datetime.timezone.utc
    )
    now = utc_now.astimezone(datetime.timezone(datetime.timedelta(hours=offset)))
    assert openai_usage.today_cost('test-secret', now) == Decimal('0.29')
    url, kwargs = calls[0]
    assert url == 'https://api.openai.com/v1/organization/costs'
    assert kwargs['headers'] == {'Authorization': 'Bearer test-secret'}
    assert kwargs['timeout'] == 15
    assert kwargs['params'] == {
        'start_time': 1790080496,
        'end_time': 1790685296,
        'bucket_width': '1d',
        'limit': 1,
    }
    assert len(calls) == 2
    assert calls[1] == (url, {**kwargs, 'params': {**kwargs['params'], 'page': 'next'}})
    assert kwargs['params']['end_time'] - kwargs['params']['start_time'] == 604800


def test_command(api):
    api.return_value = response([1.25, 2])
    result = openai_usage.openaiusage()
    assert result == 'OpenAI: $3.25 USD used in the last 7 days.'


def test_no_usage(api):
    api.return_value = response(data=[])
    assert '$0.00 USD' in openai_usage.openaiusage()


def test_missing_key(api):
    pmxbot.config.clear()
    assert 'Configure openai_admin_key' in openai_usage.openaiusage()
    api.assert_not_called()


def test_environment_key(api, monkeypatch):
    pmxbot.config.clear()
    monkeypatch.setenv('OPENAI_ADMIN_KEY', 'environment-secret')
    api.return_value = response()
    openai_usage.openaiusage()
    assert (
        api.call_args.kwargs['headers']['Authorization'] == 'Bearer environment-secret'
    )


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_http_error(api, status):
    api.return_value.raise_for_status.side_effect = requests.HTTPError(
        'secret response', response=Mock(status_code=status)
    )
    result = openai_usage.openaiusage()
    assert ('access denied' if status in (401, 403) else 'unavailable') in result
    assert 'secret' not in result


def test_timeout(api):
    api.side_effect = requests.Timeout('secret')
    assert (
        openai_usage.openaiusage()
        == 'OpenAI usage is unavailable. Please try again later.'
    )


@pytest.mark.parametrize(
    'page',
    [
        {},
        {'data': None},
        response(['NaN']).json(),
        response([None]).json(),
        response(has_more=True).json(),
        response(has_more=True, next_page='repeated').json(),
        response(
            data=[{'results': [{'amount': {'value': 1, 'currency': 'eur'}}]}]
        ).json(),
    ],
)
def test_invalid_response(api, page):
    api.return_value = Mock(json=Mock(return_value=page))
    assert 'invalid usage response' in openai_usage.openaiusage()


def test_midnight(api):
    api.return_value = response([1.25])
    now = datetime.datetime(2026, 9, 29, tzinfo=datetime.timezone.utc)
    assert openai_usage.today_cost('test-secret', now) == Decimal('1.25')
    api.assert_called_once()
    assert api.call_args.kwargs['params'] == {
        'start_time': 1790035200,
        'end_time': 1790640000,
        'bucket_width': '1d',
        'limit': 1,
    }


def test_default_now(api, monkeypatch):
    now = datetime.datetime(2026, 9, 29, 12, tzinfo=datetime.timezone.utc)
    clock = Mock(wraps=datetime.datetime)
    clock.now.return_value = now
    monkeypatch.setattr(openai_usage.datetime, 'datetime', clock)
    api.return_value = response()
    assert openai_usage.today_cost('test-secret') == 0
    clock.now.assert_called_once_with(datetime.timezone.utc)
    params = api.call_args.kwargs['params']
    assert params['end_time'] == int(now.timestamp())
    assert params['start_time'] == int((now - datetime.timedelta(days=7)).timestamp())
    clock.now.reset_mock()
    openai_usage.today_cost('test-secret', now)
    clock.now.assert_not_called()
