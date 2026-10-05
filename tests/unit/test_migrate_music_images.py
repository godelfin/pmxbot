import sqlite3
from contextlib import closing

import pytest

from pmxbot.migrate_music_images import migrate, parse_prompt
from pmxbot.music import MusicLibrary


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return '\n'.join(db.iterdump())


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


def test_unlinked_apply_preserves_records_and_is_idempotent(
    unlinked_database, tmp_path
):
    with closing(sqlite3.connect(unlinked_database)) as db:
        cached = db.execute('SELECT * FROM image_cache ORDER BY id').fetchall()
    before = dump(unlinked_database)
    backup = tmp_path / 'backup.sqlite'
    report = migrate(unlinked_database, unlinked_cache=True, apply=True, backup=backup)
    assert dump(backup) == before
    assert report['mapping'][0]['album_id'] == 1
    new_album_id = report['mapping'][1]['album_id']
    assert new_album_id > 1
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


def test_requires_new_backup(unlinked_database, tmp_path):
    before = unlinked_database.read_bytes()
    with pytest.raises(ValueError, match='backup'):
        migrate(unlinked_database, apply=True)
    with pytest.raises(FileExistsError):
        migrate(unlinked_database, apply=True, backup=unlinked_database)
    assert unlinked_database.read_bytes() == before


def test_default_recovers_unlinked_images(unlinked_database):
    assert migrate(unlinked_database)['linked'] == 3


def test_requires_numeric_image_ids(tmp_path):
    path = tmp_path / 'old.sqlite'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE image_cache (cache_key TEXT PRIMARY KEY)')
    with pytest.raises(ValueError, match='image_cache.id'):
        migrate(path)
