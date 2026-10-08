"""Canonical application users, independent of IRC and web authentication."""

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

from .storage import SQLiteStorage


class UserError(ValueError):
    """A domain error in a user operation."""


class InvalidUsername(UserError):
    """The username does not meet the application naming policy."""


class UsernameTaken(UserError):
    """The normalized username already belongs to a user."""


class UserNotFound(UserError):
    """No user has the requested ID or username."""


class InvalidCredentials(UserError):
    """Authentication failed without disclosing account existence or state."""


_PASSWORD_ITERATIONS = 600_000
_USER_COLUMNS = (
    'id, username, normalized_username, display_name, created_at, enabled, can_pair_irc'
)


def _password_bytes(password):
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise UserError('Password must contain 1–1024 characters.')
    try:
        return password.encode('utf-8')
    except UnicodeEncodeError:
        raise UserError('Password must be valid Unicode text.') from None


def _hash_password(password):
    value = _password_bytes(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', value, salt, _PASSWORD_ITERATIONS)
    return f'pbkdf2_sha256${_PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}'


def _verify_password(password, encoded):
    try:
        value = _password_bytes(password)
    except UserError:
        return False
    # Perform the same expensive derivation for missing or malformed credentials.
    salt, expected, valid = bytes(16), bytes(32), False
    if isinstance(encoded, str):
        try:
            algorithm, iterations, salt_hex, digest_hex = encoded.split('$')
            if (
                algorithm == 'pbkdf2_sha256'
                and iterations == str(_PASSWORD_ITERATIONS)
                and len(salt_hex) == 32
                and len(digest_hex) == 64
            ):
                salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
                valid = len(salt) == 16 and len(expected) == 32
        except ValueError:
            pass
    actual = hashlib.pbkdf2_hmac('sha256', value, salt, _PASSWORD_ITERATIONS)
    return hmac.compare_digest(actual, expected) and valid


def normalize_username(username):
    """Trim surrounding whitespace and casefold a validated ASCII username."""
    if not isinstance(username, str) or not re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', username.strip()
    ):
        raise InvalidUsername(
            'Username must be 1–64 ASCII letters, digits, dots, underscores, or '
            'hyphens, starting with a letter or digit.'
        )
    return username.strip().casefold()


@dataclass(frozen=True)
class User:
    id: int
    username: str
    normalized_username: str
    display_name: str
    created_at: str
    enabled: bool
    can_pair_irc: bool

    @property
    def may_pair_irc(self):
        """Pairing requires an enabled account and an explicit grant."""
        return self.enabled and self.can_pair_irc


