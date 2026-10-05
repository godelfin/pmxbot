"""Verify and retire legacy IDs: python -m pmxbot.retire_music_image_ids DB."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def retire(database, *, apply=False, backup=None):
    """Preview read-only, or back up and drop the fully migrated legacy table.

    Stop the bot before applying. Existing album IDs and image records are retained.
    """
    database = Path(database).expanduser().resolve(strict=True)
    mode = 'rw' if apply else 'ro'
    with closing(sqlite3.connect(database.as_uri() + '?mode=' + mode, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        try:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            report = {'legacy_rows': 0, 'unmigrated': [], 'dropped': False}
            if 'music_image_ids' not in tables:
                db.rollback()
                return dict(report, already_retired=True)
            if not {'albums', 'artists', 'album_images', 'image_cache'} <= tables:
                raise ValueError(
                    'Music library and image cache must exist before retiring legacy IDs'
                )
            rows = db.execute('''SELECT legacy.id FROM music_image_ids AS legacy
                WHERE NOT EXISTS (
                    SELECT 1 FROM album_images AS links
                    JOIN albums ON albums.id = links.album_id
                    JOIN artists ON artists.id = albums.artist_id
                    JOIN image_cache AS cache ON cache.cache_key = links.cache_key
                    WHERE links.cache_key = legacy.cache_key)
                ORDER BY legacy.id''').fetchall()
            report['legacy_rows'] = db.execute(
                'SELECT COUNT(*) FROM music_image_ids'
            ).fetchone()[0]
            report['unmigrated'] = [row[0] for row in rows]
            if not apply:
                db.rollback()
                return report
            if rows:
                raise ValueError(
                    'Unmigrated legacy IDs: '
                    + ', '.join(map(str, report['unmigrated']))
                )
            if backup is None:
                raise ValueError('--backup is required with --apply')
            destination = Path(backup).expanduser().resolve()
            with destination.open('xb'):
                pass
            # A separate read connection can snapshot while this transaction
            # holds the writer lock, preventing links from changing after validation.
            with closing(
                sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            ) as source:
                with closing(sqlite3.connect(str(destination))) as snapshot:
                    source.backup(snapshot)
            db.execute('DROP TABLE music_image_ids')
            db.commit()
            return dict(report, dropped=True, backup=str(destination))
        except Exception:
            db.rollback()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    parser.add_argument(
        '--apply', action='store_true', help='Drop the table; stop the bot first'
    )
    parser.add_argument('--backup', help='New backup file, required with --apply')
    args = parser.parse_args()
    try:
        report = retire(args.database, apply=args.apply, backup=args.backup)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Cleanup failed: {exc}\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
