"""Persistent music library, independent of image generation and IRC commands."""

import json
import logging
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from .styles import ALBUM_ART_STYLES


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


@dataclass(frozen=True)
class GenerationInputs:
    """One artwork interpretation of a canonical release; never writes storage.

    ``album_properties`` is consumed directly by the shared ``album_prompt``
    builder. Each variation persists source identity and creative properties.
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
    style: str = ''

    creative_fields = (
        'format',
        'format_description',
        'genre',
        'artist_genre',
        'description',
        'artist_description',
        'style',
    )

    @classmethod
    def from_source(cls, album, image):
        from .images import generation_metadata

        saved = generation_metadata(image).get('generation_inputs', {}) if image else {}
        if not isinstance(saved, dict):
            saved = {}
        properties = {
            field: saved.get(field, album.get(field)) or ''
            for field in cls.creative_fields
        }
        if any(
            not isinstance(value, str) or len(value) > 10000 or '\x00' in value
            for value in properties.values()
        ):
            properties = {
                field: album.get(field) or '' for field in cls.creative_fields
            }
        if properties['style'] not in ALBUM_ART_STYLES:
            properties['style'] = ''
        return cls(
            album_id=album['id'],
            artist_id=album['artist_id'],
            source_image_id=image['id'] if image else 0,
            artist_name=album['artist_name'],
            title=album['title'],
            **properties,
        )

    def prepare(self, values, choices):
        """Validate a complete draft, allowing configured or canonical choices."""
        values = dict(values)
        values.setdefault('style', '')
        if set(values) != set(self.creative_fields):
            raise ValueError('Submit all creative fields only.')
        if not isinstance(values['style'], str) or (
            values['style'] != '' and values['style'] not in ALBUM_ART_STYLES
        ):
            raise ValueError('Invalid choice for style.')
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


_ALBUM_ARTWORK_FIELDS = ('format', 'format_description', 'genre', 'description')
_ARTWORK_DETAIL_LABELS = {
    'format': 'format',
    'format_description': 'format description',
    'genre': 'album genre',
    'description': 'album description',
    'artist_genre': 'band genre',
    'artist_description': 'band description',
}


def artwork_detail_context(field):
    return (
        f' Do not literally add the {_ARTWORK_DETAIL_LABELS[field]} '
        'as text to the image.'
    )


def album_prompt(album, *, database=None, rule_result=None):
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
        prompt += f" the genre of music is {album['genre']}."
    if album['description']:
        prompt += f" {album['description']}"
    for field in _ALBUM_ARTWORK_FIELDS:
        if album.get(field):
            prompt += artwork_detail_context(field)
    from .prompt_rules import clarifications

    result = (
        rule_result
        if rule_result is not None
        else clarifications(album['artist_name'], album['title'], database)
    )
    return prompt + result[1]


def generate_album_image(
    library,
    cache,
    album_id,
    nick='',
    channel='',
    *,
    include_metadata=True,
    snapshot=None,
    exact_prompt=None,
):
    """Generate/cache an existing album's image; failed requests keep the album."""
    album = snapshot if snapshot is not None else library.get_album(album_id)
    if not include_metadata:
        album = dict(
            album, genre=None, description=None, format=None, format_description=None
        )
    prompt = (
        exact_prompt
        if exact_prompt is not None
        else album_prompt(album, database=library.database)
    )
    try:
        # Snapshot only properties used by this initial prompt. Band annotations
        # are supported by variations, but not included in initial generation.
        # Existing durable IRC snapshots omit artist identity. Resolve only that
        # stable ID; names, creative properties and prompt stay frozen at enqueue.
        artist_id = (
            album['artist_id']
            if 'artist_id' in album
            else library.get_album(album_id)['artist_id']
        )
        inputs = dict(
            album_id=album['id'],
            artist_id=artist_id,
            source_image_id=None,
            artist_name=album['artist_name'],
            title=album['title'],
            **{
                field: album.get(field) or ''
                for field in GenerationInputs.creative_fields
            },
        )
        inputs.update(artist_genre='', artist_description='')
        url = cache.get(prompt, nick, channel, inputs=inputs)
    except Exception as exc:
        try:
            library.record_image_failure(album_id, prompt, nick, channel, exc)
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).error('Could not store album image failure')
        raise
    library.record_image(album_id, cache.cache_key(prompt))
    return url


