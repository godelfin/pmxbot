"""Render real album views for the browser tests, without live providers."""

import json
import runpy
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import pmxbot


def main():
    setattr(pmxbot, 'config', {})
    helpers = runpy.run_path('tests/unit/test_album_page.py')
    with tempfile.TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
        cache, request = helpers['page'].__wrapped__(
            Path(temporary), patch, SimpleNamespace()
        )
        helpers['insert'](cache)
        from pmxbot.music import MusicLibrary

        library = MusicLibrary(cache.database)
        output = Path(sys.argv[1])
        output.mkdir(parents=True, exist_ok=True)
        output.joinpath('one.html').write_text(request()['body'], encoding='utf-8')
        empty = library.create_album('Empty', 'No artwork')
        output.joinpath('empty.html').write_text(
            request('/albums/' + str(empty['id']))['body'], encoding='utf-8'
        )
        earlier = library.create_album('Earlier', 'Previous album')
        with sqlite3.connect(cache.database) as db:
            db.execute('UPDATE albums SET id = 41 WHERE id = ?', (earlier['id'],))
        with sqlite3.connect(cache.database) as db:
            db.execute("UPDATE albums SET created_at = '2026-01-01'")
        for identifier in (41, 43):
            output.joinpath('album' + str(identifier) + '.html').write_text(
                request('/albums/' + str(identifier))['body'], encoding='utf-8'
            )
        for identifier in range(43, 63):
            with sqlite3.connect(cache.database) as db:
                db.execute(
                    '''INSERT INTO image_cache
                    (id, cache_key, prompt, normalized_prompt, settings_json,
                     local_filename, hosted_url, generation_metadata_json, created_at, parent_image_id)
                    VALUES (?, ?, ?, 'prompt', '{}', '/unused', ?, ?, '2026-01-03', ?)''',
                    (
                        identifier,
                        str(identifier),
                        'Prompt #' + str(identifier),
                        (
                            None
                            if identifier == 44
                            else 'https://art.example/' + str(identifier)
                        ),
                        json.dumps(
                            {
                                'revised_prompt': 'Metadata #' + str(identifier),
                                'operation': (
                                    'create_new' if identifier == 43 else 'edit'
                                ),
                            }
                        ),
                        None if identifier == 43 else identifier - 1,
                    ),
                )
            library.record_image(42, str(identifier))
        from pmxbot.web import auth
        from pmxbot.users import User

        for signed in (False, True):
            if signed:
                patch.setattr(
                    auth,
                    'current_user',
                    lambda: User(
                        1,
                        'Alice',
                        'alice',
                        None,
                        '2026',
                        True,
                        False,
                        can_generate_images=True,
                    ),
                )
                patch.setattr(
                    type(cache), 'variation_capability', lambda self, image: (True, '')
                )
                pmxbot.config['images_enabled'] = True
            suffix = '-signed' if signed else ''
            for identifier in range(42, 63):
                output.joinpath(str(identifier) + suffix + '.html').write_text(
                    request('/albums/42?source_image_id=' + str(identifier))['body'],
                    encoding='utf-8',
                )
                output.joinpath(str(identifier) + suffix + '.json').write_text(
                    request(
                        '/albums/42?source_image_id=' + str(identifier),
                        accept='application/json',
                    )['body'],
                    encoding='utf-8',
                )

        # Collection fixtures are rendered last so album shortcut fixtures retain
        # their three-album boundaries.
        for number in range(27):
            album = library.create_album('<Band>', f'<Album> {number:02}')
            key = f'gallery-{number}'
            with sqlite3.connect(cache.database) as db:
                db.execute(
                    """INSERT INTO image_cache
                (cache_key, prompt, normalized_prompt, settings_json,
                 local_filename, hosted_url, generation_metadata_json)
                VALUES (?, '', '', '{}', '/unused', ?, '{}')""",
                    (key, f'https://art.example/gallery-{number}'),
                )
            library.record_image(album['id'], key)
        output.joinpath('create-album.html').write_text(
            request('/create_album')['body'], encoding='utf-8'
        )
        from contextlib import closing
        from pmxbot.prompt_rules import PromptRuleStore

        with closing(PromptRuleStore(str(cache.database))) as rules:
            manager = rules.create('rule-manager')
            manager = rules.update(
                manager.id, role='admin', can_manage_prompt_rules=True
            )
        previous_user = auth.current_user
        patch.setattr(auth, 'current_user', lambda: manager)
        for name in ('prompt_rules', 'users'):
            output.joinpath(name + '.html').write_text(
                request('/admin/' + name)['body'], encoding='utf-8'
            )
        patch.setattr(auth, 'current_user', previous_user)
        query = '?q=%3CAlbum%3E&artist=%3CBand%3E&sort=album&order=asc&page=1'
        output.joinpath('gallery.html').write_text(
            request('/gallery' + query)['body'], encoding='utf-8'
        )
        output.joinpath('catalog.html').write_text(
            request('/' + query)['body'], encoding='utf-8'
        )


if __name__ == "__main__":
    main()