class UserStore(SQLiteStorage):
    """Open the main SQLite database using the existing storage lifecycle.

    Construct with the configured database URI; call close() when finished.
    Opening the store adds only the users table, including on older databases.
    """

    def __init__(self, uri):
        # Prefix bare filenames before parsing, including Windows drive letters.
        if self.uri_matches(uri) and not uri.startswith('sqlite:'):
            uri = 'sqlite:' + uri
        parsed = urlparse(uri)
        if parsed.scheme not in ('', 'sqlite') or not parsed.path:
            raise UserError('User storage requires a SQLite database URI.')
        super().__init__(uri)

    def init_tables(self):
        try:
            self.db.execute('PRAGMA foreign_keys = ON')
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute(
                '''CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL CHECK (
                    length(username) BETWEEN 1 AND 64
                    AND instr(username, char(0)) = 0
                    AND username NOT GLOB '*[^A-Za-z0-9_.-]*'
                    AND substr(username, 1, 1) GLOB '[A-Za-z0-9]'
                ),
                normalized_username TEXT NOT NULL UNIQUE
                    CHECK (normalized_username = lower(username)),
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                can_pair_irc INTEGER NOT NULL DEFAULT 0
                    CHECK (can_pair_irc IN (0, 1))
            )'''
            )
            self.db.execute(
                '''CREATE TABLE IF NOT EXISTS user_passwords (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                password_hash TEXT NOT NULL
            )'''
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.close()
            raise

    @staticmethod
    def _user(row):
        if row is None:
            raise UserNotFound('Unknown user.')
        return User(*row[:5], bool(row[5]), bool(row[6]))

    def get_by_id(self, user_id):
        return self._user(
            self.db.execute(
                f'SELECT {_USER_COLUMNS} FROM users WHERE id = ?', (user_id,)
            ).fetchone()
        )

    def get_by_username(self, username):
        return self._user(
            self.db.execute(
                f'SELECT {_USER_COLUMNS} FROM users WHERE normalized_username = ?',
                (normalize_username(username),),
            ).fetchone()
        )

    def list_users(self):
        """Return users in stable ID order, including disabled accounts."""
        return [
            self._user(row)
            for row in self.db.execute(f'SELECT {_USER_COLUMNS} FROM users ORDER BY id')
        ]

    @staticmethod
    def _validate_fields(display_name, enabled, can_pair_irc):
        if display_name is not None and not isinstance(display_name, str):
            raise UserError('Display name must be text.')
        if type(enabled) is not bool or type(can_pair_irc) is not bool:
            raise UserError('Enabled and IRC pairing permission must be booleans.')

    def create(
        self,
        username,
        *,
        display_name=None,
        enabled=True,
        can_pair_irc=False,
        password=None,
    ):
        normalized = normalize_username(username)
        self._validate_fields(display_name, enabled, can_pair_irc)
        password_hash = _hash_password(password) if password is not None else None
        try:
            self.db.execute('BEGIN IMMEDIATE')
            cursor = self.db.execute(
                '''INSERT INTO users
                (username, normalized_username, display_name, enabled, can_pair_irc)
                VALUES (?, ?, ?, ?, ?)''',
                (
                    username.strip(),
                    normalized,
                    username.strip() if display_name is None else display_name,
                    enabled,
                    can_pair_irc,
                ),
            )
            if password_hash is not None:
                self.db.execute(
                    'INSERT INTO user_passwords VALUES (?, ?)',
                    (cursor.lastrowid, password_hash),
                )
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            # The INSERT's unique constraint arbitrates concurrent creates.
            if 'UNIQUE constraint failed: users.normalized_username' not in str(exc):
                raise
            raise UsernameTaken('Username already exists.') from exc
        except Exception:
            self.db.rollback()
            raise
        return self.get_by_id(cursor.lastrowid)

    def set_password(self, user_id, password):
        """Set or replace a salted hash; the plaintext is never persisted."""
        password_hash = _hash_password(password)
        self.get_by_id(user_id)
        self.db.execute(
            '''INSERT INTO user_passwords (user_id, password_hash) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET password_hash = excluded.password_hash''',
            (user_id, password_hash),
        )

    def authenticate(self, username, password):
        """Verify credentials and enabled state; return only public user fields."""
        try:
            user = self.get_by_username(username)
        except (InvalidUsername, UserNotFound):
            user = None
        row = (
            self.db.execute(
                'SELECT password_hash FROM user_passwords WHERE user_id = ?', (user.id,)
            ).fetchone()
            if user is not None
            else None
        )
        verified = _verify_password(password, row[0] if row else None)
        if not verified or user is None or not user.enabled:
            raise InvalidCredentials('Invalid username or password.')
        return user

    def update(self, user_id, *, display_name=None, enabled=None, can_pair_irc=None):
        """Update supplied fields without changing identity or creation time."""
        self._validate_fields(
            display_name,
            True if enabled is None else enabled,
            False if can_pair_irc is None else can_pair_irc,
        )
        cursor = self.db.execute(
            '''UPDATE users SET display_name = COALESCE(?, display_name),
            enabled = COALESCE(?, enabled), can_pair_irc = COALESCE(?, can_pair_irc)
            WHERE id = ?''',
            (display_name, enabled, can_pair_irc, user_id),
        )
        if not cursor.rowcount:
            raise UserNotFound('Unknown user.')
        return self.get_by_id(user_id)
