"""Small, process-local web authentication built on CherryPy sessions."""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from urllib.parse import unquote, urlsplit

import cherrypy
from cherrypy.lib import sessions

import pmxbot
from pmxbot.users import UserError, UserNotFound, UserStore

COOKIE_NAME = 'pmxbot_session'
_CSRF_SECRET = secrets.token_bytes(32)


class LoginThrottle:
    """Bound password derivations across all clients, without trusting proxy IPs."""

    def __init__(self):
        self.attempts = deque()
        self.lock = threading.Lock()

    def check(self):
        now = time.monotonic()
        with self.lock:
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 30:
                raise cherrypy.HTTPError(429, 'Please try again later.')
            self.attempts.append(now)


login_throttle = LoginThrottle()


def session_config(config):
    secure = config.get('web_session_secure', True)
    if type(secure) is not bool:
        raise ValueError('web_session_secure must be a boolean')
    return {
        'tools.sessions.on': True,
        'tools.sessions.name': COOKIE_NAME,
        'tools.sessions.storage_class': sessions.RamSession,
        'tools.sessions.timeout': 30,
        'tools.sessions.httponly': True,
        'tools.sessions.secure': secure,
        'tools.sessions.path': config.web_base or '/',
        'tools.sessions.persistent': False,
        'tools.sessions.debug': False,
        'tools.web_session_headers.on': True,
        'tools.redact_auth_query.on': True,
    }


def session_headers():
    cherrypy.response.cookie[COOKIE_NAME]['samesite'] = 'Lax'
    cherrypy.response.headers['Cache-Control'] = 'no-store'
    cherrypy.response.headers['Referrer-Policy'] = 'same-origin'
    if str(cherrypy.response.status).startswith('429'):
        cherrypy.response.headers['Retry-After'] = '60'


def redact_auth_query():
    # Never put query credentials in CherryPy's access log, even on a bad GET.
    request = cherrypy.request
    if request.path_info.rstrip('/').endswith(('/login', '/logout')):
        request.request_line = '{} {} {}'.format(
            request.method,
            request.script_name + request.path_info,
            'HTTP/{}.{}'.format(*request.protocol),
        )


cherrypy.tools.web_session_headers = cherrypy.Tool(
    'before_finalize', session_headers, priority=60
)

cherrypy.tools.redact_auth_query = cherrypy.Tool('on_start_resource', redact_auth_query)


def invalidate_session():
    cherrypy.session.clear()
    cherrypy.session.regenerate()
    sessions.expire()


def current_user():
    """Re-read canonical identity every time; missing/disabled accounts revoke it."""
    if not (cherrypy.request.config or {}).get('tools.sessions.on'):
        return None
    user_id = cherrypy.session.get('user_id')
    if user_id is None:
        return None
    try:
        with closing(UserStore(pmxbot.config.database)) as store:
            user = store.get_by_id(user_id)
    except UserNotFound:
        user = None
    except (sqlite3.Error, UserError):
        raise cherrypy.HTTPError(503, 'User storage is unavailable') from None
    if user is None or not user.enabled:
        invalidate_session()
        return None
    return user


def require_authenticated_user():
    """Future protected handlers call this and receive a User or an explicit 401."""
    user = current_user()
    if user is None:
        raise cherrypy.HTTPError(401, 'Authentication required.')
    return user


def csrf_token():
    """Bind a form token to this session without storing another session field."""
    if not (cherrypy.request.config or {}).get('tools.sessions.on'):
        return ''
    # Load even an anonymous session so CherryPy persists its identifier.
    cherrypy.session.get('user_id')
    return hmac.new(
        _CSRF_SECRET, cherrypy.session.id.encode('ascii'), hashlib.sha256
    ).hexdigest()


def require_csrf(token):
    """Reusable guard for POST form mutations, including anonymous login."""
    cherrypy.lib.cptools.allow(['POST'])
    expected = csrf_token()
    if (
        not expected
        or not isinstance(token, str)
        or not token.isascii()
        or not hmac.compare_digest(token, expected)
    ):
        raise cherrypy.HTTPError(403, 'Invalid form token.')


def safe_return_to(value):
    """Accept only absolute-path references inside this mounted application."""
    fallback = (pmxbot.config.web_base or '') + '/'
    if not isinstance(value, str) or not value.isascii() or not value.startswith('/'):
        return fallback
    # Reject encoding ambiguity, dot traversal, controls, and browser backslashes.
    decoded = unquote(value)
    if '%' in decoded or any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in decoded
    ):
        return fallback
    if '\\' in decoded or decoded.startswith('//'):
        return fallback
    try:
        parsed = urlsplit(decoded)
    except ValueError:
        return fallback
    base = pmxbot.config.web_base
    if (
        parsed.scheme
        or parsed.netloc
        or any(part in ('.', '..') for part in parsed.path.split('/'))
        or (base and not parsed.path.startswith(base + '/'))
    ):
        return fallback
    return value


def redirect(path):
    """Keep Location relative so TLS proxies need no trusted host/scheme headers."""
    response = cherrypy.HTTPRedirect(path, 303)
    response.urls = [path]
    raise response
