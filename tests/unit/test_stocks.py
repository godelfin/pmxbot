import datetime as dt
from decimal import Decimal
from unittest.mock import Mock

import pytest
import requests

import pmxbot
from pmxbot import core, stocks


def instant(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=stocks.UTC)


OPEN = instant('2026-10-21T19:42:00')
CLOSED = instant('2026-10-21T21:00:00')


def intraday(**overrides):
    return dict(
        symbol='FUN',
        exchange='NYSE',
        currency='USD',
        timestamp=int(OPEN.timestamp()),
        close='32.45',
        previous_close='31.25',
        **overrides,
    )


def daily():
    return {
        'meta': {'symbol': 'FUN', 'exchange': 'NYSE', 'currency': 'USD'},
        'values': [
            {'datetime': '2026-10-21', 'close': '32.45'},
            {'datetime': '2026-10-20', 'close': '33.30'},
        ],
    }


@pytest.fixture
def api(monkeypatch):
    get = Mock(return_value=Mock(json=Mock(return_value=intraday())))
    monkeypatch.setattr(stocks.requests, 'get', get)
    monkeypatch.setattr(stocks, 'service', stocks.QuoteService())
    monkeypatch.setattr(pmxbot, 'config', {'twelve_data_api_key': 'secret'})
    monkeypatch.delenv('TWELVE_DATA_API_KEY', raising=False)
    return get


def test_registration():
    handler = next(core.Handler.find_matching('!fun', '#any-channel'))
    assert handler.func is stocks.fun


@pytest.mark.parametrize(
    'price,expected',
    [
        ('32.45', '+$1.20 (+3.84%)'),
        ('30.00', '-$1.25 (-4.00%)'),
        ('31.25', '$0.00 (0.00%)'),
    ],
)
def test_performance(api, price, expected):
    api.return_value.json.return_value['close'] = price
    quote = stocks.service.get('secret', OPEN)
    assert quote.price == Decimal(price)
    message = stocks.format_quote(quote, OPEN)
    assert expected in message
    assert 'today | NYSE: as of Oct 21 2026 03:42 PM ET' in message
    assert '\n' not in message
    kwargs = api.call_args.kwargs
    assert kwargs['timeout'] == (3.05, 10)
    assert kwargs['headers'] == {'Authorization': 'apikey secret'}
    assert kwargs['params'] == {'symbol': 'FUN', 'exchange': 'NYSE', 'prepost': 'false'}


@pytest.mark.parametrize(
    'now,day,live,close_hour',
    [
        ('2026-10-21T13:29:59', '2026-10-20', False, 20),
        ('2026-10-21T13:30:00', '2026-10-21', True, 20),
        ('2026-10-21T19:59:59', '2026-10-21', True, 20),
        ('2026-10-21T20:00:00', '2026-10-21', False, 20),
        ('2026-10-24T16:00:00', '2026-10-23', False, 20),
        ('2026-12-25T16:00:00', '2026-12-24', False, 18),
        ('2026-11-27T17:59:59', '2026-11-27', True, 18),
        ('2026-11-27T18:00:00', '2026-11-27', False, 18),
        ('2026-03-06T14:30:00', '2026-03-06', True, 21),
        ('2026-03-09T13:30:00', '2026-03-09', True, 20),
        ('2026-11-02T14:30:00', '2026-11-02', True, 21),
        ('2026-04-03T16:00:00', '2026-04-02', False, 20),
    ],
)
def test_calendar(now, day, live, close_hour):
    session = stocks.market_session(instant(now))
    assert session.date.isoformat() == day
    assert session.live is live
    assert session.closing.hour == close_hour


@pytest.mark.parametrize('now', [CLOSED, instant('2026-10-22T12:00:00')])
def test_daily_close(api, now):
    api.return_value.json.return_value = daily()
    quote = stocks.service.get('secret', now)
    assert quote.change == Decimal('-0.85')
    assert stocks.format_quote(quote, now) == (
        'FUN (Six Flags): $32.45 | -$0.85 (-2.55%) at last close | '
        'Oct 21 2026 04:00 PM ET'
    )
    assert api.call_args.args[0].endswith('/time_series')
    assert api.call_args.kwargs['params']['adjust'] == 'none'
    assert api.call_args.kwargs['params']['end_date'] == '2026-10-21'


def test_close_publication_grace(api):
    with pytest.raises(stocks.QuoteUnavailable):
        stocks.service.get('secret', instant('2026-10-21T20:05:00'))
    api.assert_not_called()


@pytest.mark.parametrize('minutes', [3, 16, 120])
def test_stale_quote(api, minutes):
    now = OPEN + dt.timedelta(minutes=minutes)
    # All chosen times remain in the regular session except 120; use an earlier quote.
    quote_time = OPEN - dt.timedelta(minutes=120)
    now = quote_time + dt.timedelta(minutes=minutes)
    api.return_value.json.return_value['timestamp'] = int(quote_time.timestamp())
    quote = stocks.service.get('secret', now)
    assert f'delayed/stale {minutes}m' in stocks.format_quote(quote, now)


@pytest.mark.parametrize(
    'field,value',
    [
        ('close', 'NaN'),
        ('close', '-1'),
        ('close', 'Infinity'),
        ('previous_close', '0'),
        ('previous_close', None),
        ('previous_close', ''),
        ('symbol', 'SIX'),
        ('exchange', 'NASDAQ'),
        ('currency', 'EUR'),
        ('timestamp', 0),
        ('timestamp', 'bad'),
        ('timestamp', 10**30),
        ('timestamp', int((OPEN + dt.timedelta(minutes=1)).timestamp())),
        ('is_extended_hours', True),
        ('is_extended_hours', 'true'),
    ],
)
def test_invalid_quote(api, field, value):
    api.return_value.json.return_value[field] = value
    with pytest.raises(stocks.QuoteUnavailable):
        stocks.service.get('secret', OPEN)


