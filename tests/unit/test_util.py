import urllib.parse

import pytest
import requests

from pmxbot import commands, util


def test_lookup(wordnik_http):
    opened = wordnik_http([{'text': 'A short-legged dog.'}, {'text': 'Ignored.'}])
    assert util.lookup('dachshund') == 'A short-legged dog.'
    request = opened.call_args.args[0]
    assert request.get_method() == 'GET'
    assert (
        request.full_url
        == 'https://api.wordnik.com/v4/word.json/dachshund/definitions?limit=1'
    )
    assert request.get_header('Content-type') == 'application/json'


@pytest.mark.parametrize(
    'body,status',
    [([], 200), (None, 200), ([{}], 200), ([{'text': ''}], 200), ([], 403)],
)
def test_lookup_missing(wordnik_http, body, status):
    wordnik_http(body, status)
    assert util.lookup('unknown') is None


def test_acronym_lookup(provider_http):
    send = provider_http(
        ''.join(
            f'<td class="result-list__body__meaning"><b>Meaning {n}</b></td>'
            for n in range(4)
        )
    )
    assert util.lookup_acronym(' n.s.f.w. ') == ['Meaning 0', 'Meaning 1', 'Meaning 2']
    request = send.call_args.args[0]
    assert request.method == 'GET'
    assert request.url == 'https://www.acronymfinder.com/NSFW.html'
    assert request.headers['Accept'] == 'text/html'
    assert request.headers['User-Agent'].startswith('Mozilla/')
    assert send.call_args.kwargs['timeout'] == 10


def test_acronym_limit(provider_http):
    provider_http(
        '<td class="result-list__body__meaning">One</td><td class="result-list__body__meaning">Two</td>'
    )
    assert util.lookup_acronym('IRC', limit=1) == ['One']


def test_acronym_missing(provider_http):
    provider_http('<html></html>')
    assert util.lookup_acronym('unknown') == []


def test_urban_lookup(provider_http):
    send = provider_http(
        {'list': [{'definition': ' First\n meaning. '}, {'definition': 'Ignored.'}]}
    )
    assert util.urban_lookup('relay chat') == 'First meaning.'
    request = send.call_args.args[0]
    assert request.method == 'GET'
    url = urllib.parse.urlsplit(request.url)
    assert (url.scheme, url.netloc, url.path) == (
        'http',
        'api.urbandictionary.com',
        '/v0/define',
    )
    assert urllib.parse.parse_qs(url.query) == {'term': ['relay chat']}
    assert send.call_args.kwargs['timeout'] == 5


def test_urban_long_definition(provider_http):
    first = 'A complete sentence. ' * 21
    provider_http({'list': [{'definition': first + 'Another sentence. ' * 20}]})
    result = util.urban_lookup('long')
    assert 400 <= len(result) <= 450
    assert result.endswith('.')


@pytest.mark.parametrize(
    'body', [{'list': []}, {}, {'list': [{}]}, {'list': [{'definition': ''}]}]
)
def test_urban_missing(provider_http, body):
    provider_http(body)
    assert util.urban_lookup('unknown') is None


@pytest.mark.parametrize(
    'lookup',
    [util.lookup_acronym, util.urban_lookup, commands.acit, commands.urbandict],
)
def test_http_forbidden(provider_http, lookup):
    provider_http('Forbidden', status=403)
    with pytest.raises(requests.HTTPError) as exc:
        lookup('irc')
    assert exc.value.response.status_code == 403


@pytest.mark.parametrize('style', range(4))
def test_get_insult(provider_http, monkeypatch, style):
    send = provider_http('<div class="insult" id="insult">Your silly hat.</div>')
    monkeypatch.setattr(commands.random, 'randrange', lambda limit: style)
    result = commands.get_insult()
    assert result == 'Your silly hat.'
    assert result.type == style
    assert send.call_args.args[0].url == f'http://autoinsult.com/?style={style}'
    assert send.call_args.args[0].method == 'GET'


def test_insult_connection_error(provider_http):
    send = provider_http('')
    send.side_effect = requests.ConnectionError('offline')
    assert commands.get_insult() is None
