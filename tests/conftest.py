import os

import pytest

import pmxbot.util


@pytest.fixture
def needs_wordnik(needs_internet):
    if 'wordnik' not in dir(pmxbot.util):
        pytest.skip('Wordnik not available')


@pytest.fixture
def google_api_key(monkeypatch):
    key = os.environ.get('GOOGLE_API_KEY')
    if not key:
        pytest.skip("Need GOOGLE_API_KEY environment variable")
    monkeypatch.setitem(pmxbot.config, 'Google API key', key)


def pytest_addoption(parser):
    parser.addoption('--block-http', action='store_true', help='Block external HTTP')


@pytest.fixture(autouse=True)
def block_http(request, monkeypatch):
    if not request.config.getoption('--block-http'):
        return

    def blocked(*args, **kwargs):
        pytest.fail('Unexpected external HTTP/network access')

    import socket
    import urllib.request

    import requests

    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'send', blocked)
    monkeypatch.setattr(urllib.request, 'urlopen', blocked)


@pytest.fixture
def provider_http(monkeypatch, block_http):
    """Fixed responses at Requests' transport boundary; parsing stays real."""
    import json
    from unittest.mock import Mock

    import requests

    def install(body, status=200):
        response = requests.Response()
        response.status_code = status
        response._content = (
            json.dumps(body) if not isinstance(body, str) else body
        ).encode()
        response.encoding = 'utf-8'

        def reply(request, **kwargs):
            response.request = request
            response.url = request.url
            return response

        send = Mock(side_effect=reply)
        monkeypatch.setattr(requests.sessions.Session, 'send', send)
        return send

    return install


@pytest.fixture
def wordnik_http(monkeypatch, block_http):
    """Keep the Wordnik SDK's URL construction and deserialization real."""
    import io
    import json
    import urllib.error
    import urllib.request
    from email.message import Message
    from unittest.mock import Mock

    def install(body, status=200):
        response = io.BytesIO(json.dumps(body).encode())
        response.headers = Message()
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        open_url = Mock(return_value=response)
        if status != 200:
            open_url.side_effect = urllib.error.HTTPError(
                'https://api.wordnik.com', status, 'Forbidden', {}, io.BytesIO()
            )
            open_url.side_effect.close()
        monkeypatch.setattr(urllib.request, 'urlopen', open_url)
        return open_url

    return install


@pytest.fixture
def autoinsult_http(provider_http):
    return provider_http('<div class="insult" id="insult">Your silly hat.</div>')
