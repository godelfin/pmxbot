import sqlite3
from contextlib import closing

import pytest

from pmxbot.music import MusicLibrary
from pmxbot.retire_music_image_ids import retire


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'bot.sqlite'
    library = MusicLibrary(path)
    album = library.create_album('Band', 'Album')
    library.record_image(album['id'], 'key')
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            'CREATE TABLE image_cache (id INTEGER PRIMARY KEY, cache_key TEXT UNIQUE)'
        )
        db.execute("INSERT INTO image_cache VALUES (10, 'key')")
        db.execute(
            'CREATE TABLE music_image_ids (id INTEGER PRIMARY KEY, cache_key TEXT UNIQUE)'
        )
        db.execute("INSERT INTO music_image_ids VALUES (1, 'key')")
    return path


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return '\n'.join(db.iterdump())


def test_preview_and_apply_preserve_other_tables(database, tmp_path):
    before = dump(database)
    contents = database.read_bytes()
    assert retire(database) == {'legacy_rows': 1, 'unmigrated': [], 'dropped': False}
    assert database.read_bytes() == contents
    backup = tmp_path / 'backup.sqlite'
    assert retire(database, apply=True, backup=backup)['dropped']
    assert dump(backup) == before
    with closing(sqlite3.connect(database)) as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='music_image_ids'"
        ).fetchone()
        assert db.execute('SELECT * FROM image_cache').fetchall() == [(10, 'key')]
        assert db.execute('SELECT * FROM album_images').fetchall() == [(1, 'key')]
    assert MusicLibrary(database).get_album(1)['title'] == 'Album'
    assert MusicLibrary(database).create_album('Band', 'New')['id'] == 2
    assert retire(database, apply=True)['already_retired']


@pytest.mark.parametrize('missing', ['link', 'cache', 'album', 'artist'])
def test_refuse_unmigrated_ids(database, tmp_path, missing):
    table = {
        'link': 'album_images',
        'cache': 'image_cache',
        'album': 'albums',
        'artist': 'artists',
    }[missing]
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('DELETE FROM ' + table)
    before = dump(database)
    assert retire(database)['unmigrated'] == [1]
    with pytest.raises(ValueError, match='Unmigrated legacy IDs: 1'):
        retire(database, apply=True, backup=tmp_path / 'backup.sqlite')
    assert dump(database) == before
    assert not (tmp_path / 'backup.sqlite').exists()


def test_requires_new_backup(database):
    before = dump(database)
    with pytest.raises(ValueError, match='backup'):
        retire(database, apply=True)
    with pytest.raises(FileExistsError):
        retire(database, apply=True, backup=database)
    assert dump(database) == before
