import datetime
import io
from contextlib import closing
from http.cookies import SimpleCookie
from urllib.parse import urlencode

import cherrypy
import pytest
from bs4 import BeautifulSoup
from cherrypy.lib.sessions import RamSession

import pmxbot
from pmxbot.music import MusicLibrary
from pmxbot.users import UserStore
from pmxbot.web import auth, viewer


@pytest.fixture(params=['', '/bot'])
def web(request, tmp_path, monkeypatch):
    monkeypatch.setattr(pmxbot, 'config', {})
    config = viewer.init_config(
        {'database': f'sqlite:{tmp_path / "bot.sqlite"}', 'web_base': request.param}
    )
    monkeypatch.setattr(pmxbot, 'config', config)
    monkeypatch.setattr(auth, 'login_throttle', auth.LoginThrottle())
    with closing(UserStore(config.database)) as store:
        user = store.create('Alice', password='correct password', can_pair_irc=True)
        store.create('disabled', password='correct password', enabled=False)
        store.create('no-password')
    album = MusicLibrary(tmp_path / 'bot.sqlite').create_album('Band', 'Album')

    class Pages(viewer.PmxbotPages):
        @cherrypy.expose
        def protected(self):
            user = auth.require_authenticated_user()
            return f'{user.id}:{user.username}:{user.may_pair_irc}'

        @cherrypy.expose
        def mutate(self, csrf_token=''):
            auth.require_authenticated_user()
            auth.require_csrf(csrf_token)
            return 'changed'

    app = cherrypy.Application(
        Pages(), config.web_base, {'/': auth.session_config(config)}
    )

    class Client:
        cookie = ''
        base = config.web_base
        user_id = user.id
        album_id = album['id']

        def set_secure(self, value):
            app.config['/']['tools.sessions.secure'] = value

        def request(self, path='/', method='GET', data=None, cookie=None):
            path, _, query = path.partition('?')
            body = urlencode(data or {}).encode()
            env = {
                'REQUEST_METHOD': method,
                'SCRIPT_NAME': self.base,
                'PATH_INFO': path,
                'QUERY_STRING': query,
                'SERVER_NAME': 'localhost',
                'HTTP_HOST': 'localhost',
                'SERVER_PORT': '80',
                'SERVER_PROTOCOL': 'HTTP/1.1',
                'CONTENT_TYPE': 'application/x-www-form-urlencoded',
                'CONTENT_LENGTH': str(len(body)),
                'HTTP_COOKIE': self.cookie if cookie is None else cookie,
                'wsgi.version': (1, 0),
                'wsgi.url_scheme': 'http',
                'wsgi.input': io.BytesIO(body),
                'wsgi.errors': io.StringIO(),
                'wsgi.multithread': False,
                'wsgi.multiprocess': False,
                'wsgi.run_once': False,
            }
            response = {}

            def start(status, headers, exc_info=None):
                response.update(status=int(status.split()[0]), headers=dict(headers))

            output = app(env, start)
            try:
                response['body'] = b''.join(output).decode()
            finally:
                output.close()
            cookies = SimpleCookie(response['headers'].get('Set-Cookie', ''))
            if auth.COOKIE_NAME in cookies:
                self.cookie = auth.COOKIE_NAME + '=' + cookies[auth.COOKIE_NAME].value
            return response

        def token(self, path='/login'):
            soup = BeautifulSoup(self.request(path)['body'], 'html.parser')
            return soup.select_one('input[name=csrf_token]')['value']

        def login(self, **kwargs):
            data = {
                'username': ' alice ',
                'password': 'correct password',
                'csrf_token': self.token(),
            }
            data.update(kwargs)
            return self.request('/login', 'POST', data)

    yield Client()


def session_id(cookie):
    return SimpleCookie(cookie)[auth.COOKIE_NAME].value


