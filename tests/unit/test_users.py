import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import pytest

from pmxbot.music import MusicLibrary
from pmxbot.users import (
    InvalidUsername,
    UserError,
    UserNotFound,
    UserStore,
    UsernameTaken,
)


@pytest.fixture
def uri(tmp_path):
    return f'sqlite:{tmp_path / "bot.sqlite"}'


@pytest.fixture
def store(uri):
    with closing(UserStore(uri)) as store:
        yield store


def test_persistence_and_lookup(store, uri):
    assert store.list_users() == []
    alice = store.create(' Alice ', display_name='Alice Example')
    bob = store.create('bob')
    assert alice.id > 0
    assert bob.id > alice.id
    assert alice.username == 'Alice'
    assert alice.normalized_username == 'alice'
    assert alice.display_name == 'Alice Example'
    assert bob.display_name == 'bob'
    datetime.strptime(alice.created_at, '%Y-%m-%d %H:%M:%S')
    assert alice.enabled is True
    assert alice.can_pair_irc is False
    assert alice.may_pair_irc is False
    with closing(UserStore(uri)) as reopened:
        assert reopened.get_by_id(alice.id) == alice
        assert reopened.get_by_username('  ALIce\t') == alice
        assert reopened.list_users() == [alice, bob]


def test_existing_database_is_preserved(tmp_path):
    database = tmp_path / 'existing.sqlite'
    library = MusicLibrary(database)
    album = library.create_album('Band', 'Album', created_by='alice')
    with sqlite3.connect(str(database)) as db:
        db.execute(
            'CREATE TABLE logs (id INTEGER PRIMARY KEY, nick TEXT, message TEXT)'
        )
        db.execute("INSERT INTO logs VALUES (7, 'alice', 'historical message')")
        before = {
            name: db.execute(f'SELECT * FROM {name}').fetchall()
            for name in (
                'logs',
                'artists',
                'albums',
                'album_images',
                'album_image_failures',
            )
        }
    for _ in range(2):
        with closing(UserStore(str(database))) as store:
            assert store.list_users() == []
            for name, rows in before.items():
                assert store.db.execute(f'SELECT * FROM {name}').fetchall() == rows
    assert library.get_album(album['id']) == album


@pytest.mark.parametrize('username', ['alice', 'ALICE', ' Alice\t'])
def test_normalized_uniqueness(store, username):
    alice = store.create('Alice')
    with pytest.raises(UsernameTaken):
        store.create(username)
    assert store.list_users() == [alice]


@pytest.mark.parametrize(
    'username',
    [
        '',
        ' \t\n',
        None,
        123,
        'a b',
        '.alice',
        '_alice',
        '-alice',
        'a/b',
        'a@b',
        'é',
        'Ａlice',
        'a\x00b',
        'a\nb',
        'a' * 65,
    ],
)
def test_invalid_usernames(store, username):
    with pytest.raises(InvalidUsername):
        store.create(username)
    with pytest.raises(InvalidUsername):
        store.get_by_username(username)
    assert store.list_users() == []


@pytest.mark.parametrize('username', ['a', '0', 'A_b.c-9', 'a' * 64])
def test_username_boundaries(store, username):
    assert store.create(username) == store.get_by_username(username.upper())


def test_sqlite_enforces_identity_constraints(store):
    store.create('Alice')
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        store.db.execute(
            "INSERT INTO users (username, normalized_username, display_name) "
            "VALUES ('ALICE', 'alice', 'duplicate')"
        )
    for username, normalized in [('Bob', 'BOB'), ('a b', 'a b'), ('a\x00b', 'a\x00b')]:
        with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
            store.db.execute(
                'INSERT INTO users (username, normalized_username, display_name) '
                'VALUES (?, ?, ?)',
                (username, normalized, 'invalid'),
            )
    for column in ('enabled', 'can_pair_irc'):
        with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
            store.db.execute(f'UPDATE users SET {column} = 2')


def test_concurrent_create(uri):
    barrier = threading.Barrier(2)

    def create(username):
        with closing(UserStore(uri)) as store:
            barrier.wait(timeout=10)
            try:
                return store.create(username)
            except UsernameTaken:
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ['Alice', ' ALICE ']))
    assert results.count(None) == 1
    with closing(UserStore(uri)) as store:
        assert store.list_users() == [
            next(user for user in results if user is not None)
        ]


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('can_pair_irc', [False, True])
def test_permission_policy_and_updates(store, uri, enabled, can_pair_irc):
    original = store.create('alice')
    updated = store.update(
        original.id, display_name='New Name', enabled=enabled, can_pair_irc=can_pair_irc
    )
    assert updated.id == original.id
    assert updated.username == original.username
    assert updated.created_at == original.created_at
    assert updated.display_name == 'New Name'
    assert updated.enabled is enabled
    assert updated.can_pair_irc is can_pair_irc
    assert updated.may_pair_irc is (enabled and can_pair_irc)
    with closing(UserStore(uri)) as reopened:
        assert reopened.get_by_id(original.id) == updated
        assert reopened.update(original.id) == updated
        # Updating one field preserves the others, including explicit false.
        renamed = reopened.update(original.id, display_name='')
        assert renamed.display_name == ''
        assert renamed.enabled is enabled
        assert renamed.can_pair_irc is can_pair_irc


def test_create_disabled_account(store):
    user = store.create('alice', enabled=False, can_pair_irc=True)
    assert user.enabled is False
    assert user.may_pair_irc is False


def test_missing_users(store):
    for operation in (
        lambda: store.get_by_id(999),
        lambda: store.get_by_username('missing'),
        lambda: store.update(999, enabled=False),
    ):
        with pytest.raises(UserNotFound):
            operation()


@pytest.mark.parametrize(
    'fields', [{'enabled': 'false'}, {'can_pair_irc': 1}, {'display_name': 5}]
)
def test_invalid_fields(store, fields):
    user = store.create('alice')
    with pytest.raises(UserError):
        store.create('bob', **fields)
    with pytest.raises(UserError):
        store.update(user.id, **fields)
    assert store.list_users() == [user]


def test_ids_not_reused(store):
    original = store.create('alice')
    store.db.execute('DELETE FROM users WHERE id = ?', (original.id,))
    assert store.create('bob').id > original.id


@pytest.mark.parametrize('uri', ['mongodb://localhost/pmxbot', 'sqlite:'])
def test_unsupported_database(uri):
    with pytest.raises(UserError, match='SQLite'):
        UserStore(uri)
