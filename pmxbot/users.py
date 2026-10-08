"""Canonical application users, independent of IRC and web authentication."""

import re
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
        parsed = urlparse(uri)
        if parsed.scheme not in ('', 'sqlite') or not parsed.path:
            raise UserError('User storage requires a SQLite database URI.')
        super().__init__(uri)

    def init_tables(self):
        try:
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
        except Exception:
            self.close()
            raise

    @staticmethod
    def _user(row):
        if row is None:
            raise UserNotFound('Unknown user.')
        return User(*row[:5], bool(row[5]), bool(row[6]))

    def get_by_id(self, user_id):
        return self._user(
            self.db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        )

    def get_by_username(self, username):
        return self._user(
            self.db.execute(
                'SELECT * FROM users WHERE normalized_username = ?',
                (normalize_username(username),),
            ).fetchone()
        )

    def list_users(self):
        """Return users in stable ID order, including disabled accounts."""
        return [
            self._user(row)
            for row in self.db.execute('SELECT * FROM users ORDER BY id')
        ]

    @staticmethod
    def _validate_fields(display_name, enabled, can_pair_irc):
        if display_name is not None and not isinstance(display_name, str):
            raise UserError('Display name must be text.')
        if type(enabled) is not bool or type(can_pair_irc) is not bool:
            raise UserError('Enabled and IRC pairing permission must be booleans.')

    def create(self, username, *, display_name=None, enabled=True, can_pair_irc=False):
        normalized = normalize_username(username)
        self._validate_fields(display_name, enabled, can_pair_irc)
        try:
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
        except sqlite3.IntegrityError as exc:
            # The INSERT's unique constraint arbitrates concurrent creates.
            if 'UNIQUE constraint failed: users.normalized_username' not in str(exc):
                raise
            raise UsernameTaken('Username already exists.') from exc
        return self.get_by_id(cursor.lastrowid)

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
