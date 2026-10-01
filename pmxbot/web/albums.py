"""Read-only album pages using the existing music library and image cache."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import cherrypy
import jinja2

import pmxbot

environment = jinja2.Environment(
    loader=jinja2.PackageLoader('pmxbot.web'), autoescape=True
)


def public_image_url(value):
    """Only use ordinary public web URLs as image sources."""
    try:
        parsed = urlsplit(value or '')
        return (
            value
            if (
                parsed.scheme in ('http', 'https')
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            )
            else None
        )
    except ValueError:
        return None


def read_album(database, album_id):
    """Read a snapshot without creating tables, migrating, or recording hits."""
    parsed = urlsplit(database)
    if parsed.scheme not in ('', 'sqlite') or not parsed.path:
        raise cherrypy.HTTPError(503, 'Album browsing requires a SQLite database.')
    uri = Path(parsed.path).resolve().as_uri() + '?mode=ro'
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=20)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {'albums', 'artists'} <= tables:
                raise cherrypy.HTTPError(404, 'Album not found.')
            row = db.execute(
                '''SELECT albums.*, artists.name AS artist_name,
                artists.genre AS artist_genre, artists.description AS artist_description
                FROM albums JOIN artists ON artists.id = albums.artist_id
                WHERE albums.id = ?''',
                (album_id,),
            ).fetchone()
            if row is None:
                raise cherrypy.HTTPError(404, 'Album not found.')
            album = dict(row)
            related = [
                dict(row)
                for row in db.execute(
                    '''SELECT id, title FROM albums WHERE artist_id = ? AND id != ?
                ORDER BY created_at DESC, id DESC''',
                    (album['artist_id'], album_id),
                )
            ]
            for item in [album, *related]:
                item.update(image_url=None, prompt=None)
            if {'album_images', 'image_cache'} <= tables:
                # Match !music ID's newest hosted image and its stored prompt.
                # Without a hosted image, show the latest saved prompt instead.
                for item in [album, *related]:
                    for index, image in enumerate(
                        db.execute(
                            '''SELECT hosted_url, prompt FROM image_cache
                        JOIN album_images USING (cache_key) WHERE album_id = ?
                        ORDER BY image_cache.created_at DESC, image_cache.rowid DESC''',
                            (item['id'],),
                        )
                    ):
                        if index == 0:
                            item['prompt'] = image['prompt']
                        url = public_image_url(image['hosted_url'])
                        if url:
                            item.update(image_url=url, prompt=image['prompt'])
                            break
            return album, related
    except sqlite3.Error:
        raise cherrypy.HTTPError(503, 'Album storage is unavailable.') from None


class AlbumPage:
    _cp_config: ClassVar[dict] = {
        'tools.allow.on': True,
        'tools.allow.methods': ['GET', 'HEAD'],
    }

    @cherrypy.expose
    def default(self, album_id=None, *extra):
        if (
            extra
            or not album_id
            or not album_id.isascii()
            or not album_id.isdecimal()
            or len(album_id) > 19
            or not 0 < int(album_id) <= 9223372036854775807
        ):
            raise cherrypy.HTTPError(404, 'Album not found.')
        album, related = read_album(
            pmxbot.config.get('database', 'sqlite:pmxbot.sqlite'), int(album_id)
        )
        base = pmxbot.config.get('web_base', '').rstrip('/')
        canonical = f'{base}/albums/{album["id"]}'
        if album_id != str(album['id']) or cherrypy.request.path_info.endswith('/'):
            raise cherrypy.HTTPRedirect(canonical, 301)
        return (
            environment.get_template('album.html')
            .render(album=album, related=related, base=base, canonical=canonical)
            .encode('utf-8')
        )
