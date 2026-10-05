import sqlite3
from contextlib import closing

import pytest

from pmxbot.migrate_music_images import migrate, parse_prompt
from pmxbot.music import MusicLibrary


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'bot.sqlite'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            'CREATE TABLE music_image_ids (id INTEGER PRIMARY KEY, cache_key TEXT UNIQUE)'
        )
        db.execute('''CREATE TABLE image_cache (cache_key TEXT PRIMARY KEY,
            prompt TEXT, requested_by TEXT, created_at TEXT)''')
        for identifier, title, genre in [
            (1, 'Record', 'Jazz'),
            (2, 'RECORD', 'Rock'),
            (3, 'Other', 'Pop'),
        ]:
            key = f'key-{identifier}'
            prompt = (
                f'an album cover for the band Band. the name of the album is {title}. '
                f'this is the Vinyl, Remastered edition. the genre of music is {genre}.'
            )
            db.execute('INSERT INTO music_image_ids VALUES (?, ?)', (identifier, key))
            db.execute(
                'INSERT INTO image_cache VALUES (?, ?, ?, ?)',
                (key, prompt, 'alice', '2025-01-02 03:04:05'),
            )
        db.execute("INSERT INTO music_image_ids VALUES (4, 'missing')")
        db.execute("INSERT INTO music_image_ids VALUES (5, 'unparseable')")
        db.execute(
            "INSERT INTO image_cache VALUES ('unparseable', 'a cat', 'bob', '2025-01-01')"
        )
    return path


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return '\n'.join(db.iterdump())


def test_dry_run_does_not_change_database(database):
    before = database.read_bytes()
    report = migrate(database)
    assert not report['applied']
    assert report['linked'] == 3
    assert len(report['skipped']) == 2
    assert database.read_bytes() == before


def test_apply_preserves_keys_attribution_and_is_idempotent(database, tmp_path):
    before = dump(database)
    backup = tmp_path / 'backup.sqlite'
    report = migrate(database, apply=True, backup=backup)
    assert dump(backup) == before
    assert [item['album_id'] for item in report['mapping']] == [1, 1, 3]
    library = MusicLibrary(database)
    album = library.get_album(1)
    assert album['genre'] == 'Jazz'
    assert album['created_by'] == 'alice'
    assert album['created_at'] == '2025-01-02 03:04:05'
    assert [image['cache_key'] for image in album['images']] == ['key-1', 'key-2']
    after = dump(database)
    rerun = migrate(database, apply=True, backup=tmp_path / 'second.sqlite')
    assert rerun['linked'] == 0
    assert rerun['already_linked'] == 3
    assert dump(database) == after
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('SELECT COUNT(*) FROM music_image_ids').fetchone()[0] == 5
        assert db.execute('SELECT COUNT(*) FROM image_cache').fetchone()[0] == 4


def test_existing_album_and_id_collision(database, tmp_path):
    library = MusicLibrary(database)
    existing = library.create_album(
        'Band', 'Record', genre='Original', created_by='bob'
    )
    with closing(library.connect()) as db, db:
        db.execute(
            "INSERT INTO artists (id, name, normalized_name) VALUES (99, 'Unrelated', 'unrelated')"
        )
        db.execute(
            "INSERT INTO albums (id, artist_id, title, normalized_title) VALUES (3, 99, 'Keep', 'keep')"
        )
    report = migrate(database, apply=True, backup=tmp_path / 'backup.sqlite')
    assert report['mapping'][0]['album_id'] == existing['id']
    assert report['mapping'][2]['album_id'] > existing['id']
    assert library.get_album(3)['title'] == 'Keep'
    assert library.get_album(existing['id'])['genre'] == 'Original'
    assert library.get_album(existing['id'])['created_by'] == 'bob'


def test_requires_new_backup(database, tmp_path):
    before = database.read_bytes()
    with pytest.raises(ValueError, match='backup'):
        migrate(database, apply=True)
    with pytest.raises(FileExistsError):
        migrate(database, apply=True, backup=database)
    assert database.read_bytes() == before


def test_row_import_rolls_back_on_error(database, tmp_path):
    with closing(MusicLibrary(database).connect()) as db, db:
        db.execute(
            '''CREATE TRIGGER fail_link BEFORE INSERT ON album_images
            WHEN NEW.cache_key = 'key-2' BEGIN SELECT RAISE(ABORT, 'test failure'); END'''
        )
    before = dump(database)
    with pytest.raises(sqlite3.IntegrityError):
        migrate(database, apply=True, backup=tmp_path / 'backup.sqlite')
    assert dump(database) == before


@pytest.mark.parametrize(
    'suffix', ['.', ', but nowhere should the genre be mentioned.']
)
def test_prompt_versions(suffix):
    parsed = parse_prompt(
        'an album cover for the band A. the name of the album is B. '
        'this is the Vinyl, Deluxe edition. the genre of music is Jazz' + suffix
    )
    assert parsed == {
        'artist': 'A',
        'title': 'B',
        'format': 'Vinyl',
        'format_description': 'Deluxe',
        'genre': 'Jazz',
    }
    assert (
        parse_prompt('an album cover for the band A. the name of the album is B')[
            'title'
        ]
        == 'B'
    )