def generate_album_new(
    library, cache, inputs, nick='', channel='', *, exact_prompt=None
):
    """Create standalone artwork with a unique request identity, never a cache hit."""
    from dataclasses import replace

    inputs = replace(inputs, source_image_id=0)
    prompt = (
        exact_prompt
        if exact_prompt is not None
        else artwork_prompt(inputs, database=library.database)
    )
    try:
        return cache.create_image(prompt, nick, channel, inputs=asdict(inputs))
    except Exception as exc:
        library.record_image_failure(inputs.album_id, prompt, nick, channel, exc)
        raise


def artwork_prompt(inputs, *, database=None, rule_result=None):
    prompt = album_prompt(
        inputs.album_properties(), database=database, rule_result=rule_result
    )
    for field, label in (
        ('artist_genre', 'Band genre'),
        ('artist_description', 'Band description'),
    ):
        if getattr(inputs, field):
            prompt += (
                f' {label}: {json.dumps(getattr(inputs, field), ensure_ascii=False)}.'
            )
            prompt += artwork_detail_context(field)
    if inputs.style:
        style = ALBUM_ART_STYLES[inputs.style]
        prompt += (
            f" Artwork style: {style['name']}. {style['visual_definition']}"
            f" Signature elements: {', '.join(style['signature_elements'])}."
            f" Avoid: {', '.join(style['avoid'])}."
            ' Use this style as visual guidance; do not add its name as text.'
        )
    return prompt


def generate_album_variation(
    library, cache, source_image_id, inputs, nick='', channel='', *, exact_prompt=None
):
    """Generate an immutable child through the configured album image backend."""
    return continue_album_image(
        library,
        cache,
        source_image_id,
        inputs,
        nick,
        channel,
        variation=True,
        exact_prompt=exact_prompt,
    )


def continue_album_image(
    library,
    cache,
    source_image_id,
    inputs,
    nick='',
    channel='',
    *,
    variation=False,
    exact_prompt=None,
):
    """F13's backend contract: continue a selected image using an F12 draft.

    Returns the new immutable image record, including its numeric ID. Album
    properties are not updated. Persistence of the child and link is atomic.
    """
    from .images import ImageError

    if (
        not isinstance(inputs, GenerationInputs)
        or inputs.source_image_id != source_image_id
    ):
        raise ImageError(
            'Continuation requires structured inputs for the source image.'
        )
    try:
        album, source = cache.get_album_page(inputs.album_id, source_image_id)
    except LookupError:
        raise ImageError('Unknown source image or album association.') from None
    except (sqlite3.Error, OSError):
        raise ImageError('Could not read the source; check album storage.') from None
    canonical = GenerationInputs.from_source(album, source)
    if any(
        getattr(inputs, field) != getattr(canonical, field)
        for field in (
            'album_id',
            'artist_id',
            'artist_name',
            'title',
            'source_image_id',
        )
    ):
        raise ImageError('Continuation cannot change the release or source identity.')
    try:
        canonical.prepare(
            {field: getattr(inputs, field) for field in inputs.creative_fields}, {}
        )
    except ValueError as exc:
        raise ImageError(str(exc)) from None
    prompt = (
        exact_prompt
        if exact_prompt is not None
        else (
            'Continue the source artwork with this release interpretation: '
            + artwork_prompt(inputs, database=library.database)
        )
    )
    try:
        operation = cache.vary_image if variation else cache.continue_image
        return operation(source_image_id, prompt, nick, channel, inputs=asdict(inputs))
    except Exception as exc:
        try:
            library.record_image_failure(inputs.album_id, prompt, nick, channel, exc)
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).error('Could not store album image failure')
        raise
