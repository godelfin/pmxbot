import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
from PIL import Image

from pmxbot.backfill_thumbnails import backfill, main
from pmxbot.images import ImageCache, ImageError


@pytest.fixture
def cache(tmp_path):
    database = tmp_path / 'pmxbot.sqlite'
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            '''CREATE TABLE image_cache (id INTEGER PRIMARY KEY, cache_key TEXT,
            local_filename TEXT, thumb_url TEXT, hosted_url TEXT)'''
        )
        db.executemany(
            'INSERT INTO image_cache VALUES (?, ?, ?, ?, ?)',
            [
                (1, 'first', 'original.png', None, 'https://original.example/1'),
                (2, 'empty', '', None, None),
                (3, 'done', 'original.png', 'https://existing.example/thumb', None),
                (4, 'second', 'original.png', None, None),
                (5, 'missing', 'missing.png', None, None),
            ],
        )
    Image.new('RGBA', (800, 400), 'blue').save(tmp_path / 'original.png')
    return ImageCache(
        dict(
            database=f'sqlite:{database}',
            images_directory=str(tmp_path / 'images'),
            r2_endpoint_url='https://r2.example.com',
            r2_bucket='album-images',
            r2_public_url='https://images.example.com',
            r2_access_key_id='test',
            r2_secret_access_key='test',
        )
    )


def read_rows(cache):
    with closing(sqlite3.connect(cache.database)) as db:
        return db.execute('SELECT * FROM image_cache ORDER BY id').fetchall()


def test_upload_failure_leaves_row_pending_and_rerun_skips_success(cache, monkeypatch):
    destinations = []

    def upload(filename, destination):
        with Image.open(filename) as image:
            assert image.size == (512, 512)
            assert image.format == 'JPEG'
            assert image.getpixel((0, 0)) == (255, 255, 255)
        destinations.append(destination)
        if destination['key'] == 'first_thumb.jpg':
            raise ImageError('Upload failed')

    monkeypatch.setattr(cache, 'upload', upload)
    before = read_rows(cache)
    directory = cache.directory
    assert backfill(cache, source_root=cache.database.parent) == 2
    after = read_rows(cache)
    assert after[:3] == before[:3]
    assert after[3] == (
        4,
        'second',
        'original.png',
        'https://images.example.com/second_thumb.jpg',
        None,
    )
    assert after[4] == before[4]
    assert [item['key'] for item in destinations] == [
        'first_thumb.jpg',
        'second_thumb.jpg',
    ]
    assert all(item['bucket'] == 'album-images' for item in destinations)
    assert cache.directory == directory
    assert not directory.exists()
    upload_mock = Mock()
    monkeypatch.setattr(cache, 'upload', upload_mock)
    assert backfill(cache, source_root=cache.database.parent) == 1
    assert upload_mock.call_count == 1
    assert read_rows(cache)[0][3] == 'https://images.example.com/first_thumb.jpg'
    assert read_rows(cache)[0][4] == before[0][4]


def test_dry_run_needs_no_credentials_and_writes_nothing(cache, monkeypatch):
    before = read_rows(cache)
    cache.r2 = {}
    upload = Mock()
    save = Mock()
    monkeypatch.setattr(cache, 'upload', upload)
    monkeypatch.setattr(cache, 'save_thumbnail', save)
    assert backfill(cache, dry_run=True, source_root=cache.database.parent) == 1
    assert read_rows(cache) == before
    upload.assert_not_called()
    save.assert_not_called()
    assert not cache.directory.exists()


def test_missing_database_is_not_created(tmp_path):
    database = tmp_path / 'missing.sqlite'
    with pytest.raises(SystemExit) as exc:
        main(['--database', str(database), '--dry-run'])
    assert exc.value.code == 1
    assert not database.exists()