def test_login_rotation_identity_navigation_and_logout(web):
    token = web.token()
    anonymous_cookie = web.cookie
    response = web.request(
        '/login',
        'POST',
        {
            'username': ' ALICE ',
            'password': 'correct password',
            'csrf_token': token,
            'return_to': web.base + '/gallery?sort=band',
        },
    )
    assert response['status'] == 303
    assert response['headers']['Location'] == web.base + '/gallery?sort=band'
    assert web.cookie != anonymous_cookie
    signed_cookie = web.cookie
    assert RamSession.cache[session_id(signed_cookie)][0] == {'user_id': web.user_id}
    assert session_id(anonymous_cookie) not in RamSession.cache
    for path in ('/', '/gallery', f'/albums/{web.album_id}', '/login'):
        response = web.request(path)
        assert response['status'] == 200
        assert 'Hi, Alice.' in response['body']
        soup = BeautifulSoup(response['body'], 'html.parser')
        assert (
            soup.select_one('form[action$="/logout"]')['action'] == web.base + '/logout'
        )
    assert web.request('/protected')['body'] == f'{web.user_id}:Alice:True'
    assert web.request('/protected', cookie=anonymous_cookie)['status'] == 401
    web.cookie = signed_cookie
    token = web.token('/')
    assert web.request('/mutate', 'POST', {'csrf_token': token})['status'] == 200
    response = web.request('/logout', 'POST', {'csrf_token': token})
    assert response['status'] == 303
    assert 'expires=' in response['headers']['Set-Cookie'].lower()
    assert session_id(signed_cookie) not in RamSession.cache
    assert web.request('/protected', cookie=signed_cookie)['status'] == 401
    assert 'Hi, Alice.' not in web.request('/')['body']


@pytest.mark.parametrize(
    'credentials',
    [
        {'username': 'missing'},
        {'password': 'wrong'},
        {'username': 'disabled'},
        {'username': 'no-password'},
        {'username': '<script>'},
    ],
)
def test_generic_failures(web, credentials):
    response = web.login(**credentials)
    assert response['status'] == 401
    assert 'Invalid username or password.' in response['body']
    assert 'correct password' not in response['body']
    assert RamSession.cache[session_id(web.cookie)][0] == {}
    assert web.request('/protected')['status'] == 401


@pytest.mark.parametrize('deleted', [False, True])
def test_revocation_is_persistent_even_if_reenabled(web, deleted):
    assert web.login()['status'] == 303
    cookie = web.cookie
    with closing(UserStore(pmxbot.config.database)) as store:
        if deleted:
            store.db.execute('DELETE FROM users WHERE id = ?', (web.user_id,))
        else:
            store.update(web.user_id, enabled=False)
    assert web.request('/protected')['status'] == 401
    assert session_id(cookie) not in RamSession.cache
    if not deleted:
        with closing(UserStore(pmxbot.config.database)) as store:
            store.update(web.user_id, enabled=True)
    assert web.request('/protected', cookie=cookie)['status'] == 401


def test_csrf_and_method_guards(web):
    token = web.token()
    for bad in ('', 'bad', 'é', [token, token]):
        assert (
            web.request(
                '/login',
                'POST',
                {
                    'username': 'Alice',
                    'password': 'correct password',
                    'csrf_token': bad,
                },
            )['status']
            == 403
        )
    assert web.login()['status'] == 303
    assert web.request('/logout')['status'] == 405
    assert web.request('/login', 'PUT')['status'] == 405
    assert web.request('/mutate', 'POST')['status'] == 403
    assert web.request('/logout', 'POST', {'csrf_token': token})['status'] == 403
    assert web.request('/logout', 'POST')['status'] == 403
    assert web.request('/protected')['status'] == 200
    cookie = web.cookie
    other_token = web.token('/login')
    assert (
        web.request('/logout', 'POST', {'csrf_token': other_token}, cookie='')['status']
        == 403
    )
    web.cookie = cookie
    assert web.request('/protected')['status'] == 200


@pytest.mark.parametrize(
    'target',
    [
        'https://evil.test',
        '//evil.test',
        '/\\evil.test',
        '/%2fevil.test',
        '/bot/../outside',
        '/bot/%2e%2e/outside',
        '/bot/%252e%252e/outside',
        '/bot/\r\nLocation:evil',
        'gallery',
        '/bot/🔐',
        '/bot/%7f',
        '/bot-other/gallery',
    ],
)
def test_redirect_safety(web, target):
    # /bot-other is a valid path for root deployments only.
    expected = (
        target if not web.base and target == '/bot-other/gallery' else web.base + '/'
    )
    response = web.login(return_to=target)
    assert response['headers']['Location'] == expected