@pytest.mark.parametrize('field', ['close', 'previous_close', 'timestamp'])
def test_missing_quote_fields(api, field):
    del api.return_value.json.return_value[field]
    with pytest.raises(stocks.QuoteUnavailable):
        stocks.service.get('secret', OPEN)


@pytest.mark.parametrize('payload', [{}, None, [], {'status': 'error', 'code': 429}])
def test_invalid_payload(api, payload):
    api.return_value.json.return_value = payload
    with pytest.raises(stocks.QuoteUnavailable):
        stocks.service.get('secret', OPEN)


@pytest.mark.parametrize('index,date', [(0, '2026-10-20'), (1, '2026-10-19')])
def test_daily_dates_required(api, index, date):
    data = daily()
    data['values'][index]['datetime'] = date
    api.return_value.json.return_value = data
    with pytest.raises(stocks.QuoteUnavailable):
        stocks.service.get('secret', CLOSED)


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_http_failures(api, status):
    api.return_value.raise_for_status.side_effect = requests.HTTPError(
        'secret', response=Mock(status_code=status)
    )
    with pytest.raises(stocks.QuoteUnavailable, match='Market data unavailable'):
        stocks.service.get('secret', OPEN)


def test_timeout_and_error_cache(api):
    api.side_effect = requests.Timeout('secret')
    for seconds in [0, 30, 60]:
        with pytest.raises(stocks.QuoteUnavailable, match='Market data unavailable'):
            stocks.service.get('secret', OPEN + dt.timedelta(seconds=seconds))
    assert api.call_count == 2


@pytest.mark.parametrize(
    'now,ttl,payload', [(OPEN, 60, intraday()), (CLOSED, 900, daily())]
)
def test_cache_lifetime(api, now, ttl, payload):
    api.return_value.json.return_value = payload
    stocks.service.get('secret', now)
    stocks.service.get('secret', now + dt.timedelta(seconds=ttl - 1))
    api.assert_called_once()
    stocks.service.get('secret', now + dt.timedelta(seconds=ttl))
    assert api.call_count == 2


def test_cache_does_not_cross_close(api):
    stocks.service.get('secret', OPEN)
    api.return_value.json.return_value = daily()
    quote = stocks.service.get('secret', CLOSED)
    assert not quote.session.live
    assert api.call_count == 2


def test_cache_key_change(api):
    stocks.service.get('old-key', OPEN)
    stocks.service.get('new-key', OPEN)
    assert api.call_count == 2


def test_command_config_and_environment(api, monkeypatch):
    assert stocks.fun().startswith('FUN:')  # No wall-clock-dependent prices required.
    pmxbot.config.clear()
    api.reset_mock()
    assert 'Configure twelve_data_api_key' in stocks.fun()
    api.assert_not_called()
    monkeypatch.setenv('TWELVE_DATA_API_KEY', 'env-secret')
    service = Mock()
    service.get.side_effect = stocks.QuoteUnavailable('secret')
    monkeypatch.setattr(stocks, 'service', service)
    assert 'Quote unavailable' in stocks.fun()
    assert service.get.call_args.args[0] == 'env-secret'


def test_command_success(api, monkeypatch):
    quote = stocks.service.get('secret', OPEN)
    fake_service = Mock(get=Mock(return_value=quote))
    monkeypatch.setattr(stocks, 'service', fake_service)
    assert stocks.fun().startswith('FUN (Six Flags): $32.45 | +$1.20 (+3.84%) today')


@pytest.mark.parametrize(
    'now,dates',
    [
        ('2026-10-24T16:00:00', ('2026-10-23', '2026-10-22')),
        ('2026-12-25T16:00:00', ('2026-12-24', '2026-12-23')),
    ],
)
def test_weekend_and_holiday_daily_results(api, now, dates):
    payload = daily()
    for bar, date in zip(payload['values'], dates):
        bar['datetime'] = date
    api.return_value.json.return_value = payload
    quote = stocks.service.get('secret', instant(now))
    assert quote.change == Decimal('-0.85')
    assert 'at last close' in stocks.format_quote(quote, instant(now))


def test_cached_age_is_recomputed(api):
    quote = stocks.service.get('secret', OPEN)
    now = OPEN + dt.timedelta(minutes=3)
    assert 'delayed/stale 3m' in stocks.format_quote(quote, now)


def test_failed_refresh_does_not_serve_expired_data(api):
    stocks.service.get('secret', OPEN)
    api.side_effect = requests.Timeout('secret')
    with pytest.raises(stocks.QuoteUnavailable):
        stocks.service.get('secret', OPEN + dt.timedelta(seconds=60))


def test_cache_invalidates_at_open(api):
    now = instant('2026-10-22T13:29:59')
    api.return_value.json.return_value = daily()
    stocks.service.get('secret', now)
    now += dt.timedelta(seconds=1)
    payload = intraday()
    payload['timestamp'] = int(now.timestamp())
    api.return_value.json.return_value = payload
    quote = stocks.service.get('secret', now)
    assert quote.session.live
    assert api.call_count == 2


@pytest.mark.parametrize('values', [[], [{}], [{}, {}, {}], None])
def test_malformed_daily_bars(api, values):
    payload = daily()
    payload['values'] = values
    api.return_value.json.return_value = payload
    with pytest.raises(stocks.QuoteUnavailable):
        stocks.service.get('secret', CLOSED)
