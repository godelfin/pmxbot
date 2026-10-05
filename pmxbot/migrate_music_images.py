"""Import legacy music image IDs: python -m pmxbot.migrate_music_images DB."""

import argparse
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .music import MusicLibrary, normalize

PREFIX = 'an album cover for the band '
SEPARATOR = '. the name of the album is '
EDITION = '. this is the '
GENRE = '. the genre of music is '
GENRE_SUFFIX = ', but nowhere should the genre be mentioned'


def parse_prompt(prompt):
    """Accept historical generated prompts; reject ambiguous delimiters."""
    if not prompt.startswith(PREFIX) or prompt.count(SEPARATOR) != 1:
        raise ValueError('unrecognized or ambiguous artist/album prompt')
    artist, rest = prompt[len(PREFIX) :].split(SEPARATOR)
    metadata = {'genre': None, 'format': None, 'format_description': None}
    if EDITION in rest:
        if rest.count(EDITION) != 1 or rest.count(GENRE) != 1:
            raise ValueError('ambiguous edition or genre')
        title, rest = rest.split(EDITION)
        edition, genre = rest.split(GENRE)
        if not edition.endswith(' edition') or not genre.endswith('.'):
            raise ValueError('unrecognized edition or genre')
        parts = edition[: -len(' edition')].split(', ')
        if len(parts) != 2 or not all(parts):
            raise ValueError('ambiguous format description')
        genre = genre[:-1]
        genre = genre.removesuffix(GENRE_SUFFIX)
        metadata.update(format=parts[0], format_description=parts[1], genre=genre)
    else:
        if GENRE in rest:
            raise ValueError('unrecognized prompt variant')
        title = rest.removesuffix('.')
    if not normalize(artist) or not normalize(title):
        raise ValueError('empty artist or title')
    return dict(artist=artist, title=title, **metadata)


def import_rows(db):
    """Import in one transaction, preserving existing library rows and links."""
    report = {'linked': 0, 'already_linked': 0, 'skipped': [], 'mapping': []}
    with db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('''SELECT legacy.id AS legacy_id, legacy.cache_key,
            cache.prompt, cache.requested_by, cache.created_at
            FROM music_image_ids AS legacy LEFT JOIN image_cache AS cache
            ON cache.cache_key = legacy.cache_key ORDER BY legacy.id''').fetchall()
        for row in rows:
            try:
                if row['prompt'] is None:
                    raise ValueError('missing image_cache row or prompt')
                values = parse_prompt(row['prompt'])
            except ValueError as exc:
                report['skipped'].append(
                    {'legacy_id': row['legacy_id'], 'reason': str(exc)}
                )
                continue
            db.execute(
                '''INSERT INTO artists
                (name, normalized_name, created_by, created_at)
                VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                ON CONFLICT(normalized_name) DO NOTHING''',
                (
                    values['artist'],
                    normalize(values['artist']),
                    row['requested_by'],
                    row['created_at'],
                ),
            )
            artist_id = db.execute(
                'SELECT id FROM artists WHERE normalized_name = ?',
                (normalize(values['artist']),),
            ).fetchone()[0]
            album = db.execute(
                'SELECT id FROM albums WHERE artist_id = ? AND normalized_title = ?',
                (artist_id, normalize(values['title'])),
            ).fetchone()
            if album:
                album_id = album[0]
            else:
                # Preserve a legacy ID when free. If occupied, allocate above
                # both ID spaces so a later legacy row cannot collide with it.
                album_id = row['legacy_id']
                if db.execute(
                    'SELECT 1 FROM albums WHERE id = ?', (album_id,)
                ).fetchone():
                    album_id = db.execute('''SELECT MAX(value) + 1 FROM (
                        SELECT COALESCE(MAX(id), 0) AS value FROM albums
                        UNION ALL SELECT COALESCE(MAX(id), 0) FROM music_image_ids
                        UNION ALL SELECT COALESCE(MAX(seq), 0) FROM sqlite_sequence
                        WHERE name = 'albums')''').fetchone()[0]
                db.execute(
                    '''INSERT INTO albums
                    (id, artist_id, title, normalized_title, genre, format,
                     format_description, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))''',
                    (
                        album_id,
                        artist_id,
                        values['title'],
                        normalize(values['title']),
                        values['genre'],
                        values['format'],
                        values['format_description'],
                        row['requested_by'],
                        row['created_at'],
                    ),
                )
            inserted = db.execute(
                '''INSERT INTO album_images
                (album_id, cache_key)
                VALUES (?, ?) ON CONFLICT(album_id, cache_key) DO NOTHING''',
                (album_id, row['cache_key']),
            )
            report['linked' if inserted.rowcount else 'already_linked'] += 1
            report['mapping'].append(
                {
                    'legacy_id': row['legacy_id'],
                    'album_id': album_id,
                    'cache_key': row['cache_key'],
                }
            )
    return report


def migrate(database, *, apply=False, backup=None):
    """Dry-run on a SQLite snapshot, or back up and import the real database.

    Stop the bot before applying. Neither cached files nor legacy rows are removed.
    """
    database = Path(database).expanduser().resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
        tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {'music_image_ids', 'image_cache'} <= tables:
            raise ValueError('Database requires music_image_ids and image_cache tables')
        with tempfile.TemporaryDirectory(prefix='pmxbot-music-migration-') as directory:
            if apply:
                if backup is None:
                    raise ValueError('--backup is required with --apply')
                destination = Path(backup).expanduser().resolve()
                # Exclusive creation prevents overwriting the DB or an earlier backup.
                with destination.open('xb'):
                    pass
            else:
                destination = Path(directory) / 'preview.sqlite'
            with closing(sqlite3.connect(str(destination))) as snapshot:
                source.backup(snapshot)
            target = database if apply else destination
            with closing(MusicLibrary(target).connect()) as db:
                report = import_rows(db)
            return dict(
                report, applied=apply, backup=str(destination) if apply else None
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', help='SQLite database file (not a sqlite: URI)')
    parser.add_argument(
        '--apply', action='store_true', help='Write changes; stop the bot first'
    )
    parser.add_argument('--backup', help='New backup file, required with --apply')
    args = parser.parse_args()
    try:
        report = migrate(args.database, apply=args.apply, backup=args.backup)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Migration failed: {exc}\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
