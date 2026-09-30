"""Organization spending reported by OpenAI's Costs API."""

import datetime
import time
import decimal
import os

import requests

import pmxbot

from .core import command


def today_cost(key, now):
    """Sum all reported costs from last seven days, following pagination."""
    # start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_time = int(time.time())
    start_time = end_time - (7* 24 * 60 * 60) # last 7 days
    params = {
        'start_time': start_time,
        'end_time': end_time,
        'bucket_width': '1d',
        'limit': 1,
    }
    total = decimal.Decimal(0)
    if params['start_time'] == params['end_time']:
        return total
    seen = set()
    while True:
        response = requests.get(
            'https://api.openai.com/v1/organization/costs',
            headers={'Authorization': f'Bearer {key}'},
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        page = response.json()
        for bucket in page['data']:
            for result in bucket['results']:
                amount = result['amount']
                if amount['currency'].lower() != 'usd':
                    raise ValueError('Unexpected currency')
                value = decimal.Decimal(str(amount['value']))
                if not value.is_finite():
                    raise ValueError('Invalid cost')
                total += value
        if not page['has_more']:
            return total
        cursor = page['next_page']
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError('Invalid pagination cursor')
        seen.add(cursor)
        params['page'] = cursor


@command(name='openaiusage')
def openaiusage():
    "Report today's OpenAI organization spending (UTC) and credit availability."
    key = pmxbot.config.get('openai_admin_key') or os.environ.get('OPENAI_ADMIN_KEY')
    if not key:
        return (
            'Configure openai_admin_key or OPENAI_ADMIN_KEY with an OpenAI admin key.'
        )
    try:
        cost = today_cost(key, datetime.datetime.now(datetime.timezone.utc))
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (401, 403):
            return (
                'OpenAI usage access denied. Check the admin key and its permissions.'
            )
        return 'OpenAI usage is unavailable. Please try again later.'
    except requests.RequestException:
        return 'OpenAI usage is unavailable. Please try again later.'
    except (ValueError, KeyError, TypeError, AttributeError, decimal.InvalidOperation):
        return 'OpenAI returned an invalid usage response. Please try again later.'
    return (
        f'OpenAI: ${cost:.2f} USD used in the last 7 days.'
    )