def test_cookie_defaults_anonymous_browsing_and_expiry(web):
    for path in ('/', '/gallery', f'/albums/{web.album_id}'):
        response = web.request(path)
        assert response['status'] == 200
        soup = BeautifulSoup(response['body'], 'html.parser')
        assert not soup.select('nav[aria-label=Account]')
        if path == '/':
            form = soup.select_one('.signin-card form')
            assert form['action'] == web.base + '/login'
            assert form['method'] == 'post'
            assert form.select_one('input[name=return_to]')['value'] == web.base + '/'
            assert form.select_one('input[name=csrf_token]')['value']
        else:
            assert not soup.select('.signin-card')
        assert response['headers']['Cache-Control'] == 'no-store'
    cookie = SimpleCookie(response['headers']['Set-Cookie'])[auth.COOKIE_NAME]
    assert cookie['secure'] and cookie['httponly']
    assert cookie['samesite'] == 'Lax'
    assert cookie['path'] == (web.base or '/')
    assert not cookie['domain'] and not cookie['expires']
    assert web.login()['status'] == 303
    key = session_id(web.cookie)
    data, expires = RamSession.cache[key]
    RamSession.cache[key] = (
        data,
        expires - datetime.timedelta(days=1),
    )
    assert web.request('/protected')['status'] == 401


