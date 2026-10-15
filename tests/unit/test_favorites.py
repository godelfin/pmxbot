"""Favorites are personal preferences, with private ownership and batch defaults."""

# Imported pytest fixtures intentionally share test argument names.
# ruff: noqa: F811

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from pmxbot.favorites import FavoriteError, FavoriteStore
from pmxbot.music import MusicLibrary
from pmxbot.users import UserStore
from tests.unit.test_album_page import insert, page  # noqa: F401


@pytest.fixture
def artwork(page):
    cache, request = page
    insert(cache)
    library = MusicLibrary(cache.database)
    with closing(cache.connect()) as db:
        for identifier in (43, 44):
            db.execute(
                '''INSERT INTO image_cache
                (id, cache_key, prompt, normalized_prompt, settings_json, local_filename,
                 hosted_url, thumb_url, generation_metadata_json, created_at)
                VALUES (?, ?, '', '', '{}', '/unused', ?, ?, '{}', '2026-02-01')''',
                (
                    identifier,
                    str(identifier),
                    f'https://art.example/{identifier}',
                    f'https://art.example/thumb/{identifier}',
                ),
            )
            library.record_image(42, str(identifier))
    with closing(UserStore('sqlite:' + str(cache.database))) as store:
        alice = store.create('Alice', password='correct password')
        bob = store.create('Bob', password='correct password')
    return cache, request, alice, bob


def selected(cache, user=None, explicit=None):
    return cache.get_album_page(42, explicit, user.id if user else None)[1]['id']


def token(request):
    return BeautifulSoup(request('/albums/42')['body'], 'html.parser').select_one(
        '[name=csrf_token]'
    )['value']


def favorite(request, image=42, **extra):
    return request(
        '/favorites',
        method='POST',
        data=dict(
            album_id='42', image_id=str(image), csrf_token=token(request), **extra
        ),
        accept='application/json',
    )


def login(request, username='Alice'):
    assert (
        request(
            '/login',
            method='POST',
            data={'username': username, 'password': 'correct password'},
        )['status']
        == 303
    )


def test_toggle_switch_unique_and_persistent(artwork):
    cache, _, alice, _ = artwork
    store = FavoriteStore(cache.database)
    assert not alice.can_generate_images
    assert store.toggle(alice, 42, 42) == 42
    assert selected(cache, alice) == 42
    assert store.toggle(alice, 42, 43) == 43
    with closing(store.connect()) as db:
        rows = db.execute(
            'SELECT user_id, album_id, image_id, created_at FROM user_album_favorites'
        ).fetchall()
        assert len(rows) == 1 and tuple(rows[0][:3]) == (alice.id, 42, 43)
        datetime.strptime(rows[0][3], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                'INSERT INTO user_album_favorites (user_id, album_id, image_id, cache_key) VALUES (?, 42, 42, ?)',
                (alice.id, 'key'),
            )
    assert selected(cache, alice) == 43
    assert FavoriteStore(cache.database).toggle(alice, 42, 43) is None
    assert selected(cache, alice) == 44


def test_default_precedence_and_ties(artwork):
    cache, _, alice, bob = artwork
    store = FavoriteStore(cache.database)
    assert selected(cache) == 44
    store.toggle(alice, 42, 42)
    assert selected(cache) == 42  # Unique aggregate winner for anonymous visitors.
    assert selected(cache, bob) == 42  # No personal favorite yet.
    store.toggle(bob, 42, 43)
    assert selected(cache) == 44  # Tied votes fall back to most recent, then ID.
    assert selected(cache, alice) == 42
    assert selected(cache, bob) == 43
    assert selected(cache, alice, 44) == 44
    with pytest.raises(LookupError):
        selected(cache, alice, 999)
    with closing(UserStore('sqlite:' + str(cache.database))) as users:
        third = users.create('third')
    store.toggle(third, 42, 43)
    assert selected(cache) == 43
    assert selected(cache, alice) == 42
    # created_at retains priority over image ID for the latest-image fallback.
    store.toggle(third, 42, 43)
    with closing(cache.connect()) as db:
        db.execute("UPDATE image_cache SET created_at = '2027' WHERE id = 42")
    assert selected(cache) == 42


