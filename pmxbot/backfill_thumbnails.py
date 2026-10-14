"""Upload missing thumbnails: python -m pmxbot.backfill_thumbnails --help."""

import argparse
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from .dictlib import ConfigDict
from .images import ImageCache, ImageError


def backfill(cache, *, dry_run=False, source_root=None):
    """Return the failure count; commit each URL only after a successful upload.

    Relative local filenames use source_root (the current directory by default).
    Originals, generation metadata, and original hosted URLs are left intact.
    """
    mode = 'ro' if dry_run else 'rw'
    with closing(
        sqlite3.connect(cache.database.as_uri() + f'?mode={mode}', uri=True, timeout=20)
    ) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT id, cache_key, local_filename FROM image_cache
            WHERE local_filename != '' AND thumb_url IS NULL ORDER BY id"""
        ).fetchall()
        print(f'{len(rows)} images need thumbnails.', flush=True)
        if not rows:
            return 0
        if not dry_run:
            cache.destination(rows[0]['cache_key'])
            for name in ('access_key_id', 'secret_access_key'):
                if not cache.r2[name]:
                    raise ImageError(f'Configure R2_{name.upper()} before uploading.')
        failures = 0
        uploaded = 0
        root = Path(source_root or Path.cwd()).expanduser().resolve()
        # Use temporary thumbnails so the backfill cannot replace cached files.
        with tempfile.TemporaryDirectory(prefix='pmxbot-thumbnails-') as temporary:
            original_directory = cache.directory
            cache.directory = Path(temporary)
            try:
                for row in rows:
                    try:
                        key = row['cache_key']
                        if (
                            not key
                            or key in ('.', '..')
                            or any(char in key for char in ('/', '\\', '\x00'))
                        ):
                            raise ImageError(
                                'Invalid cache key for a thumbnail filename.'
                            )
                        filename = Path(row['local_filename']).expanduser()
                        if not filename.is_absolute():
                            filename = root / filename
                        if not filename.is_file():
                            raise ImageError(f'Saved original is missing: {filename}')
                        if dry_run:
                            print(
                                f"Would upload image #{row['id']}: {filename}",
                                flush=True,
                            )
                            continue
                        destination = dict(
                            cache.destination(key), key=f'{key}_thumb.jpg'
                        )
                        thumbnail = cache.save_thumbnail(filename, key)
                        cache.upload(thumbnail, destination)
                        url = (
                            cache.r2['public_url'].rstrip('/')
                            + '/'
                            + quote(destination['key'])
                        )
                        with db:
                            changed = db.execute(
                                """UPDATE image_cache SET thumb_url = ?
                                WHERE id = ? AND cache_key = ? AND local_filename = ?
                                AND thumb_url IS NULL""",
                                (url, row['id'], key, row['local_filename']),
                            ).rowcount
                        uploaded += changed
                        print(
                            f"Image #{row['id']}: "
                            + (
                                url
                                if changed
                                else 'row changed; skipped database update'
                            ),
                            flush=True,
                        )
                    except (ImageError, OSError, sqlite3.Error) as exc:
                        failures += 1
                        print(f"Image #{row['id']} failed: {exc}", flush=True)
            finally:
                cache.directory = original_directory
        print(f'{uploaded} updated; {failures} failed.', flush=True)
        return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=ConfigDict.from_yaml, default={})
    parser.add_argument('--database', type=Path, help='Override database from config.')
    parser.add_argument(
        '--source-root',
        type=Path,
        help='Base for relative local_filename paths (default: cwd).',
    )
    parser.add_argument(
        '--dry-run', action='store_true', help='List rows without uploading or writing.'
    )
    args = parser.parse_args(argv)
    # Only storage helpers are needed; no OpenAI configuration or calls.
    config = dict(args.config, image_api='images')
    if args.database is not None:
        config['database'] = 'sqlite:' + str(args.database.expanduser().resolve())
    try:
        failures = backfill(
            ImageCache(config), dry_run=args.dry_run, source_root=args.source_root
        )
    except (ImageError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'{exc}\n')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
