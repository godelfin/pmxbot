"""Regular-session FUN prices from Twelve Data with IRC performance colors."""

import datetime as dt
import os
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache

import pandas_market_calendars as mcal
import pytz
import requests

import pmxbot
from .core import command

UTC = dt.timezone.utc
EASTERN = pytz.timezone('America/New_York')


class QuoteUnavailable(ValueError):
    """No trustworthy regular-session price is available."""


@dataclass(frozen=True)
class Session:
    date: dt.date
    previous: dt.date
    opening: dt.datetime
    closing: dt.datetime
    live: bool


@dataclass(frozen=True)
class Quote:
    price: Decimal
    previous_close: Decimal
    timestamp: dt.datetime
    session: Session

    @property
    def change(self):
        return self.price - self.previous_close

    @property
    def percent(self):
        return self.change / self.previous_close * 100


@lru_cache(maxsize=8)
def schedule(day):
    return mcal.get_calendar('NYSE').schedule(
        start_date=day - dt.timedelta(days=32), end_date=day
    )


def market_session(now):
    """Select today's open session or the most recently completed session."""
    rows = list(schedule(now.astimezone(EASTERN).date()).iterrows())
    for index in range(len(rows) - 1, 0, -1):
        date, row = rows[index]
        opening = row['market_open'].to_pydatetime()
        closing = row['market_close'].to_pydatetime()
        if now >= opening:
            return Session(
                date.date(), rows[index - 1][0].date(), opening, closing, now < closing
            )
    raise QuoteUnavailable('NYSE calendar unavailable')


def positive_decimal(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise QuoteUnavailable('Invalid price or previous close')
    return number


def request_data(key, endpoint, **params):
    response = requests.get(
        'https://api.twelvedata.com/' + endpoint,
        headers={'Authorization': f'apikey {key}'},
        params={'symbol': 'FUN', 'exchange': 'NYSE', 'prepost': 'false', **params},
        timeout=(3.05, 10),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get('status') == 'error':
        raise QuoteUnavailable('Market data unavailable')
    return data


def validate_instrument(data):
    if (
        data['symbol'] != 'FUN'
        or data['exchange'] != 'NYSE'
        or data['currency'] != 'USD'
    ):
        raise QuoteUnavailable('Unexpected instrument')


def fetch_quote(key, session, now):
    """Never use an intraday/extended-hours quote as a completed daily close."""
    if session.live:
        data = request_data(key, 'quote')
        validate_instrument(data)
        timestamp = dt.datetime.fromtimestamp(int(data['timestamp']), UTC)
        if data.get(
            'is_extended_hours', False
        ) is not False or not session.opening <= timestamp <= min(now, session.closing):
            raise QuoteUnavailable('No current regular-session quote')
        return Quote(
            positive_decimal(data['close']),
            positive_decimal(data['previous_close']),
            timestamp,
            session,
        )

    # Allow delayed feeds to finish the daily candle before calling it a close.
    if now < session.closing + dt.timedelta(minutes=15):
        raise QuoteUnavailable(
            'Final closing price pending; try again after 15 minutes'
        )
    data = request_data(
        key,
        'time_series',
        interval='1day',
        # Daily history excludes end_date: use midnight after the target day.
        end_date=(session.date + dt.timedelta(days=1)).isoformat(),
        outputsize=2,
        order='desc',
        adjust='none',
    )
    validate_instrument(data['meta'])
    latest, previous = data['values']
    if (
        latest['datetime'] != session.date.isoformat()
        or previous['datetime'] != session.previous.isoformat()
    ):
        raise QuoteUnavailable('Completed daily prices unavailable')
    return Quote(
        positive_decimal(latest['close']),
        positive_decimal(previous['close']),
        session.closing,
        session,
    )


class QuoteService:
    """Bounded, synchronized cache; session changes always invalidate it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.identity = None
        self.expires = dt.datetime.min.replace(tzinfo=UTC)
        self.quote = None
        self.error = None

    def get(self, key, now):
        session = market_session(now)
        identity = (key, session.date, session.live)
        with self.lock:
            if self.identity != identity or now >= self.expires:
                self.identity = identity
                self.quote = None
                self.error = None
                try:
                    self.quote = fetch_quote(key, session, now)
                except (
                    requests.RequestException,
                    ValueError,
                    KeyError,
                    TypeError,
                    OverflowError,
                    OSError,
                    InvalidOperation,
                ) as exc:
                    # Do not expose provider errors, request URLs or credentials.
                    self.error = (
                        str(exc)
                        if isinstance(exc, QuoteUnavailable)
                        else 'Market data unavailable'
                    )
                ttl = 60 if session.live or self.error else 900
                self.expires = now + dt.timedelta(seconds=ttl)
            if self.error:
                raise QuoteUnavailable(self.error)
            return self.quote


def rounded(value):
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def format_quote(quote, now):
    """Always show the source time; re-evaluate age even for cached quotes."""
    change = rounded(quote.change)
    percent = rounded(quote.percent)
    sign = '+' if change > 0 else '-' if change < 0 else ''
    percent_sign = '+' if percent > 0 else '-' if percent < 0 else ''
    performance = f'{sign}${abs(change):.2f} ({percent_sign}{abs(percent):.2f}%)'
    if change:
        color = '03' if change > 0 else '04'
        performance = f'\x03{color}{performance}\x03'
    timestamp = quote.timestamp.astimezone(EASTERN)
    if quote.session.live:
        age = int((now - quote.timestamp).total_seconds() // 60)
        status = f' | delayed/stale {age}m' if age >= 3 else ''
        when = f'today | NYSE: as of {timestamp:%b %d %Y %I:%M %p} ET{status}'
    else:
        when = f'at last close | {timestamp:%b %d %Y %I:%M %p} ET'
    return f'FUN (Six Flags): ${rounded(quote.price):.2f} | {performance} {when}'


service = QuoteService()


@command(name='fun')
def fun():
    "Report Six Flags (NYSE: FUN) regular-session price and daily performance."
    key = pmxbot.config.get('twelve_data_api_key') or os.environ.get(
        'TWELVE_DATA_API_KEY'
    )
    if not key:
        return 'FUN: Configure twelve_data_api_key or TWELVE_DATA_API_KEY.'
    now = dt.datetime.now(UTC)
    try:
        return format_quote(service.get(key, now), now)
    except (QuoteUnavailable, InvalidOperation):
        return (
            'FUN: Quote unavailable (or final close pending). Please try again later.'
        )
