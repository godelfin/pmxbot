"""Canonical application users, independent of IRC and web authentication."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
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
    'id, username, normalized_username, display_name, created_at, enabled, can_pair_irc, '
    'role, can_generate_images, must_change_password, session_version, nsfw, can_manage_prompt_rules'
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

    role: str = 'user'
    can_generate_images: bool = False
    must_change_password: bool = False
    session_version: int = field(default=0, compare=False, repr=False)

    nsfw: bool = False
    can_manage_prompt_rules: bool = False

    def has_permission(self, permission):
        """Explicit capabilities remain independent of administrative roles."""
        return (
            self.enabled
            and not self.must_change_password
            and {
                'generate_images': self.can_generate_images,
                'pair_irc': self.can_pair_irc,
                'nsfw': self.nsfw,
                'manage_prompt_rules': self.can_manage_prompt_rules,
            }.get(permission, False)
        )

    @property
    def is_admin(self):
        return self.enabled and self.role == 'admin' and not self.must_change_password

    @property
    def may_pair_irc(self):
        """Pairing requires an enabled account and an explicit grant."""
        return self.has_permission('pair_irc')


class UserStore(SQLiteStorage):
    """Open the main SQLite database using the existing storage lifecycle.

    Construct with the configured database URI; call close() when finished.
    Opening the store additively migrates user, credential and audit tables.
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
            # Additive migrations preserve identity, credentials and existing grants.
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(users)')}
            for name, definition in (
                (
                    'role',
                    "TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin'))",
                ),
                (
                    'can_generate_images',
                    'INTEGER NOT NULL DEFAULT 0 CHECK (can_generate_images IN (0, 1))',
                ),
                (
                    'must_change_password',
                    'INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1))',
                ),
                ('session_version', 'INTEGER NOT NULL DEFAULT 0'),
                (
                    'can_manage_prompt_rules',
                    'INTEGER NOT NULL DEFAULT 0 CHECK (can_manage_prompt_rules IN (0, 1))',
                ),
                ('nsfw', 'INTEGER NOT NULL DEFAULT 0 CHECK (nsfw IN (0, 1))'),
            ):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE users ADD COLUMN {name} {definition}')
                    if name == 'can_generate_images':
                        # Preserve access only for accounts predating this migration.
                        self.db.execute('UPDATE users SET can_generate_images = 1')
            self.db.execute(
                '''CREATE TABLE IF NOT EXISTS user_admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER REFERENCES users(id),
                target_id INTEGER NOT NULL REFERENCES users(id),
                action TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                changes TEXT NOT NULL
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
        return User(
            *row[:5],
            bool(row[5]),
            bool(row[6]),
            row[7],
            bool(row[8]),
            bool(row[9]),
            row[10],
            bool(row[11]),
            bool(row[12]),
        )

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
                (username, normalized_username, display_name, enabled, can_pair_irc,
                 can_generate_images)
                VALUES (?, ?, ?, ?, ?, 0)''',
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
        with self._transaction():
            self.get_by_id(user_id)
            self._write_password(user_id, password_hash, False)

    def _write_password(self, user_id, password_hash, temporary):
        self.db.execute(
            '''INSERT INTO user_passwords (user_id, password_hash) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET password_hash = excluded.password_hash''',
            (user_id, password_hash),
        )
        self.db.execute(
            'UPDATE users SET must_change_password = ?, session_version = session_version + 1 WHERE id = ?',
            (temporary, user_id),
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

    @contextmanager
    def _transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _audit(self, actor_id, target_id, action, changes):
        self.db.execute(
            'INSERT INTO user_admin_audit (actor_id, target_id, action, changes) VALUES (?, ?, ?, ?)',
            (actor_id, target_id, action, json.dumps(changes, sort_keys=True)),
        )

    def _require_admin(self, actor_id, version):
        actor = self.get_by_id(actor_id)
        if not actor.is_admin or actor.session_version != version:
            raise UserError('Administrator access is required. Sign in again.')

    def bootstrap_admin(self, username):
        """Explicit local-only bootstrap; cannot replace an existing active admin."""
        with self._transaction():
            if self.db.execute(
                "SELECT 1 FROM users WHERE role = 'admin' AND enabled = 1"
            ).fetchone():
                raise UserError('An active administrator already exists.')
            user = self.get_by_username(username)
            if (
                not user.enabled
                or user.must_change_password
                or not self.db.execute(
                    'SELECT 1 FROM user_passwords WHERE user_id = ?', (user.id,)
                ).fetchone()
            ):
                raise UserError(
                    'Bootstrap requires an enabled account with a password.'
                )
            self.db.execute(
                "UPDATE users SET role = 'admin', session_version = session_version + 1 WHERE id = ?",
                (user.id,),
            )
            self._audit(None, user.id, 'bootstrap_admin', {'role': 'admin'})
        return self.get_by_id(user.id)

    def update(
        self,
        user_id,
        *,
        display_name=None,
        enabled=None,
        can_pair_irc=None,
        role=None,
        can_generate_images=None,
        can_manage_prompt_rules=None,
    ):
        """Trusted local API; web callers must use admin_update instead."""
        self._validate_fields(
            display_name,
            True if enabled is None else enabled,
            False if can_pair_irc is None else can_pair_irc,
        )
        if role is not None and role not in ('user', 'admin'):
            raise UserError('Invalid role.')
        if can_generate_images is not None and type(can_generate_images) is not bool:
            raise UserError('Image-generation permission must be a boolean.')
        if (
            can_manage_prompt_rules is not None
            and type(can_manage_prompt_rules) is not bool
        ):
            raise UserError('Prompt-rule permission must be a boolean.')
        with self._transaction():
            self._update(
                user_id,
                {
                    'display_name': display_name,
                    'enabled': enabled,
                    'can_pair_irc': can_pair_irc,
                    'role': role,
                    'can_generate_images': can_generate_images,
                    'can_manage_prompt_rules': can_manage_prompt_rules,
                },
            )
        return self.get_by_id(user_id)

    def _update(self, user_id, fields):
        user = self.get_by_id(user_id)
        changes = {
            key: value
            for key, value in fields.items()
            if value is not None and value != getattr(user, key)
        }
        if (
            user.enabled
            and user.role == 'admin'
            and (changes.get('enabled') is False or changes.get('role') == 'user')
            and self.db.execute(
                "SELECT count(*) FROM users WHERE role = 'admin' AND enabled = 1"
            ).fetchone()[0]
            <= 1
        ):
            raise UserError('Cannot remove or disable the last active administrator.')
        if changes:
            assignments = ', '.join(f'{key} = ?' for key in changes)
            security = any(key != 'display_name' for key in changes)
            self.db.execute(
                f'UPDATE users SET {assignments}, session_version = session_version + ? WHERE id = ?',
                (*changes.values(), int(security), user_id),
            )
        return {
            key: {'before': getattr(user, key), 'after': value}
            for key, value in changes.items()
        }

    def set_nsfw(self, user, enabled):
        """Save this user's browsing preference without revoking their sessions."""
        if type(enabled) is not bool:
            raise UserError('Torta Mode must be a boolean.')
        with self._transaction():
            fresh = self.get_by_id(user.id)
            if (
                not fresh.enabled
                or fresh.must_change_password
                or fresh.session_version != user.session_version
            ):
                raise InvalidCredentials('Sign in again.')
            self.db.execute(
                'UPDATE users SET nsfw = ? WHERE id = ?', (enabled, user.id)
            )
        return self.get_by_id(user.id)

    def admin_update(
        self,
        actor,
        target_id,
        *,
        role,
        enabled,
        can_generate_images,
        can_manage_prompt_rules=None,
    ):
        if (
            role not in ('user', 'admin')
            or type(enabled) is not bool
            or type(can_generate_images) is not bool
        ):
            raise UserError('Invalid account settings.')
        if (
            can_manage_prompt_rules is not None
            and type(can_manage_prompt_rules) is not bool
        ):
            raise UserError('Invalid prompt-rule permission.')
        with self._transaction():
            self._require_admin(actor.id, actor.session_version)
            changes = self._update(
                target_id,
                {
                    'role': role,
                    'enabled': enabled,
                    'can_generate_images': can_generate_images,
                    'can_manage_prompt_rules': can_manage_prompt_rules,
                },
            )
            self._audit(actor.id, target_id, 'update_user', changes)

    def admin_reset_password(self, actor, target_id):
        # Random, policy-compliant secret is returned once, never persisted in clear.
        password = 'Aa1-' + secrets.token_urlsafe(24)
        password_hash = _hash_password(password)
        with self._transaction():
            self._require_admin(actor.id, actor.session_version)
            self.get_by_id(target_id)
            self._write_password(target_id, password_hash, True)
            self._audit(
                actor.id, target_id, 'reset_password', {'must_change_password': True}
            )
        return password

    def change_password(self, user, current_password, password):
        password_hash = _hash_password(password)
        with self._transaction():
            fresh = self.authenticate(user.username, current_password)
            if fresh.session_version != user.session_version:
                raise InvalidCredentials('Sign in again.')
            if _verify_password(
                password,
                self.db.execute(
                    'SELECT password_hash FROM user_passwords WHERE user_id = ?',
                    (user.id,),
                ).fetchone()[0],
            ):
                raise UserError('Choose a different password.')
            self._write_password(user.id, password_hash, False)