def test_throttle_is_bounded_and_recovers(web, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(auth.time, 'monotonic', lambda: now[0])
    # Exercise real POST guard without doing 30 expensive password derivations.
    auth.login_throttle.attempts.extend([0.0] * 30)
    response = web.login()
    assert response['status'] == 429
    assert response['headers']['Retry-After'] == '60'
    assert len(auth.login_throttle.attempts) == 30
    assert web.request('/')['status'] == 200
    now[0] = 60.0
    assert web.login()['status'] == 303
    assert len(auth.login_throttle.attempts) == 1


@pytest.mark.parametrize('path', ['/login', '/register'])
def test_auth_query_is_rejected_and_redacted(web, monkeypatch, path):
    logged = []
    monkeypatch.setattr(
        cherrypy._cplogging.LogManager,
        'access',
        lambda self: logged.append(cherrypy.request.request_line),
    )
    response = web.request(
        path + '?username=Alice&password=secret',
        'POST',
        {'csrf_token': web.token(path)},
    )
    assert response['status'] == 400
    assert all('secret' not in line and 'username=' not in line for line in logged)
    assert web.request('/protected')['status'] == 401


def test_session_configuration(monkeypatch):
    config = pmxbot.core.ConfigDict(web_base='/bot', web_session_secure=False)
    assert auth.session_config(config)['tools.sessions.secure'] is False
    config['web_session_secure'] = 'false'
    with pytest.raises(ValueError, match='boolean'):
        auth.session_config(config)


def test_explicit_development_http_cookie(web):
    web.set_secure(False)
    response = web.request('/login')
    cookie = SimpleCookie(response['headers']['Set-Cookie'])[auth.COOKIE_NAME]
    assert not cookie['secure']
    assert cookie['httponly'] and cookie['samesite'] == 'Lax'


def test_startup_enables_sessions_at_mount(monkeypatch):
    called = []
    monkeypatch.setattr(viewer, '_setup_logging', lambda: None)
    monkeypatch.setattr(pmxbot.core, '_load_library_extensions', lambda: None)
    monkeypatch.setattr(pmxbot, 'config', {})
    monkeypatch.setattr(
        cherrypy,
        'quickstart',
        lambda pages, base, config: called.append((pages, base, config)),
    )
    viewer.startup({'web_base': '/bot/'})
    pages, base, config = called[0]
    assert isinstance(pages, viewer.PmxbotPages)
    assert base == '/bot'
    assert config['/'] == auth.session_config(pmxbot.config)
    assert config['/']['tools.sessions.secure'] is True
    assert config['/']['tools.sessions.timeout'] == 30


def registration(web, **overrides):
    data = {
        'username': 'NewUser',
        'password': 'ValidPassword123',
        'password_confirmation': 'ValidPassword123',
        'csrf_token': web.token('/register'),
    }
    data.update(overrides)
    return web.request('/register', 'POST', data)


def test_registration_creates_disabled_salted_credentials(web):
    response = registration(web)
    assert response['status'] == 303
    assert response['headers']['Location'] == web.base + '/register?created=1'
    assert (
        'awaiting administrator approval' in web.request('/register?created=1')['body']
    )
    with closing(UserStore(pmxbot.config.database)) as store:
        user = store.get_by_username('newuser')
        assert user.username == 'NewUser' and not user.enabled and not user.can_pair_irc
        assert store.db.execute(
            'SELECT enabled FROM users WHERE id = ?', (user.id,)
        ).fetchone() == (0,)
        encoded = store.db.execute(
            'SELECT password_hash FROM user_passwords WHERE user_id = ?', (user.id,)
        ).fetchone()[0]
        assert encoded.startswith('pbkdf2_sha256$600000$')
        assert 'ValidPassword123' not in encoded
        from pmxbot.users import InvalidCredentials

        with pytest.raises(InvalidCredentials):
            store.authenticate('newuser', 'ValidPassword123')
        store.update(user.id, enabled=True)
        assert store.authenticate('newuser', 'ValidPassword123').id == user.id
    assert web.request('/protected')['status'] == 401


@pytest.mark.parametrize(
    'password',
    [
        'Short1',
        'alllowercase123',
        'ALLUPPERCASE123',
        'NoNumberPassword',
        'Aa1' + 'x' * 1022,
    ],
)
def test_registration_rejects_weak_passwords_without_browser_validation(web, password):
    response = registration(web, password=password, password_confirmation=password)
    assert response['status'] == 400
    assert 'Password must contain' in response['body']
    with closing(UserStore(pmxbot.config.database)) as store:
        assert len(store.list_users()) == 3


def test_registration_rejects_mismatch_and_duplicate_name(web):
    assert (
        registration(web, password_confirmation='AnotherPassword123')['status'] == 400
    )
    with closing(UserStore(pmxbot.config.database)) as store:
        original = store.get_by_username('alice')
        original_hash = store.db.execute(
            'SELECT password_hash FROM user_passwords WHERE user_id = ?', (original.id,)
        ).fetchone()
    response = registration(web, username=' aLiCe ')
    assert response['status'] == 400
    assert 'already registered' in response['body']
    with closing(UserStore(pmxbot.config.database)) as store:
        assert store.get_by_id(original.id) == original
        assert (
            store.db.execute(
                'SELECT password_hash FROM user_passwords WHERE user_id = ?',
                (original.id,),
            ).fetchone()
            == original_hash
        )
        assert store.authenticate('Alice', 'correct password') == original
        assert len(store.list_users()) == 3


def test_registration_csrf_methods_and_username_validation(web):
    assert registration(web, csrf_token='')['status'] == 403
    assert web.request('/register', 'PUT')['status'] == 405
    assert (
        web.request(
            '/register?password=secret', 'POST', {'csrf_token': web.token('/register')}
        )['status']
        == 400
    )
    assert registration(web, username='<script>')['status'] == 400
    with closing(UserStore(pmxbot.config.database)) as store:
        assert len(store.list_users()) == 3
    for path in ('/', '/login'):
        soup = BeautifulSoup(web.request(path)['body'], 'html.parser')
        assert soup.select_one('a[href$="/register"]')['href'] == web.base + '/register'


@pytest.mark.parametrize('length', [12, 1024])
def test_registration_password_length_boundaries(length):
    password = 'Aa1' + 'x' * (length - 3)
    auth.validate_registration_password(password, password)


def test_registration_respects_shared_throttle(web, monkeypatch):
    monkeypatch.setattr(auth.time, 'monotonic', lambda: 0.0)
    auth.login_throttle.attempts.extend([0.0] * 30)
    response = registration(web)
    assert response['status'] == 429
    assert response['headers']['Retry-After'] == '60'
    with closing(UserStore(pmxbot.config.database)) as store:
        assert len(store.list_users()) == 3