@pytest.mark.parametrize(
    'prompt',
    [
        'a cat',
        'an album cover for the band A. the name of the album is B. the name of the album is C.',
        'an album cover for the band A. the name of the album is B. this is the Vinyl, Deluxe, Extra edition. the genre of music is Jazz.',
    ],
)
def test_ambiguous_prompts_are_rejected(prompt):
    with pytest.raises(ValueError):
        parse_prompt(prompt)


@pytest.fixture
def unlinked_database(tmp_path):
    path = tmp_path / 'unlinked.sqlite'
    library = MusicLibrary(path)
    album = library.create_album('Band', 'Record', genre='Keep', created_by='bob')
    library.record_image(album['id'], 'linked')
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('''CREATE TABLE image_cache (id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE, prompt TEXT, requested_by TEXT, created_at TEXT)''')
        for key, prompt in [
            ('linked', 'a cat'),
            (
                'first',
                'an album cover for the band BAND. the name of the album is RECORD.',
            ),
            (
                'second',
                'an album cover for the band Other. the name of the album is New.',
            ),
            (
                'third',
                'an album cover for the band OTHER. the name of the album is NEW.',
            ),
            ('unparseable', 'a cat'),
            ('missing-prompt', None),
        ]:
            db.execute(
                'INSERT INTO image_cache (cache_key, prompt, requested_by, created_at) VALUES (?, ?, ?, ?)',
                (key, prompt, 'alice', '2025-01-02 03:04:05'),
            )
    return path


def test_unlinked_preview_without_legacy_table(unlinked_database):
    before = unlinked_database.read_bytes()
    report = migrate(unlinked_database, unlinked_cache=True)
    assert report['linked'] == 3
    assert [row['image_id'] for row in report['mapping']] == [2, 3, 4]
    assert [row['image_id'] for row in report['skipped']] == [5, 6]
    assert report['mapping'][0]['artist'] == 'BAND'
    assert report['mapping'][0]['title'] == 'RECORD'
    assert unlinked_database.read_bytes() == before


@pytest.mark.parametrize('legacy', [False, True])
def test_unlinked_apply_preserves_records_and_is_idempotent(
    unlinked_database, tmp_path, legacy
):
    if legacy:
        with closing(sqlite3.connect(unlinked_database)) as db, db:
            db.execute(
                'CREATE TABLE music_image_ids (id INTEGER PRIMARY KEY, cache_key TEXT UNIQUE)'
            )
            db.execute("INSERT INTO music_image_ids VALUES (100, 'reserved')")
    with closing(sqlite3.connect(unlinked_database)) as db:
        cached = db.execute('SELECT * FROM image_cache ORDER BY id').fetchall()
    before = dump(unlinked_database)
    backup = tmp_path / 'backup.sqlite'
    report = migrate(unlinked_database, unlinked_cache=True, apply=True, backup=backup)
    assert dump(backup) == before
    assert report['mapping'][0]['album_id'] == 1
    new_album_id = report['mapping'][1]['album_id']
    assert new_album_id > (100 if legacy else 1)
    assert report['mapping'][2]['album_id'] == new_album_id
    library = MusicLibrary(unlinked_database)
    assert library.get_album(1)['genre'] == 'Keep'
    assert library.get_album(1)['created_by'] == 'bob'
    assert library.get_album(new_album_id)['created_by'] == 'alice'
    with closing(sqlite3.connect(unlinked_database)) as db:
        assert db.execute('SELECT * FROM image_cache ORDER BY id').fetchall() == cached
        assert db.execute('SELECT COUNT(*) FROM album_images').fetchone()[0] == 4
    after = dump(unlinked_database)
    rerun = migrate(
        unlinked_database,
        unlinked_cache=True,
        apply=True,
        backup=tmp_path / 'second.sqlite',
    )
    assert rerun['linked'] == 0
    assert rerun['mapping'] == []
    assert len(rerun['skipped']) == 2
    assert dump(unlinked_database) == after


def test_unlinked_import_rolls_back(unlinked_database, tmp_path):
    with closing(sqlite3.connect(unlinked_database)) as db, db:
        db.execute(
            """CREATE TRIGGER fail_link BEFORE INSERT ON album_images
            WHEN NEW.cache_key = 'second' BEGIN SELECT RAISE(ABORT, 'test failure'); END"""
        )
    before = dump(unlinked_database)
    with pytest.raises(sqlite3.IntegrityError):
        migrate(
            unlinked_database,
            unlinked_cache=True,
            apply=True,
            backup=tmp_path / 'backup.sqlite',
        )
    assert dump(unlinked_database) == before


def test_unlinked_requires_numeric_image_ids(database):
    with pytest.raises(ValueError, match='image_cache.id'):
        migrate(database, unlinked_cache=True)
