"""OpenAI image generation with a persistent local cache and Cloudflare R2 hosting."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import queue
import re
import socket
import sqlite3
import tempfile
import threading
import unicodedata
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from urllib.parse import quote, urlsplit

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import pmxbot

from . import quotes
from .core import SwitchChannel, command, execdelay
from .music import MusicLibrary, generate_album_image

log = logging.getLogger(__name__)
_busy = threading.BoundedSemaphore(3)
_results: queue.Queue[tuple[str, str]] = queue.Queue()
# Commands and result delivery run on the bot thread. Keep provider load bounded.
_pending: queue.Queue[dict] = queue.Queue(maxsize=10)


class ImageError(Exception):
    """An error safe to display in IRC (no provider response or credentials)."""


def normalize_prompt(prompt):
    return ' '.join(unicodedata.normalize('NFKC', prompt).split()).casefold()


def openai_error_message(response, secrets=()):
    """Read only allowlisted diagnostics; never stringify an exception or body."""
    fallback = 'OpenAI request failed; please try again later.'

    def safe(value, token=False):
        if not isinstance(value, str) or not value or len(value) > 400:
            return None
        if any(secret and secret in value for secret in secrets):
            return None
        # Reject credentials, dumps, binary payloads and multiline diagnostics.
        if re.search(
            r'authorization|bearer|cookie|api[ _-]?key|secret|password|'
            r'credential|sk-|base64|b64_json|data:image|traceback|'
            r'[A-Za-z0-9+/=]{80,}|[\r\n\x00-\x1f\x7f]|[{}<>]',
            value,
            re.IGNORECASE,
        ):
            return None
        if token and not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value):
            return None
        return value

    details = []
    message = None
    try:
        status = getattr(response, 'status_code', None)
        if type(status) is int and 400 <= status <= 599:
            details.append(f'HTTP {status}')
        try:
            body = response.json()
        except (ValueError, AttributeError):
            body = None
        error = body.get('error') if isinstance(body, dict) else None
        if isinstance(error, dict):
            for name in ('code', 'type'):
                value = safe(error.get(name), token=True)
                if value:
                    details.append(f'{name}={value}')
            message = safe(error.get('message'))
        headers = getattr(response, 'headers', {})
        for header, label in (
            ('x-request-id', 'request_id'),
            ('Retry-After', 'retry_after'),
        ):
            value = safe(headers.get(header), token=True)
            if value:
                details.append(f'{label}={value}')
    except Exception:
        # Diagnostic extraction must never replace the original request failure.
        pass
    if not details and not message:
        return fallback
    suffix = f" ({', '.join(details)})" if details else ''
    return f'OpenAI image request failed{suffix}' + (f': {message}' if message else '.')


def post_json(url, provider, **kwargs):
    response = None
    try:
        response = requests.post(url, **kwargs)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        message = f'{provider} request failed; please try again later.'
        if provider == 'OpenAI':
            # requests.Response is falsey on HTTP errors; test explicitly for None.
            error_response = getattr(exc, 'response', None)
            if error_response is not None:
                response = error_response
            authorization = kwargs.get('headers', {}).get('Authorization', '')
            message = openai_error_message(
                response, secrets=(authorization, authorization.split(' ', 1)[-1])
            )
            # exc_info would also print arbitrary exception text and chained causes.
            log.warning('%s', message)
        raise ImageError(message) from exc


def initialize_image_records(db):
    """Initialize image storage and add immutable ancestry to existing records."""
    db.execute(
        '''CREATE TABLE IF NOT EXISTS image_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT NOT NULL UNIQUE, prompt TEXT NOT NULL,
            normalized_prompt TEXT NOT NULL, settings_json TEXT NOT NULL,
            local_filename TEXT NOT NULL, hosted_url TEXT,
            host TEXT NOT NULL DEFAULT 'r2', host_metadata_json TEXT,
            generation_metadata_json TEXT NOT NULL, requested_by TEXT,
            channel TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            uploaded_at TEXT, last_accessed_at TEXT,
            hit_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            parent_image_id INTEGER REFERENCES image_cache(id)
        )'''
    )

    # CREATE above obtains the schema write lock for fresh databases. Serialize
    # migration checks too, including connections in autocommit mode.
    db.execute('SAVEPOINT image_ancestry')
    try:
        db.execute('UPDATE image_cache SET id = id WHERE 0')
        columns = {row[1] for row in db.execute('PRAGMA table_info(image_cache)')}
        if 'parent_image_id' not in columns:
            db.execute(
                'ALTER TABLE image_cache ADD COLUMN '
                'parent_image_id INTEGER REFERENCES image_cache(id)'
            )
        db.execute(
            '''CREATE TRIGGER IF NOT EXISTS image_ancestry_insert
            AFTER INSERT ON image_cache WHEN NEW.parent_image_id IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'Invalid image parent')
                WHERE NOT EXISTS (
                    SELECT 1 FROM image_cache WHERE id = NEW.parent_image_id
                );
                SELECT RAISE(ABORT, 'Image ancestry cycle') WHERE NEW.id IN (
                    WITH RECURSIVE ancestors(id) AS (
                        SELECT NEW.parent_image_id
                        UNION
                        SELECT image_cache.parent_image_id FROM image_cache
                        JOIN ancestors ON image_cache.id = ancestors.id
                        WHERE image_cache.parent_image_id IS NOT NULL
                    ) SELECT id FROM ancestors
                );
            END'''
        )
        db.execute(
            '''CREATE TRIGGER IF NOT EXISTS image_ancestry_replace
            BEFORE INSERT ON image_cache
            WHEN EXISTS (SELECT 1 FROM image_cache WHERE id = NEW.id)
            OR EXISTS (SELECT 1 FROM image_cache WHERE cache_key = NEW.cache_key
                AND parent_image_id IS NOT NEW.parent_image_id)
            BEGIN SELECT RAISE(ABORT, 'Image ID is already persisted'); END'''
        )
        db.execute(
            '''CREATE TRIGGER IF NOT EXISTS image_ancestry_update
            BEFORE UPDATE OF parent_image_id, id ON image_cache
            WHEN NEW.parent_image_id IS NOT OLD.parent_image_id OR NEW.id != OLD.id
            BEGIN SELECT RAISE(ABORT, 'Image ancestry is immutable'); END'''
        )
        db.execute(
            '''CREATE TRIGGER IF NOT EXISTS image_ancestry_delete
            BEFORE DELETE ON image_cache
            WHEN EXISTS (SELECT 1 FROM image_cache WHERE parent_image_id = OLD.id)
            BEGIN SELECT RAISE(ABORT, 'Image has children'); END'''
        )
    except Exception:
        db.execute('ROLLBACK TO image_ancestry')
        raise
    finally:
        db.execute('RELEASE image_ancestry')


class ImageCache:
    def __init__(self, config):
        self.directory = (
            Path(config.get('images_directory', 'images')).expanduser().resolve()
        )
        database_uri = config.get('database', 'sqlite:pmxbot.sqlite')
        parsed = urlsplit(database_uri)
        if parsed.scheme != 'sqlite' and not (
            not parsed.scheme and database_uri.endswith('.sqlite')
        ):
            raise ImageError('Image caching requires a SQLite main bot database.')
        if not parsed.path or parsed.path == ':memory:':
            raise ImageError(
                'Image caching requires a persistent SQLite main bot database.'
            )
        # Match SQLiteStorage's URI path handling; only the connection is separate,
        # because the image worker cannot use the IRC thread's SQLite connection.
        # Python 3.8 on Windows can leave a nonexistent relative path unresolved.
        self.database = Path(parsed.path).absolute().resolve()
        self.settings = {
            'model': config.get('images_model', 'gpt-image-1'),
            'size': config.get('images_size', '1024x1024'),
            'quality': config.get('images_quality', 'low'),
            'output_format': 'png',
        }
        self.openai_key = config.get('openai_api_key') or os.environ.get(
            'OPENAI_API_KEY'
        )
        self.r2 = {
            name: config.get('r2_' + name) or os.environ.get('R2_' + name.upper(), '')
            for name in (
                'endpoint_url',
                'access_key_id',
                'secret_access_key',
                'bucket',
                'public_url',
            )
        }

    def destination(self, key):
        for name in ('endpoint_url', 'bucket', 'public_url'):
            if not self.r2[name]:
                raise ImageError(
                    f'Configure R2_{name.upper()} before generating images.'
                )
        for name in ('endpoint_url', 'public_url'):
            url = self.r2[name]
            parsed = urlsplit(url)
            if (
                parsed.scheme != 'https'
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or any(char.isspace() for char in url)
            ):
                raise ImageError(
                    f'R2_{name.upper()} must be an HTTPS URL without credentials, query, or fragment.'
                )
        return {
            'endpoint_url': self.r2['endpoint_url'].rstrip('/'),
            'bucket': self.r2['bucket'],
            'key': f'{key}.png',
        }

    def cache_key(self, prompt):
        value = dict(self.settings, prompt=normalize_prompt(prompt), version=1)
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.database), timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            initialize_image_records(db)
        except Exception:
            db.close()
            raise
        return db

    def read_connection(self):
        """Open existing storage without schema initialization or filesystem writes."""
        if not self.database.is_file():
            raise LookupError('Unknown image ID')
        db = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def get_image(self, image_id):
        """Return a persisted image record without generating or mutating storage."""
        with closing(self.read_connection()) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            row = (
                db.execute(
                    'SELECT * FROM image_cache WHERE id = ?', (image_id,)
                ).fetchone()
                if 'image_cache' in tables
                else None
            )
            if row is None:
                raise LookupError('Unknown image ID')
            return dict(row)

    def get_album_page(self, album_id, source_image_id=None):
        """Read an album and its selected or newest artwork without modifying storage."""
        with closing(self.read_connection()) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {'albums', 'artists'} <= tables:
                raise LookupError('Unknown album ID')
            row = db.execute(
                '''SELECT albums.*, artists.name AS artist_name,
                artists.genre AS artist_genre,
                artists.description AS artist_description
                FROM albums JOIN artists ON artists.id = albums.artist_id
                WHERE albums.id = ?''',
                (album_id,),
            ).fetchone()
            if row is None:
                raise LookupError('Unknown album ID')
            image = None
            if {'album_images', 'image_cache'} <= tables:
                image = db.execute(
                    '''SELECT image_cache.* FROM image_cache
                    JOIN album_images USING (cache_key) WHERE album_id = ?
                    AND (? IS NULL OR image_cache.id = ?)
                    ORDER BY image_cache.created_at DESC, image_cache.id DESC LIMIT 1''',
                    (album_id, source_image_id, source_image_id),
                ).fetchone()
            if source_image_id is not None and image is None:
                raise LookupError('Unknown source image ID')
            return dict(row), dict(image) if image is not None else None

    def list_albums(self):
        """Read all album titles and bands without initializing storage."""
        if not self.database.is_file():
            return []
        with closing(self.read_connection()) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {'albums', 'artists'} <= tables:
                return []
            return [
                dict(row)
                for row in db.execute(
                    '''SELECT albums.id, albums.title, artists.name AS artist_name
                    FROM albums JOIN artists ON artists.id = albums.artist_id
                    ORDER BY artists.normalized_name, albums.normalized_title,
                    albums.id'''
                )
            ]

    def generation_leaderboard(self, limit=10):
        """Count persisted generations linked to albums, using historical requesters."""
        if not self.database.is_file():
            return []
        with closing(self.read_connection()) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {'albums', 'album_images', 'image_cache'} <= tables:
                return []
            # Cache records are written only after successful generation. Hosting
            # errors and cache hits do not change that generation's attribution.
            # EXISTS also avoids counting duplicate historical album links twice.
            return [
                dict(row)
                for row in db.execute(
                    '''SELECT TRIM(requested_by, ?) AS username, COUNT(*) AS image_count
                    FROM image_cache
                    WHERE TRIM(requested_by, ?) != '' AND EXISTS (
                        SELECT 1 FROM album_images JOIN albums
                        ON albums.id = album_images.album_id
                        WHERE album_images.cache_key = image_cache.cache_key
                    )
                    GROUP BY username
                    ORDER BY image_count DESC, username COLLATE BINARY
                    LIMIT ?''',
                    (' \t\r\n\v\f', ' \t\r\n\v\f', limit),
                )
            ]

    def album_image_failures(self, album_id):
        """Read failure history, including databases predating failure logging."""
        with closing(self.read_connection()) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'album_image_failures'"
            ).fetchone():
                return []
            return [
                dict(row)
                for row in db.execute(
                    '''SELECT * FROM album_image_failures WHERE album_id = ?
                    ORDER BY created_at DESC, id DESC''',
                    (album_id,),
                )
            ]

    def gallery_albums(self, page, page_size=24, sort='date_desc'):
        """Read one page of albums and their latest artwork without writes."""
        order = {
            'band': 'artists.normalized_name, albums.normalized_title, albums.id',
            'album': 'albums.normalized_title, artists.normalized_name, albums.id',
            'date_asc': 'albums.created_at, albums.id',
            'date_desc': 'albums.created_at DESC, albums.id DESC',
            'genre': "NULLIF(TRIM(albums.genre), '') IS NULL, "
            'LOWER(TRIM(albums.genre)), artists.normalized_name, '
            'albums.normalized_title, albums.id',
        }[sort]
        if not self.database.is_file():
            return [], 0
        with closing(self.read_connection()) as db:
            db.execute('BEGIN')
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {'albums', 'artists'} <= tables:
                return [], 0
            total = db.execute('SELECT COUNT(*) FROM albums').fetchone()[0]
            artwork = (
                '''(SELECT image_cache.hosted_url FROM image_cache
                JOIN album_images USING (cache_key)
                WHERE album_images.album_id = albums.id
                ORDER BY image_cache.created_at DESC, image_cache.id DESC LIMIT 1)'''
                if {'album_images', 'image_cache'} <= tables
                else 'NULL'
            )
            rows = db.execute(
                f'''SELECT albums.id, albums.title, albums.genre,
                artists.name AS artist_name,
                {artwork} AS hosted_url
                FROM albums JOIN artists ON artists.id = albums.artist_id
                ORDER BY {order} LIMIT ? OFFSET ?''',
                (page_size, (page - 1) * page_size),
            )
            return [dict(row) for row in rows], total

    def album_neighbors(self, album_id):
        """Return adjacent stored album IDs, skipping gaps in the sequence."""
        with closing(self.read_connection()) as db:
            row = db.execute(
                '''SELECT
                (SELECT MAX(id) FROM albums WHERE id < ?) AS previous,
                (SELECT MIN(id) FROM albums WHERE id > ?) AS next''',
                (album_id, album_id),
            ).fetchone()
            return dict(row)

    def latest_album_image(self, album_id):
        """Return the newest hosted cache entry linked to an existing album."""
        MusicLibrary(self.database).get_album(album_id)
        with closing(self.connect()) as db:
            row = db.execute(
                '''SELECT image_cache.cache_key, hosted_url FROM image_cache
                JOIN album_images USING (cache_key)
                WHERE album_id = ? AND hosted_url IS NOT NULL AND hosted_url != ''
                ORDER BY image_cache.created_at DESC, image_cache.rowid DESC
                LIMIT 1''',
                (album_id,),
            ).fetchone()
            if row is None:
                raise ImageError(f'No cached image for album #{album_id}.')
            db.execute(
                '''UPDATE image_cache SET hit_count = hit_count + 1,
                last_accessed_at = CURRENT_TIMESTAMP WHERE cache_key = ?''',
                (row['cache_key'],),
            )
            return row['hosted_url']

    def get(self, prompt, nick='', channel=''):
        if not normalize_prompt(prompt):
            raise ImageError('Usage: !image <prompt>')
        key = self.cache_key(prompt)
        destination = self.destination(key)
        url = self.r2['public_url'].rstrip('/') + '/' + quote(destination['key'])
        with closing(self.connect()) as db:
            row = db.execute(
                'SELECT * FROM image_cache WHERE cache_key = ?', (key,)
            ).fetchone()
            metadata = json.loads(row['host_metadata_json'] or '{}') if row else {}
            if (
                row
                and row['host'] == 'r2'
                and row['hosted_url']
                and all(
                    metadata.get(name) == value for name, value in destination.items()
                )
            ):
                db.execute(
                    '''UPDATE image_cache SET hit_count = hit_count + 1,
                    hosted_url = ?, last_accessed_at = CURRENT_TIMESTAMP WHERE cache_key = ?''',
                    (url, key),
                )
                return url
            for name in ('access_key_id', 'secret_access_key'):
                if not self.r2[name]:
                    raise ImageError(
                        f'Configure R2_{name.upper()} before generating images.'
                    )
            filename = (
                Path(row['local_filename']) if row else self.directory / f'{key}.png'
            )
            if not row or not filename.is_file():
                if not self.openai_key:
                    raise ImageError(
                        'Configure OPENAI_API_KEY before generating images.'
                    )
                image, metadata = self.generate(prompt)
                filename = self.directory / f'{key}.png'
                self.save(filename, image)
                self.persist_image(db, key, prompt, filename, metadata, nick, channel)
            try:
                metadata = self.upload(filename, destination)
            except ImageError as exc:
                db.execute(
                    'UPDATE image_cache SET last_error = ? WHERE cache_key = ?',
                    (str(exc), key),
                )
                raise ImageError(
                    'Image saved locally, but upload failed. Repeat the prompt to retry.'
                ) from None
            db.execute(
                '''UPDATE image_cache SET hosted_url = ?, host_metadata_json = ?, host = 'r2',
                uploaded_at = CURRENT_TIMESTAMP, last_accessed_at = CURRENT_TIMESTAMP,
                last_error = NULL WHERE cache_key = ?''',
                (url, json.dumps(metadata), key),
            )
            return url

    def persist_image(
        self,
        db,
        key,
        prompt,
        filename,
        metadata,
        nick='',
        channel='',
        parent_image_id=None,
    ):
        """Persist image bytes' metadata; retries retain the original ancestry.

        A supplied parent must already exist. An existing key may only be
        reused with the same parent (or omitted parent for ordinary retries).
        Invalid ancestry raises sqlite3.IntegrityError.
        """
        row = db.execute(
            'SELECT parent_image_id FROM image_cache WHERE cache_key = ?', (key,)
        ).fetchone()
        if row is not None:
            if parent_image_id is not None and row[0] != parent_image_id:
                raise sqlite3.IntegrityError('Image ancestry is immutable')
            parent_image_id = row[0]
        db.execute(
            '''INSERT INTO image_cache
            (cache_key, prompt, normalized_prompt, settings_json,
             local_filename, generation_metadata_json, requested_by, channel, host, parent_image_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'r2', ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                prompt = excluded.prompt,
                normalized_prompt = excluded.normalized_prompt,
                settings_json = excluded.settings_json,
                local_filename = excluded.local_filename,
                generation_metadata_json = excluded.generation_metadata_json,
                hosted_url = NULL, host_metadata_json = NULL,
                uploaded_at = NULL, last_error = NULL, host = 'r2' ''',
            (
                key,
                prompt,
                normalize_prompt(prompt),
                json.dumps(self.settings),
                str(filename),
                json.dumps(metadata),
                nick,
                str(channel),
                parent_image_id,
            ),
        )

    def generate(self, prompt):
        data = post_json(
            'https://api.openai.com/v1/images/generations',
            'OpenAI',
            headers={'Authorization': f'Bearer {self.openai_key}'},
            json=dict(self.settings, prompt=prompt, n=1),
            timeout=(10, 300),
        )
        try:
            item = data['data'][0]
            image = base64.b64decode(item['b64_json'], validate=True)
            if not image.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('Not a PNG')
            metadata = {key: value for key, value in data.items() if key != 'data'}
            metadata['revised_prompt'] = item.get('revised_prompt')
            return image, metadata
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error):
            raise ImageError('OpenAI returned no valid PNG image.') from None

    @staticmethod
    def save(filename, image):
        # Keep partial files out of the cache, even if the process is interrupted.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=filename.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(image)
            temporary.replace(filename)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def upload(self, filename, destination):
        try:
            with closing(
                boto3.client(
                    's3',
                    endpoint_url=destination['endpoint_url'],
                    region_name='auto',
                    aws_access_key_id=self.r2['access_key_id'],
                    aws_secret_access_key=self.r2['secret_access_key'],
                    config=Config(
                        signature_version='s3v4',
                        connect_timeout=10,
                        read_timeout=60,
                        retries={'mode': 'standard', 'total_max_attempts': 3},
                        s3={'addressing_style': 'path'},
                    ),
                )
            ) as client, filename.open('rb') as stream:
                result = client.put_object(
                    Bucket=destination['bucket'],
                    Key=destination['key'],
                    Body=stream,
                    ContentType='image/png',
                )
            return dict(destination, etag=result.get('ETag'))
        except (BotoCoreError, ClientError, OSError):
            raise ImageError(
                'Cloudflare R2 upload failed; please try again later.'
            ) from None


def _generate(cache, prompt, channel, nick, album_id=None):
    try:
        if album_id is None:
            result = cache.get(prompt, nick, channel)
        else:
            url = generate_album_image(
                MusicLibrary(cache.database),
                cache,
                album_id,
                nick,
                channel,
                include_metadata=False,
            )
            result = f'#{album_id} {url}\n{_album_url(album_id)}'
    except ImageError as exc:
        result = str(exc)
    except Exception as exc:  # noqa: BLE001 - worker must always deliver a safe result
        # Exceptions may contain request credentials; never echo or log their text.
        log.error('Image request failed (%s)', type(exc).__name__)
        result = 'Image request failed; check the bot storage and configuration.'
    _results.put((channel, f'{nick}: {result}'))


def _album_url(album_id):
    url = pmxbot.config.get('albums_url') or pmxbot.config.get('logs URL')
    if not url:
        base = pmxbot.config.get('web_base', '/').strip('/')
        url = f'http://{socket.getfqdn()}/{base}'
    return f'{url.rstrip("/")}/albums/{album_id}'


@command()
def music(channel, nick, rest=''):
    "Generate a random album cover, or retrieve a cached cover: !music [album ID]."
    if not pmxbot.config.get('images_enabled', False):
        return 'Image generation is disabled; configure images_enabled to enable it.'
    if rest.strip():
        value = rest.strip()
        if value.startswith('#'):
            value = value[1:]
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            return 'Usage: !music [album ID]'
        album_id = int(value)
        try:
            cache = ImageCache(pmxbot.config)
            album = MusicLibrary(cache.database).get_album(album_id)
            url = cache.latest_album_image(album_id)
        except LookupError:
            return f'Unknown album ID: #{album_id}.'
        except ImageError as exc:
            return str(exc)
        except Exception:  # noqa: BLE001 - never expose storage details in IRC
            return 'Could not look up album image; check the bot storage and configuration.'
        return f'{_album_details(album)}; #{album_id} {url}'
    configured = pmxbot.config.get('quote_libraries', {})
    libraries = (
        {
            name.lower(): library
            for name, library in configured.items()
            if isinstance(name, str) and isinstance(library, str) and library.strip()
        }
        if isinstance(configured, Mapping)
        else {}
    )
    selected = {}
    for name in ('band', 'album'):
        value, _, _ = quotes.Quotes.store.lookup(library=libraries.get(name, name))
        if not value:
            return f'No {name} entries found. Add one with !{name} add: <text>.'
        selected[name] = value
    return _start_image(
        None,
        channel,
        nick,
        None,
        album_data={
            'artist': selected['band'],
            'title': selected['album'],
        },
    )


def _album_details(album):
    return f"Band: {album['artist_name']}; Album: {album['title']}"


def _start_image(rest, channel, nick, acknowledgement, album_data=None):
    "Start or queue an image request with a command-specific acknowledgement."
    if not pmxbot.config.get('images_enabled', False):
        return 'Image generation is disabled; configure images_enabled to enable it.'
    started = _busy.acquire(blocking=False)
    if not started and _pending.full():
        return 'The image request queue is full; please try again shortly.'
    try:
        cache = ImageCache(pmxbot.config)
        album_id = None
        if album_data is not None:
            album = MusicLibrary(cache.database).create_album(
                **album_data, created_by=nick
            )
            album_id = album['id']
            acknowledgement = (
                f"Looking up or generating your album cover... {_album_details(album)}"
            )
        job = dict(
            target=_generate,
            args=(cache, rest, channel, nick),
            kwargs={'album_id': album_id},
            daemon=True,
        )
        if started:
            threading.Thread(**job).start()
        else:
            _pending.put_nowait(job)
            acknowledgement = (
                f'Image request queued ({_pending.qsize()} waiting). '
                f'{acknowledgement}'
            )
    except ImageError as exc:
        if started:
            _busy.release()
        return str(exc)
    except Exception:  # noqa: BLE001 - release the worker slot on startup failure
        if started:
            _busy.release()
        return 'Could not start image request; check the image configuration.'
    return acknowledgement


@execdelay('image results', None, 1, repeat=True)
def image_results():
    "Deliver worker results on the bot thread, preserving logging and silent mode."
    try:
        channel, result = _results.get_nowait()
    except queue.Empty:
        return
    try:
        job = _pending.get_nowait()
    except queue.Empty:
        _busy.release()
    else:
        try:
            threading.Thread(**job).start()
        except Exception:  # noqa: BLE001 - keep draining after a startup failure
            _, _, next_channel, nick = job['args']
            _results.put(
                (
                    next_channel,
                    f'{nick}: Could not start image request; check the image configuration.',
                )
            )
    yield SwitchChannel(channel)
    yield from result.splitlines()
