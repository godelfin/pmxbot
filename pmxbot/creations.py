"""Manual releases staged in the existing durable generation queue."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace

from . import albums
from .images import ImageError
from .music import GenerationInputs, MusicLibrary, normalize
from .users import UserStore
from .urls import public_application_url


def generation_choices():
    genres = sorted(set(albums.genres).union(*albums.genres.values()))
    return dict(
        format=sorted(albums.formats),
        format_description=sorted(albums.format_desc),
        genre=genres,
        artist_genre=genres,
    )


def authorized(config, user_id):
    with closing(UserStore(config['database'])) as users:
        user = users.get_by_id(user_id)
    if not user.has_permission('generate_images'):
        raise ImageError('Image generation permission is required.')
    return user


def prepare(values):
    values = dict(values)
    identity = {}
    errors = {}
    for field in ('artist_name', 'title'):
        value = values.pop(field, '')
        if (
            not isinstance(value, str)
            or not normalize(value)
            or len(value) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            errors[field] = 'Enter 1–255 characters without control characters.'
        else:
            identity[field] = ' '.join(value.split())
    if errors:
        return None, errors
    draft = GenerationInputs(
        0, 0, 0, **identity, **dict.fromkeys(GenerationInputs.creative_fields, '')
    )
    try:
        return draft.prepare(values, generation_choices()), {}
    except ValueError as exc:
        # Keep the common validator authoritative while locating field errors.
        blank = dict.fromkeys(GenerationInputs.creative_fields, '')
        for field in GenerationInputs.creative_fields:
            if field not in values:
                continue
            try:
                draft.prepare(
                    dict(blank, **{field: values[field]}), generation_choices()
                )
            except ValueError as field_error:
                errors[field] = str(field_error)
        return None, errors or {'generation': str(exc)}


def release_key(inputs):
    return json.dumps([normalize(inputs.artist_name), normalize(inputs.title)])


def conflicting_album(db, inputs):
    return db.execute(
        '''SELECT albums.id FROM albums JOIN artists ON artists.id = albums.artist_id
        WHERE artists.normalized_name = ? AND albums.normalized_title = ?''',
        (normalize(inputs.artist_name), normalize(inputs.title)),
    ).fetchone()


def generate(job, cache, config):
    """Persist a recoverable image first, then publish the release atomically.

    The request's stable cache key reuses saved artwork after hosting or database
    failure. No artist or album exists while the provider is running.
    """
    if not config.get('images_enabled', False):
        raise ImageError('Image generation is disabled.')
    authorized(config, job['user_id'])
    inputs = GenerationInputs(**json.loads(job['inputs_json']))
    from .jobs import job_prompt

    prompt = job_prompt(job, inputs)
    key = 'album-creation-' + job['id']
    cache.stage_album_image(
        prompt, key, asdict(inputs), job['requested_by'], job['channel']
    )
    library = MusicLibrary(cache.database)
    with closing(library.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if conflicting_album(db, inputs):
            raise ValueError('This artist already has an album with that title.')
        db.execute(
            '''INSERT INTO artists (name, normalized_name, created_by)
            VALUES (?, ?, ?) ON CONFLICT(normalized_name) DO NOTHING''',
            (inputs.artist_name, normalize(inputs.artist_name), job['requested_by']),
        )
        artist = db.execute(
            'SELECT * FROM artists WHERE normalized_name = ?',
            (normalize(inputs.artist_name),),
        ).fetchone()
        cursor = db.execute(
            '''INSERT INTO albums (artist_id, title, normalized_title, genre, format,
            format_description, description, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                artist['id'],
                inputs.title,
                normalize(inputs.title),
                inputs.genre,
                inputs.format,
                inputs.format_description,
                inputs.description,
                job['requested_by'],
            ),
        )
        album_id = cursor.lastrowid
        db.execute('INSERT INTO album_images VALUES (?, ?)', (album_id, key))
        image = db.execute(
            'SELECT * FROM image_cache WHERE cache_key = ?', (key,)
        ).fetchone()
        metadata = json.loads(image['generation_metadata_json'])
        metadata['generation_inputs'] = asdict(
            replace(
                inputs,
                album_id=album_id,
                artist_id=artist['id'],
                artist_name=artist['name'],
            )
        )
        metadata['operation'] = 'create_new'
        db.execute(
            'UPDATE image_cache SET generation_metadata_json = ? WHERE id = ?',
            (json.dumps(metadata), image['id']),
        )
        db.execute(
            '''UPDATE generation_jobs SET album_id = ?, result_image_id = ?,
            status = 'succeeded', completed_at = CURRENT_TIMESTAMP, error = NULL
            WHERE id = ? AND status = 'running' ''',
            (album_id, image['id'], job['id']),
        )
    return dict(image)


def announcement(job, config):
    inputs = GenerationInputs(**json.loads(job['inputs_json']))
    url = public_application_url(config.get('albums_url'), f"albums/{job['album_id']}")
    if not url:
        raise ValueError('Configure albums_url for album announcements.')

    def clean(value):
        return (
            ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127)
            .encode('utf-8')[:80]
            .decode('utf-8', errors='ignore')
        )

    return (
        f"New album created by {clean(job['requested_by'])}: "
        f"{clean(inputs.artist_name)} — {clean(inputs.title)}\n{url}"
    )
