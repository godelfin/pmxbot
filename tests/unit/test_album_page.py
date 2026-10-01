import io
import sqlite3
from contextlib import closing
from wsgiref.util import setup_testing_defaults

import cherrypy
import pytest
from bs4 import BeautifulSoup

import pmxbot
from pmxbot.images import ImageCache
from pmxbot.music import MusicLibrary
from pmxbot.web.viewer import PmxbotPages


@pytest.fixture
def library(tmp_path, monkeypatch):
    database = tmp_path / 'music.sqlite'
    config = {
        'database': f'sqlite:{database}',
        'images_directory': str(tmp_path / 'images'),
    }
    monkeypatch.setattr(pmxbot, 'config', config)
    library = MusicLibrary(database)
    library.create_album(
        'Björk <script>',
        'First <script>',
        genre='Electronic',
        format='LP',
        format_description='Studio',
        description='Album description',
    )
    library.create_album('Björk <script>', 'Second')
    library.create_album('Björk <script>', 'Third')
    library.create_album('Someone else', 'Unrelated')
    with closing(library.connect()) as db, db:
        db.execute(
            "UPDATE artists SET genre='Art pop', description='Artist description' WHERE id=1"
        )
    return library


def request(path, method='GET', base=''):
    app = cherrypy.Application(PmxbotPages(), script_name=base)
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        PATH_INFO=path,
        SCRIPT_NAME=base,
        REQUEST_METHOD=method,
        **{'wsgi.input': io.BytesIO()},
    )
    response = {}

    def start_response(status, headers, exc_info=None):
        response.update(status=int(status.split()[0]), headers=dict(headers))

    body = app(environ, start_response)
    try:
        response['body'] = b''.join(body).decode()
    finally:
        if hasattr(body, 'close'):
            body.close()
    return response


def add_image(
    library,
    key,
    album_id=1,
    url='https://images.example/cover.png',
    prompt='Saved <prompt>',
):
    with closing(ImageCache(pmxbot.config).connect()) as db:
        db.execute(
            '''INSERT INTO image_cache
            (cache_key, prompt, normalized_prompt, settings_json, local_filename,
             hosted_url, generation_metadata_json) VALUES (?, ?, '', '{}', '', ?, '{}')''',
            (key, prompt, url),
        )
    library.record_image(album_id, key, 'tester')


def test_placeholder_metadata_and_related(library):
    before = library.database.read_bytes()
    response = request('/albums/1')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert soup.select_one('link[rel=canonical]')['href'] == '/albums/1'
    assert soup.select_one('main [role=img]')
    for value in [
        'Björk <script>',
        'Electronic',
        'LP',
        'Studio',
        'Album description',
        'Art pop',
        'Artist description',
    ]:
        assert value in soup.get_text()
    assert [a['href'] for a in soup.select('aside a')] == ['/albums/3', '/albums/2']
    assert 'Unrelated' not in soup.get_text()
    assert not soup.select('script, form, input, textarea, button, pre')
    assert library.database.read_bytes() == before


def test_latest_hosted_image_and_its_original_prompt(library):
    add_image(library, 'old', url='https://images.example/old.png', prompt='Old prompt')
    add_image(library, 'new')
    add_image(library, 'unhosted', url=None, prompt='Not uploaded')
    add_image(library, 'related', album_id=2)
    before = library.database.read_bytes()
    response = request('/albums/1')
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert soup.select_one('main img')['src'] == 'https://images.example/cover.png'
    assert soup.pre.get_text() == 'Saved <prompt>'
    assert not soup.select('prompt, script')
    assert len(soup.select('aside img')) == 1
    assert len(soup.select('aside [role=img]')) == 1
    assert library.database.read_bytes() == before


@pytest.mark.parametrize(
    'url', [None, '', 'javascript:alert(1)', 'data:text/html,test', 'https://[bad']
)
def test_unavailable_image_still_shows_saved_prompt(library, url):
    add_image(library, 'missing', url=url)
    soup = BeautifulSoup(request('/albums/1')['body'], 'html.parser')
    assert not soup.select('main img')
    assert soup.pre.get_text() == 'Saved <prompt>'


@pytest.mark.parametrize(
    'path',
    [
        '/albums',
        '/albums/0',
        '/albums/-1',
        '/albums/nope',
        '/albums/999',
        '/albums/9223372036854775808',
        '/albums/' + '9' * 100,
        '/albums/1/extra',
    ],
)
def test_invalid_and_unknown_ids(library, path):
    assert request(path)['status'] == 404


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_read_only_methods(library, method):
    before = library.database.read_bytes()
    response = request('/albums/1', method)
    assert response['status'] == 405
    assert library.database.read_bytes() == before


def test_mount_prefix_canonical_and_head(library):
    pmxbot.config['web_base'] = '/logs'
    soup = BeautifulSoup(request('/albums/1', base='/logs')['body'], 'html.parser')
    assert soup.select_one('link[rel=canonical]')['href'] == '/logs/albums/1'
    assert soup.select_one('aside a')['href'] == '/logs/albums/3'
    response = request('/albums/01', base='/logs')
    assert response['status'] == 301
    assert response['headers']['Location'].endswith('/logs/albums/1')
    response = request('/albums/1', 'HEAD', base='/logs')
    assert response['status'] == 200
    assert not response['body']


def test_empty_related_and_dangling_cache(library):
    library.record_image(4, 'missing-cache-record', 'tester')
    soup = BeautifulSoup(request('/albums/4')['body'], 'html.parser')
    assert 'No other albums' in soup.aside.get_text()
    assert not soup.select('main img, pre')


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    database = tmp_path / 'missing.sqlite'
    monkeypatch.setattr(pmxbot, 'config', {'database': f'sqlite:{database}'})
    assert request('/albums/1')['status'] == 503
    assert not database.exists()


def test_no_music_tables_are_created(tmp_path, monkeypatch):
    database = tmp_path / 'empty.sqlite'
    sqlite3.connect(database).close()
    monkeypatch.setattr(pmxbot, 'config', {'database': f'sqlite:{database}'})
    before = database.read_bytes()
    assert request('/albums/1')['status'] == 404
    assert database.read_bytes() == before
