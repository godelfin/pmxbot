"""OpenAI image generation with a persistent local cache and ImgBB hosting."""

import base64
import binascii
import hashlib
import json
import logging
import os
import queue
import sqlite3
import tempfile
import threading
import unicodedata
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import requests

import pmxbot

from .core import SwitchChannel, command, execdelay

log = logging.getLogger(__name__)
_busy = threading.Lock()
_results = queue.Queue()


class ImageError(Exception):
    """An error safe to display in IRC (no provider response or credentials)."""


def normalize_prompt(prompt):
    return ' '.join(unicodedata.normalize('NFKC', prompt).split()).casefold()


def post_json(url, provider, **kwargs):
    try:
        response = requests.post(url, **kwargs)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        raise ImageError(
            f'{provider} request failed; please try again later.'
        ) from None


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
        self.database = Path(parsed.path).resolve()
        self.settings = {
            'model': config.get('images_model', 'gpt-image-1'),
            'size': config.get('images_size', '1024x1024'),
            'quality': config.get('images_quality', 'low'),
            'output_format': 'png',
        }
        self.openai_key = config.get('openai_api_key') or os.environ.get(
            'OPENAI_API_KEY'
        )
        self.imgbb_key = config.get('imgbb_api_key') or os.environ.get('IMGBB_API_KEY')

    def cache_key(self, prompt):
        value = dict(self.settings, prompt=normalize_prompt(prompt), version=1)
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.database), timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute(
                '''CREATE TABLE IF NOT EXISTS image_cache (
                cache_key TEXT PRIMARY KEY, prompt TEXT NOT NULL,
                normalized_prompt TEXT NOT NULL, settings_json TEXT NOT NULL,
                local_filename TEXT NOT NULL, hosted_url TEXT,
                host TEXT NOT NULL DEFAULT 'imgbb', host_metadata_json TEXT,
                generation_metadata_json TEXT NOT NULL, requested_by TEXT,
                channel TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                uploaded_at TEXT, last_accessed_at TEXT,
                hit_count INTEGER NOT NULL DEFAULT 0, last_error TEXT
            )'''
            )
        except Exception:
            db.close()
            raise
        return db

    def get(self, prompt, nick='', channel=''):
        if not normalize_prompt(prompt):
            raise ImageError('Usage: !image <prompt>')
        key = self.cache_key(prompt)
        with closing(self.connect()) as db:
            row = db.execute(
                'SELECT * FROM image_cache WHERE cache_key = ?', (key,)
            ).fetchone()
            if row and row['hosted_url']:
                db.execute(
                    '''UPDATE image_cache SET hit_count = hit_count + 1,
                    last_accessed_at = CURRENT_TIMESTAMP WHERE cache_key = ?''',
                    (key,),
                )
                return row['hosted_url']
            if not self.imgbb_key:
                raise ImageError('Configure IMGBB_API_KEY before generating images.')
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
                db.execute(
                    '''INSERT OR REPLACE INTO image_cache
                    (cache_key, prompt, normalized_prompt, settings_json,
                     local_filename, generation_metadata_json, requested_by, channel)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (
                        key,
                        prompt,
                        normalize_prompt(prompt),
                        json.dumps(self.settings),
                        str(filename),
                        json.dumps(metadata),
                        nick,
                        str(channel),
                    ),
                )
            try:
                hosted_url, metadata = self.upload(filename)
            except ImageError as exc:
                db.execute(
                    'UPDATE image_cache SET last_error = ? WHERE cache_key = ?',
                    (str(exc), key),
                )
                raise ImageError(
                    'Image saved locally, but upload failed. Repeat the prompt to retry.'
                ) from None
            db.execute(
                '''UPDATE image_cache SET hosted_url = ?, host_metadata_json = ?,
                uploaded_at = CURRENT_TIMESTAMP, last_accessed_at = CURRENT_TIMESTAMP,
                last_error = NULL WHERE cache_key = ?''',
                (hosted_url, json.dumps(metadata), key),
            )
            return hosted_url

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

    def upload(self, filename):
        with filename.open('rb') as stream:
            data = post_json(
                'https://api.imgbb.com/1/upload',
                'ImgBB',
                data={'key': self.imgbb_key, 'name': filename.stem},
                files={'image': (filename.name, stream, 'image/png')},
                timeout=(10, 60),
            )
        try:
            metadata = data['data']
            url = metadata['url']
            parsed = urlsplit(url)
            if (
                data.get('success') is not True
                or parsed.scheme != 'https'
                or not parsed.netloc
                or any(char.isspace() for char in url)
            ):
                raise ValueError('Invalid hosted URL')
            return url, metadata
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ImageError('ImgBB returned no valid hosted URL.') from None


def _generate(cache, prompt, channel, nick):
    try:
        result = cache.get(prompt, nick, channel)
    except ImageError as exc:
        result = str(exc)
    except Exception as exc:  # noqa: BLE001 - worker must always deliver a safe result
        # Exceptions may contain request credentials; never echo or log their text.
        log.error('Image request failed (%s)', type(exc).__name__)
        result = 'Image request failed; check the bot storage and configuration.'
    _results.put((channel, f'{nick}: {result}'))


@command(aliases=('imagine',))
def image(rest, channel, nick):
    "Generate and cache an image: !image <prompt>."
    if not normalize_prompt(rest):
        return 'Usage: !image <prompt>'
    if not pmxbot.config.get('images_enabled', False):
        return 'Image generation is disabled; configure images_enabled to enable it.'
    if not _busy.acquire(blocking=False):
        return 'An image request is already running; please try again shortly.'
    try:
        cache = ImageCache(pmxbot.config)
        threading.Thread(
            target=_generate, args=(cache, rest, channel, nick), daemon=True
        ).start()
    except ImageError as exc:
        _busy.release()
        return str(exc)
    except Exception:  # noqa: BLE001 - release the worker slot on startup failure
        _busy.release()
        return 'Could not start image request; check the image configuration.'
    return 'Looking up or generating your image…'


@execdelay('image results', None, 1, repeat=True)
def image_results():
    "Deliver worker results on the bot thread, preserving logging and silent mode."
    try:
        channel, result = _results.get_nowait()
    except queue.Empty:
        return
    _busy.release()
    yield SwitchChannel(channel)
    yield result
