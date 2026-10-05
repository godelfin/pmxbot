"""Persistent music library, independent of image generation and IRC commands."""

import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


class MusicLibrary:
    def __init__(self, database):
        self.database = Path(database)

    def connect(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.database), timeout=20)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys = ON')
            db.execute('''CREATE TABLE IF NOT EXISTS artists (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE, genre TEXT, description TEXT,
                created_by TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist_id INTEGER NOT NULL REFERENCES artists(id),
                title TEXT NOT NULL, normalized_title TEXT NOT NULL,
                genre TEXT, format TEXT, format_description TEXT, description TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (artist_id, normalized_title)
            )''')
            db.execute('BEGIN IMMEDIATE')
            db.execute("""CREATE TABLE IF NOT EXISTS album_images (
                album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                cache_key TEXT NOT NULL,
                PRIMARY KEY (album_id, cache_key)
            )""")
            columns = {row['name'] for row in db.execute('PRAGMA table_info(albums)')}
            if 'cache_key' in columns:
                db.execute("""INSERT INTO album_images
                    (album_id, cache_key)
                    SELECT id, cache_key
                    FROM albums WHERE cache_key IS NOT NULL
                    ON CONFLICT(album_id, cache_key) DO NOTHING""")
                db.execute("""UPDATE albums SET cache_key = NULL,
                    image_created_by = NULL, image_created_at = NULL
                    WHERE cache_key IS NOT NULL""")
            # Also upgrades existing tables. Conflicting historical links abort
            # the transaction rather than silently choosing an album owner.
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS album_images_unique_cache_key '
                'ON album_images(cache_key)'
            )
            db.commit()
        except Exception:
            db.rollback()
            db.close()
            raise
        return db

    def create_album(
        self,
        artist,
        title,
        *,
        genre=None,
        format=None,
        format_description=None,
        description=None,
        created_by=None,
    ):
        """Create or select an album without requesting an image.

        Existing pairs fill missing genre/format metadata from supplied values,
        retaining their original populated metadata and attribution.
        Legacy image IDs are reserved so they cannot identify unrelated albums.
        """
        if not normalize(artist) or not normalize(title):
            raise ValueError('Artist and album title must not be empty')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            legacy = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'music_image_ids'"
            ).fetchone()
            floor = db.execute('SELECT COALESCE(MAX(id), 0) FROM albums').fetchone()[0]
            if legacy:
                floor = max(
                    floor,
                    db.execute(
                        'SELECT COALESCE(MAX(id), 0) FROM music_image_ids'
                    ).fetchone()[0],
                )
            sequence = db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'albums'"
            ).fetchone()
            floor = max(floor, sequence[0] if sequence else 0)
            db.execute(
                '''INSERT INTO artists (name, normalized_name, created_by)
                VALUES (?, ?, ?) ON CONFLICT(normalized_name) DO NOTHING''',
                (artist, normalize(artist), created_by),
            )
            artist_id = db.execute(
                'SELECT id FROM artists WHERE normalized_name = ?', (normalize(artist),)
            ).fetchone()['id']
            db.execute(
                '''INSERT INTO albums
                (id, artist_id, title, normalized_title, genre, format,
                 format_description, description, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artist_id, normalized_title) DO UPDATE SET
                    genre = COALESCE(albums.genre, excluded.genre),
                    format = COALESCE(albums.format, excluded.format),
                    format_description = COALESCE(
                        albums.format_description, excluded.format_description
                    )''',
                (
                    floor + 1,
                    artist_id,
                    title,
                    normalize(title),
                    genre,
                    format,
                    format_description,
                    description,
                    created_by,
                ),
            )
            album_id = db.execute(
                'SELECT id FROM albums WHERE artist_id = ? AND normalized_title = ?',
                (artist_id, normalize(title)),
            ).fetchone()['id']
        return self.get_album(album_id)

    def get_album(self, album_id):
        with closing(self.connect()) as db:
            row = db.execute(
                '''SELECT albums.*, artists.name AS artist_name FROM albums
                JOIN artists ON artists.id = albums.artist_id WHERE albums.id = ?''',
                (album_id,),
            ).fetchone()
            if row is None:
                raise LookupError('Unknown album ID')
            album = dict(row)
            for legacy_column in ('cache_key', 'image_created_by', 'image_created_at'):
                album.pop(legacy_column, None)
            album['images'] = [
                dict(image)
                for image in db.execute(
                    """SELECT cache_key
                FROM album_images WHERE album_id = ? ORDER BY cache_key""",
                    (album_id,),
                )
            ]
        return album

    def record_image(self, album_id, cache_key):
        """Link an image to one album; attribution belongs to the image cache.

        Cache keys are logical references: the independently managed image cache
        need not exist when creating or reading a music library.
        """
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute(
                'SELECT album_id FROM album_images WHERE cache_key = ?',
                (cache_key,),
            ).fetchone()
            if owner is not None and owner['album_id'] != album_id:
                raise sqlite3.IntegrityError(
                    f'Image is already linked to album #{owner["album_id"]}'
                )
            db.execute(
                """INSERT INTO album_images (album_id, cache_key)
                VALUES (?, ?) ON CONFLICT(album_id, cache_key) DO NOTHING""",
                (album_id, cache_key),
            )


def album_prompt(album):
    prompt = (
        f"an album cover for the band {album['artist_name']}. "
        f"the name of the album is {album['title']}."
    )
    edition = ', '.join(
        value for value in (album['format'], album['format_description']) if value
    )
    if edition:
        prompt += f" this is the {edition} edition."
    if album['genre']:
        prompt += (
            f" the genre of music is {album['genre']}, "
            "but nowhere should the genre be mentioned."
        )
    if album['description']:
        prompt += f" {album['description']}"
    return prompt


def generate_album_image(library, cache, album_id, nick='', channel=''):
    """Generate/cache an existing album's image; failed requests keep the album."""
    album = library.get_album(album_id)
    prompt = album_prompt(album)
    url = cache.get(prompt, nick, channel)
    library.record_image(album_id, cache.cache_key(prompt))
    return url