@pytest.mark.parametrize('delete', ['user', 'album', 'image', 'link'])
def test_foreign_keys_ownership_and_deletion(artwork, delete):
    cache, _, alice, _ = artwork
    store = FavoriteStore(cache.database)
    store.toggle(alice, 42, 43)
    other = MusicLibrary(cache.database).create_album('Other', 'Other')
    with closing(store.connect()) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE user_album_favorites SET album_id = ?', (other['id'],))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE user_album_favorites SET image_id = 42 WHERE cache_key = '43'"
            )
        statement, params = {
            'user': ('DELETE FROM users WHERE id = ?', (alice.id,)),
            'album': ('DELETE FROM albums WHERE id = ?', (42,)),
            'image': ('DELETE FROM image_cache WHERE id = ?', (43,)),
            'link': (
                'DELETE FROM album_images WHERE album_id = ? AND cache_key = ?',
                (42, '43'),
            ),
        }[delete]
        db.execute(statement, params)
        assert not db.execute('SELECT * FROM user_album_favorites').fetchall()
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_zero_one_and_foreign_images_rejected(artwork):
    cache, _, alice, _ = artwork
    store = FavoriteStore(cache.database)
    library = MusicLibrary(cache.database)
    empty = library.create_album('Empty', 'Empty')['id']
    assert cache.get_album_page(empty)[1] is None
    with pytest.raises(FavoriteError):
        store.toggle(alice, empty, 42)
    # Move versions to another album so only one remains in the first.
    with closing(store.connect()) as db:
        db.execute(
            'UPDATE album_images SET album_id = ? WHERE cache_key != ?', (empty, 'key')
        )
    assert cache.get_album_page(42)[1]['id'] == 42
    for album, image in ((42, 42), (empty, 42), (999, 43), (empty, 999)):
        with pytest.raises(FavoriteError):
            store.toggle(alice, album, image)
    assert store.toggle(alice, empty, 43) == 43


def test_concurrent_updates_are_serialized(artwork):
    cache, _, alice, bob = artwork
    store = FavoriteStore(cache.database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda image: store.toggle(alice, 42, image), (42, 43, 44, 42))
        )
    with closing(store.connect()) as db:
        rows = db.execute(
            'SELECT image_id FROM user_album_favorites WHERE user_id = ?', (alice.id,)
        ).fetchall()
        assert len(rows) <= 1
        assert not rows or rows[0][0] in results
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert (
            list(pool.map(lambda _: store.toggle(bob, 42, 42), range(2))).count(None)
            == 1
        )
    with closing(store.connect()) as db:
        assert not db.execute(
            'SELECT 1 FROM user_album_favorites WHERE user_id = ?', (bob.id,)
        ).fetchone()


