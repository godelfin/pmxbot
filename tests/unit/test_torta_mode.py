"""An authenticated, per-user opt-in for otherwise hidden collection entries."""

# Imported pytest fixtures intentionally share test argument names.
# ruff: noqa: F811

import sqlite3
from contextlib import closing

import pytest
from bs4 import BeautifulSoup

import pmxbot
from pmxbot.images import ImageCache
from pmxbot.music import MusicLibrary
from pmxbot.users import InvalidCredentials, UserError, UserStore, _hash_password
from tests.unit.test_web_auth import web  # noqa: F401


def mode(web, enabled='1', **extra):
    data = {
        'enabled': enabled,
        'csrf_token': web.token('/'),
        'return_to': web.base + '/gallery',
    }
    data.update(extra)
    return web.request('/torta_mode', 'POST', data)


@pytest.fixture
def collection(web):
    cache = ImageCache(pmxbot.config)
    library = MusicLibrary(cache.database)
    hidden = [
        library.create_album('Las Tortas', 'First'),
        library.create_album('Band', 'A Torta!'),
    ]
    visible = [
        library.create_album('Tortasaurus', 'Second'),
        library.create_album('Band', 'Retorta'),
    ]
    return cache, hidden, visible


def test_additive_migration_preserves_users_and_permissions(tmp_path):
    database = tmp_path / 'old.sqlite'
    encoded = _hash_password('correct password')
    with sqlite3.connect(database) as db:
        db.execute(
            '''CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT,
            normalized_username TEXT, display_name TEXT, created_at TEXT,
            enabled INTEGER, can_pair_irc INTEGER, role TEXT,
            can_generate_images INTEGER, must_change_password INTEGER, session_version INTEGER)'''
        )
        db.execute(
            "INSERT INTO users VALUES (7, 'Alice', 'alice', 'Alice', '2020', 1, 1, 'admin', 0, 0, 4)"
        )
        db.execute(
            'CREATE TABLE user_passwords (user_id INTEGER PRIMARY KEY, password_hash TEXT)'
        )
        db.execute('INSERT INTO user_passwords VALUES (7, ?)', (encoded,))
    for _ in range(2):
        with closing(UserStore(str(database))) as store:
            user = store.authenticate('Alice', 'correct password')
            assert not user.nsfw and not user.has_permission('nsfw')
            assert user.is_admin and user.can_pair_irc and not user.can_generate_images
            assert user.session_version == 4
            assert store.db.execute(
                'SELECT password_hash FROM user_passwords'
            ).fetchone() == (encoded,)
            assert not store.create('new-' + str(_)).nsfw
            with pytest.raises(sqlite3.IntegrityError):
                store.db.execute('UPDATE users SET nsfw = 2 WHERE id = 7')


@pytest.mark.parametrize(
    'artist,title,visible',
    [
        ('Torta', 'Album', False),
        ('TORTAS!', 'Album', False),
        ('A torta band', 'Album', False),
        ('Band', 'A Torta!', False),
        ('Band', 'The tortas-song', False),
        ('Tortasaurus', 'Album', True),
        ('Band', 'Retorta', True),
        ('Band', 'Tortas2', True),
        ('Band', 'Ordinary', True),
    ],
)
def test_whole_word_matching(collection, artist, title, visible):
    cache, _, _ = collection
    album = MusicLibrary(cache.database).create_album(artist, title)
    rows, _ = cache.catalog_albums(1, 100)
    assert (album['id'] in {row['id'] for row in rows}) is visible
    rows, _ = cache.catalog_albums(1, 100, include_nsfw=True)
    assert album['id'] in {row['id'] for row in rows}


def test_filter_precedes_search_counts_and_pagination(collection):
    cache, hidden, visible = collection
    rows, total = cache.catalog_albums(1, 1, q='torta')
    assert total == 2 and len(rows) == 1
    assert rows[0]['id'] in {album['id'] for album in visible}
    rows2, total2 = cache.catalog_albums(2, 1, q='torta')
    assert total2 == total and rows2[0]['id'] != rows[0]['id']
    rows, total = cache.catalog_albums(1, 100, q='torta', include_nsfw=True)
    assert total == 4
    assert {row['id'] for row in rows} == {album['id'] for album in hidden + visible}
    assert cache.catalog_albums(1, artist='Tortas')[1] == 1
    assert cache.catalog_albums(1, album_title='A Torta!')[1] == 0
    assert cache.gallery_albums(1)[1] == 3  # Plus the web fixture's original album.
    assert cache.gallery_albums(1, include_nsfw=True)[1] == 5


