"""Organization spending reported by OpenAI's Costs API."""

import datetime
import decimal
import os

import requests

import pmxbot

from .core import command


def today_cost(key, now=None):
    """Sum reported costs for the seven days ending at now, in UTC."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    now = now.astimezone(datetime.timezone.utc)
    end_time = int(now.timestamp())
    start_time = int((now - datetime.timedelta(days=7)).timestamp())
    params = {
        'start_time': start_time,
        'end_time': end_time,
        'bucket_width': '1d',
        'limit': 1,
    }
    total = decimal.Decimal(0)
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
    "Report OpenAI organization spending for the last seven days (UTC)."
    key = pmxbot.config.get('openai_admin_key') or os.environ.get('OPENAI_ADMIN_KEY')
    if not key:
        return (
            'Configure openai_admin_key or OPENAI_ADMIN_KEY with an OpenAI admin key.'
        )
    try:
        cost = today_cost(key)
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
    return f'OpenAI: ${cost:.2f} USD used in the last 7 days.'
