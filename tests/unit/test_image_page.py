import io
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import cherrypy
import pytest
from bs4 import BeautifulSoup

import pmxbot
from pmxbot.images import ImageCache
from pmxbot.music import MusicLibrary
from pmxbot.web import viewer


@pytest.fixture
def page(tmp_path, monkeypatch):
    config = pmxbot.core.ConfigDict(
        database=f'sqlite:{tmp_path / "bot.sqlite"}',
        images_directory=str(tmp_path / 'images'),
        web_base='/bot',
        bot_nickname='pmxbot',
        logo='/bot/pmxbot.png',
    )
    monkeypatch.setattr(pmxbot, 'config', config)
    cache = ImageCache(pmxbot.config)
    monkeypatch.setattr(
        ImageCache, 'generate', Mock(side_effect=AssertionError('generation'))
    )
    monkeypatch.setattr(
        ImageCache, 'upload', Mock(side_effect=AssertionError('upload'))
    )
    app = cherrypy.Application(viewer.PmxbotPages(), '/bot')

    def request(path='/images/42', method='GET'):
        env = {
            'REQUEST_METHOD': method,
            'SCRIPT_NAME': '/bot',
            'PATH_INFO': path,
            'QUERY_STRING': '',
            'SERVER_NAME': 'localhost',
            'HTTP_HOST': 'localhost',
            'CONTENT_LENGTH': '0',
            'SERVER_PORT': '80',
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'wsgi.version': (1, 0),
            'wsgi.url_scheme': 'http',
            'wsgi.input': io.BytesIO(),
            'wsgi.errors': io.StringIO(),
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
        }
        response = {}

        def start(status, headers, exc_info=None):
            response.update(status=int(status.split()[0]), headers=dict(headers))

        output = app(env, start)
        try:
            response['body'] = b''.join(output).decode()
        finally:
            output.close()
        return response

    return cache, request


def insert(cache, url='https://images.example/art.png?x=1&y=2'):
    with closing(cache.connect()) as db:
        db.execute(
            '''INSERT INTO image_cache
            (id, cache_key, prompt, normalized_prompt, settings_json, local_filename,
             hosted_url, generation_metadata_json, requested_by, channel, created_at)
            VALUES (42, 'key', ?, 'normalized', ?, '/private/secret.png', ?, ?, ?, ?, '2026-01-02 03:04:05')''',
            (
                '  exact <script> & "prompt"\nsecond line  ',
                '{"model":"gpt-image-1","quality":"low","secret":"hidden"}',
                url,
                '{"usage":{"total_tokens":7},"revised_prompt":"<b>revision</b>"}',
                '<alice>',
                '#<channel>',
            ),
        )


def test_render_and_read_only(page):
    cache, request = page
    insert(cache)
    library = MusicLibrary(cache.database)
    album = library.create_album('<Band>', '<Album>')
    library.record_image(album['id'], 'key', 'alice')
    before = cache.database.read_bytes()
    response = request()
    assert response['status'] == 200
    body = response['body']
    soup = BeautifulSoup(body, 'html.parser')
    assert soup.title.get_text() == 'Image #42'
    assert soup.select_one('main > h1').get_text() == 'Image #42'
    assert soup.select_one('meta[name=viewport]')['content'] == (
        'width=device-width, initial-scale=1'
    )
    preview = soup.select_one('#artwork-heading').parent.select_one('a')
    assert (
        preview['href']
        == preview.img['src']
        == ('https://images.example/art.png?x=1&y=2')
    )
    assert preview['class'] == ['artwork-preview']
    assert preview['rel'] == ['noreferrer']
    assert preview.img['referrerpolicy'] == 'no-referrer'
    assert 'full-size artwork' in preview['aria-label']
    dialog = soup.select_one('#artwork-dialog')
    assert dialog is not None
    assert dialog['aria-label'] == 'Full-size artwork for image #42'
    assert dialog.img['src'] == preview['href']
    assert dialog.img['referrerpolicy'] == 'no-referrer'
    assert dialog.select_one('button[type=button]')['aria-label'] == (
        'Close full-size artwork'
    )
    script = soup.find('script').get_text()
    assert "preview.addEventListener('click'" in script
    assert 'event.preventDefault()' in script
    assert 'dialog.showModal()' in script
    assert 'dialog.close()' in script
    assert [section.h2.get_text() for section in soup.select('main section')] == [
        'Artwork',
        'Original prompt',
        'Image details',
        'Settings',
        'Generation metadata',
        'Related albums',
    ]
    for section in soup.select('main section'):
        assert section['aria-labelledby'] == section.h2['id']
    assert soup.select_one('#prompt-heading').parent.pre.get_text() == (
        '  exact <script> & "prompt"\nsecond line  '
    )
    assert not soup.select('form, input, textarea')
    assert 'Image #42' in body
    assert 'src="https://images.example/art.png?x=1&amp;y=2"' in body
    assert '  exact &lt;script&gt; &amp; &#34;prompt&#34;\nsecond line  ' in body
    for value in (
        '&lt;alice&gt;',
        '#&lt;channel&gt;',
        '2026-01-02 03:04:05',
        'gpt-image-1',
        'total_tokens',
        '&lt;b&gt;revision&lt;/b&gt;',
        '&lt;Band&gt;',
        '&lt;Album&gt;',
        f'/bot/albums/{album["id"]}',
    ):
        assert value in body
    assert '/private/secret.png' not in body
    assert 'hidden' not in body
    assert '<script> & ' not in body
    assert request(method='HEAD')['body'] == ''
    assert request(method='HEAD')['status'] == 200
    assert cache.database.read_bytes() == before
    assert not any(
        word in body for word in ('method="POST"', 'Customize', 'Delete', 'Rate')
    )