def test_opt_in_is_personal_persistent_and_logout_restores_filter(web, collection):
    cache, hidden, _ = collection
    for view in ('/', '/gallery'):
        body = web.request(view)['body']
        header = BeautifulSoup(body, 'html.parser').select_one('.site-header')
        assert 'Torta Mode' not in header.get_text() and 'Las Tortas' not in body
    assert web.login()['status'] == 303
    assert mode(web)['status'] == 303
    assert (
        web.request('/protected')['status'] == 200
    )  # Preference does not revoke sessions.
    for view in ('/', '/gallery'):
        soup = BeautifulSoup(web.request(view)['body'], 'html.parser')
        assert soup.select_one('.torta-mode [name=enabled]').has_attr('checked')
        assert 'Las Tortas' in soup.get_text()
    with closing(UserStore(pmxbot.config.database)) as store:
        alice = store.get_by_id(web.user_id)
        assert alice.nsfw and not alice.can_generate_images and alice.role == 'user'
        bob = store.create('Bob', password='correct password')
        assert not bob.nsfw
    assert (
        web.request('/logout', 'POST', {'csrf_token': web.token('/')})['status'] == 303
    )
    assert not BeautifulSoup(web.request('/')['body'], 'html.parser').select(
        '.torta-mode'
    )
    assert 'Las Tortas' not in web.request('/gallery')['body']
    # Direct URLs are readable both signed out and with filtering enabled.
    assert web.request('/albums/' + str(hidden[0]['id']))['status'] == 200
    assert web.login(username='Bob')['status'] == 303
    assert 'Las Tortas' not in web.request('/')['body']
    assert web.login()['status'] == 303
    assert 'Las Tortas' in web.request('/')['body']
    assert mode(web, '0')['status'] == 303
    assert 'Las Tortas' not in web.request('/')['body']
    assert web.request('/albums/' + str(hidden[1]['id']))['status'] == 200
    assert cache.catalog_albums(1)[1] == 3


def test_mutations_require_auth_csrf_and_only_change_current_user(web):
    assert web.request('/torta_mode', 'POST', {'enabled': '1'})['status'] == 401
    assert web.login()['status'] == 303
    assert mode(web, csrf_token='')['status'] == 403
    assert mode(web, 'yes')['status'] == 400
    assert web.request('/torta_mode')['status'] == 405
    assert web.request('/?include_nsfw=1')['status'] == 400
    assert mode(web, user_id='999')['status'] >= 400
    result = web.request(
        '/torta_mode?enabled=1', 'POST', {'csrf_token': web.token('/')}
    )
    assert result['status'] == 400
    assert (
        mode(web, return_to='https://evil.test')['headers']['Location']
        == web.base + '/'
    )
    with closing(UserStore(pmxbot.config.database)) as store:
        user = store.get_by_id(web.user_id)
        assert user.nsfw and not user.can_generate_images and user.role == 'user'
        assert not store.get_by_username('disabled').nsfw
        with pytest.raises(UserError):
            store.set_nsfw(user, 'false')
        store.update(user.id, enabled=False)
        with pytest.raises(InvalidCredentials):
            store.set_nsfw(user, True)
    assert (
        web.request('/torta_mode', 'POST', {'enabled': '1', 'csrf_token': ''})['status']
        == 401
    )


def test_control_on_every_authenticated_page_and_return_url(web):
    assert web.login()['status'] == 303
    paths = (
        '/',
        '/gallery',
        '/albums/' + str(web.album_id),
        '/login',
        '/change_password',
        '/jobs',
    )
    for path in paths:
        response = web.request(path)
        assert response['status'] == 200
        soup = BeautifulSoup(response['body'], 'html.parser')
        form = soup.select_one('.torta-mode')
        assert form['action'] == web.base + '/torta_mode'
        assert form.select_one('[name=enabled]')['type'] == 'checkbox'
        assert not form.select_one('[name=enabled]').has_attr('checked')
        assert form.select_one('[name=return_to]')['value'] == web.base + path
    target = web.base + '/gallery?q=Band&sort=album&page=1'
    assert mode(web, return_to=target)['headers']['Location'] == target


def test_neighbors_follow_same_visibility_policy(collection):
    cache, hidden, visible = collection
    filters = {'sort': 'album', 'order': 'asc'}
    for album in visible:
        neighbors = cache.album_neighbors(album['id'], **filters)
        assert not {neighbors['previous'], neighbors['next']} & {
            album['id'] for album in hidden
        }
    assert cache.album_neighbors(hidden[0]['id'], **filters) == {
        'previous': None,
        'next': None,
    }
    assert cache.album_neighbors(hidden[0]['id'], include_nsfw=True, **filters) != {
        'previous': None,
        'next': None,
    }