def test_gallery_resolves_in_constant_queries(artwork, monkeypatch):
    cache, _, alice, bob = artwork
    store = FavoriteStore(cache.database)
    store.toggle(alice, 42, 42)
    store.toggle(bob, 42, 43)
    library = MusicLibrary(cache.database)
    for number in range(20):
        library.create_album('Artist', str(number))
    original = cache.read_connection
    statements = []

    def read():
        db = original()
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(cache, 'read_connection', read)
    _small, _ = cache.catalog_albums(1, 1, user_id=alice.id)
    count = len(statements)
    statements.clear()
    large, total = cache.catalog_albums(1, 24, user_id=alice.id)
    assert len(statements) == count and total == 21
    album = next(album for album in large if album['id'] == 42)
    assert album['hosted_url'].endswith('art.png?x=1&y=2')
    for user, expected in ((bob, '/43'), (None, '/44')):
        albums, _ = cache.catalog_albums(1, user_id=user.id if user else None)
        album = next(album for album in albums if album['id'] == 42)
        assert album['hosted_url'].endswith(expected)
        assert album['thumb_url'].endswith('/thumb' + expected)


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_http_auth_csrf_own_favorites_and_initial_selection(artwork):
    cache, request, alice, bob = artwork
    assert favorite(request)['status'] == 401
    assert request('/favorites?album_id=42')['status'] == 401
    assert not BeautifulSoup(request()['body'], 'html.parser').select(
        '#favorite-toggle'
    )
    login(request)
    assert (
        request(
            '/favorites',
            method='POST',
            data={'album_id': '42', 'image_id': '42', 'csrf_token': ''},
        )['status']
        == 403
    )
    assert (
        request(
            '/favorites?album_id=42',
            method='POST',
            data={'album_id': '42', 'image_id': '42', 'csrf_token': token(request)},
        )['status']
        == 400
    )
    result = favorite(request)
    assert result['status'] == 200 and json.loads(result['body']) == {
        'favorite_image_id': '42'
    }
    soup = BeautifulSoup(request()['body'], 'html.parser')
    assert soup.select_one('#favorite-toggle')['aria-pressed'] == 'true'
    assert soup.select_one('[name=source_image_id]')['value'] == '42'
    explicit = request('/albums/42?source_image_id=44', accept='application/json')
    payload = json.loads(explicit['body'])
    assert payload['image_id'] == '44'
    assert 'aria-pressed="false"' in payload['fragments']['artwork']
    assert 'favorite-toggle' in payload['fragments']['artwork']
    # Session rotation does not erase the stored preference.
    login(request)
    assert (
        json.loads(request('/favorites?album_id=42')['body'])['favorite_image_id']
        == '42'
    )
    login(request, 'Bob')
    assert (
        json.loads(request('/favorites?album_id=42')['body'])['favorite_image_id']
        is None
    )
    assert favorite(request, 43)['status'] == 200
    assert selected(cache, alice) == 42 and selected(cache, bob) == 43
    gallery = BeautifulSoup(request('/gallery')['body'], 'html.parser')
    assert gallery.select_one('.gallery img')['src'] == 'https://art.example/thumb/43'
    assert favorite(request, 43)['status'] == 200
    assert selected(cache, alice) == 42
    assert (
        request(
            '/favorites',
            method='POST',
            data={
                'album_id': '42',
                'image_id': '43',
                'user_id': str(alice.id),
                'csrf_token': token(request),
            },
        )['status']
        >= 400
    )
    assert selected(cache, alice) == 42
    assert favorite(request, 999)['status'] == 400


def test_star_only_for_authenticated_multiple_versions(artwork):
    cache, request, _, _ = artwork
    assert not BeautifulSoup(request()['body'], 'html.parser').select(
        '#favorite-toggle'
    )
    login(request)
    assert BeautifulSoup(request()['body'], 'html.parser').select('#favorite-toggle')
    with closing(cache.connect()) as db:
        db.execute("DELETE FROM album_images WHERE cache_key != 'key'")
    assert not BeautifulSoup(request()['body'], 'html.parser').select(
        '#favorite-toggle'
    )
    assert favorite(request)['status'] == 400
    with closing(cache.connect()) as db:
        db.execute('DELETE FROM album_images')
    assert not BeautifulSoup(request()['body'], 'html.parser').select(
        '#favorite-toggle'
    )
    assert favorite(request)['status'] == 400


def test_revoked_session_cannot_change_favorites(artwork):
    cache, request, alice, _ = artwork
    login(request)
    with closing(UserStore('sqlite:' + str(cache.database))) as users:
        users.update(alice.id, enabled=False)
    assert favorite(request)['status'] == 401
    with pytest.raises(PermissionError):
        FavoriteStore(cache.database).toggle(alice, 42, 42)