@pytest.mark.parametrize(
    'path',
    [
        '/images',
        '/images/',
        '/images/no',
        '/images/0',
        '/images/-1',
        '/images/1.0',
        '/images/４２',
        '/images/42/extra',
        '/images/9223372036854775808',
        '/images/' + '9' * 5000,
        '/images/999',
    ],
)
def test_invalid_and_unknown_do_not_create_storage(page, path):
    cache, request = page
    assert request(path)['status'] == 404
    assert not cache.database.exists()
    assert not cache.directory.exists()


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def test_methods_rejected_before_lookup(page, method):
    cache, request = page
    response = request(method=method)
    assert response['status'] == 405
    assert response['headers']['Allow'] == 'GET, HEAD'
    assert not cache.database.exists()


@pytest.mark.parametrize(
    'url',
    [
        None,
        '',
        'javascript:alert(1)',
        'data:image/png;base64,x',
        '//example.com/x',
        'https://user:secret@example.com/x',
        'https://[broken',
        'https://example.com/\ninject',
        'https://example.com\\evil',
        'file:///tmp/a',
        'https://example.com:bad/x',
    ],
)
def test_unavailable_artwork_retains_metadata(page, url):
    cache, request = page
    insert(cache, url)
    response = request()
    assert response['status'] == 200
    assert 'Hosted artwork is unavailable' in response['body']
    assert 'alt="Generated image' not in response['body']
    assert 'Original prompt' in response['body']
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert not soup.select('.artwork-preview, img')
    assert soup.select_one('#details-heading')
    assert soup.select_one('#metadata-heading-2')
    assert not soup.select_one('#albums-heading')


def test_database_without_image_schema_is_unchanged(page):
    cache, request = page
    with sqlite3.connect(cache.database) as db:
        db.execute('CREATE TABLE unrelated (value TEXT)')
    before = cache.database.read_bytes()
    assert request()['status'] == 404
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize('data', ['not json', 'null', '[]'])
def test_malformed_metadata(page, data):
    cache, request = page
    insert(cache)
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE image_cache SET settings_json = ?, generation_metadata_json = ?',
            (data, data),
        )
    response = request()
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert [section.h2.get_text() for section in soup.select('main section')] == [
        'Artwork',
        'Original prompt',
        'Image details',
    ]


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def test_existing_image_rejects_writes(page, method):
    cache, request = page
    insert(cache)
    before = cache.database.read_bytes()
    response = request(method=method)
    assert response['status'] == 405
    assert response['headers']['Allow'] == 'GET, HEAD'
    assert cache.database.read_bytes() == before


def test_optional_details_and_attribute_escaping(page):
    cache, request = page
    url = 'https://images.example/art.png?caption="<art>"&other=1'
    insert(cache, url)
    with sqlite3.connect(cache.database) as db:
        db.execute(
            '''UPDATE image_cache SET requested_by = NULL, channel = NULL,
            uploaded_at = ?, host = ?''',
            ('2026-10-03 09:00:00', '<host>'),
        )
    before = cache.database.read_bytes()
    response = request()
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert soup.select_one('.artwork-preview')['href'] == url
    assert soup.img['src'] == url
    assert set(soup.img.attrs) == {'src', 'alt', 'referrerpolicy'}
    assert [dt.get_text() for dt in soup.select('dt')] == [
        'Image ID',
        'Created',
        'Uploaded',
        'Image host',
    ]
    assert soup.select_one('dl').get_text().endswith('<host>\n')
    assert not soup.select('host, art')
    assert cache.database.read_bytes() == before


def test_storage_failure_is_clean(page):
    cache, request = page
    cache.database.write_text('not sqlite')
    response = request()
    assert response['status'] == 503
    assert str(cache.database) not in response['body']
