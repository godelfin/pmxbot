"""Persistent music library, independent of image generation and IRC commands."""

import json
import logging
import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


@dataclass(frozen=True)
class GenerationInputs:
    """One artwork interpretation of a canonical release; never writes storage.

    ``album_properties`` is consumed directly by the shared ``album_prompt``
    builder. Source identity and creative properties can be persisted by F13.
    """

    album_id: int
    artist_id: int
    source_image_id: int
    artist_name: str
    title: str
    format: str
    format_description: str
    genre: str
    artist_genre: str
    description: str
    artist_description: str

    creative_fields = (
        'format',
        'format_description',
        'genre',
        'artist_genre',
        'description',
        'artist_description',
    )

    @classmethod
    def from_source(cls, album, image):
        return cls(
            album_id=album['id'],
            artist_id=album['artist_id'],
            source_image_id=image['id'],
            artist_name=album['artist_name'],
            title=album['title'],
            **{field: album.get(field) or '' for field in cls.creative_fields},
        )

    def prepare(self, values, choices):
        """Validate a complete draft, allowing configured or canonical choices."""
        if set(values) != set(self.creative_fields):
            raise ValueError('Submit all six creative fields only.')
        for field, value in values.items():
            if not isinstance(value, str) or len(value) > 10000 or '\x00' in value:
                raise ValueError(
                    'Creative fields must be text of at most 10000 characters.'
                )
            if field in choices and value not in (
                '',
                getattr(self, field),
                *choices[field],
            ):
                raise ValueError(f'Invalid choice for {field}.')
        return type(self)(**dict(asdict(self), **values))

    def album_properties(self):
        return dict(asdict(self), id=self.album_id)


class MusicLibrary:
    def __init__(self, database):
        self.database = Path(database)

    def connect(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.database), timeout=20)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys = ON')
            db.execute(
                '''CREATE TABLE IF NOT EXISTS artists (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE, genre TEXT, description TEXT,
                created_by TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )'''
            )
            db.execute(
                '''CREATE TABLE IF NOT EXISTS albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist_id INTEGER NOT NULL REFERENCES artists(id),
                title TEXT NOT NULL, normalized_title TEXT NOT NULL,
                genre TEXT, format TEXT, format_description TEXT, description TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (artist_id, normalized_title)
            )'''
            )
            db.execute('BEGIN IMMEDIATE')
            db.execute(
                """CREATE TABLE IF NOT EXISTS album_images (
                album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                cache_key TEXT NOT NULL,
                PRIMARY KEY (album_id, cache_key)
            )"""
            )
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS album_images_unique_cache_key '
                'ON album_images(cache_key)'
            )
            db.execute(
                '''CREATE TABLE IF NOT EXISTS album_image_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                prompt TEXT NOT NULL, requested_by TEXT, channel TEXT,
                error_type TEXT NOT NULL, error TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )'''
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
        """
        if not normalize(artist) or not normalize(title):
            raise ValueError('Artist and album title must not be empty')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            floor = db.execute('SELECT COALESCE(MAX(id), 0) FROM albums').fetchone()[0]
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

    def record_image_failure(self, album_id, prompt, nick, channel, error):
        """Keep failed requests, storing only error messages safe for display."""
        from .images import ImageError

        message = (
            str(error)
            if isinstance(error, ImageError)
            else 'Image request failed; check the bot storage and configuration.'
        )
        with closing(self.connect()) as db, db:
            db.execute(
                '''INSERT INTO album_image_failures
                (album_id, prompt, requested_by, channel, error_type, error)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (album_id, prompt, nick, str(channel), type(error).__name__, message),
            )


# Match complete words, including hyphenated names, without matching "bass" or
# "classic". These are contextual hints, not an API moderation blocklist.
_TITLE_CONTEXTS = (
    (
        r"ass|tits?|penis|cock|dick|pussy|cunt|boobs?|breasts?|vagina|genitals?",
        "Keep the artwork non-explicit, with no nudity or sexual anatomy.",
    ),
    (
        r"anal|sex|sexual|sexy|fuck|fucking|blowjobs?|handjobs?|gangbangs?|orgy|"
        r"orgies|porn|cum|semen|masturbation|incest|bestiality|goon",
        "Do not depict sexual acts or explicit sexual content.",
    ),
    (
        r"rape|raped|raping|molest|molestation|assault",
        "Do not depict sexual violence or abuse.",
    ),
    (
        r"fag|fags|fagged|faggot|faggots|holocaust|nazi|nazis",
        "Do not depict hateful imagery, extremist symbols, or atrocities.",
    ),
)


def album_title_context(album):
    """Clarify ambiguous names only for the relevant title contexts."""
    names = normalize(f"{album['artist_name']} {album['title']}")
    instructions = [
        instruction
        for words, instruction in _TITLE_CONTEXTS
        if re.search(rf"\b(?:{words})\b", names)
    ]
    if not instructions:
        return ''
    return (
        ' The quoted band and album names are text labels for a music release. '
        + ' '.join(instructions)
    )


def album_prompt(album):
    prompt = (
        f"an album cover for the band {json.dumps(album['artist_name'], ensure_ascii=False)}. "
        f"the name of the album is {json.dumps(album['title'], ensure_ascii=False)}."
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
    if re.search(r'\btortas?\b', normalize(album['title'])):
        prompt += ' Torta is slang for "thicc Latina".'
    if re.search(r'\btortas?\b', normalize(album['artist_name'])):
        prompt += ' Torta is slang for "thicc Latina" and describes the band members.'
    if any(
        re.search(r'\bmedve\b', normalize(album[field]))
        for field in ('artist_name', 'title')
    ):
        prompt += (
            " Medve isn't a bear, he's a middle aged white man with glasses "
            "and a beard and a lopsided grin."
        )
    return prompt + album_title_context(album)


def generate_album_image(
    library, cache, album_id, nick='', channel='', *, include_metadata=True
):
    """Generate/cache an existing album's image; failed requests keep the album."""
    album = library.get_album(album_id)
    if not include_metadata:
        album = dict(
            album, genre=None, description=None, format=None, format_description=None
        )
    prompt = album_prompt(album)
    try:
        url = cache.get(prompt, nick, channel)
    except Exception as exc:
        try:
            library.record_image_failure(album_id, prompt, nick, channel, exc)
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).error('Could not store album image failure')
        raise
    library.record_image(album_id, cache.cache_key(prompt))
    return url
