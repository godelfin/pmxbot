import io
import json
import sqlite3
from contextlib import closing
from unittest.mock import Mock
from urllib.parse import urlencode, urlsplit, parse_qs

import cherrypy
import pytest
from PIL import Image
from bs4 import BeautifulSoup

import pmxbot
from pmxbot.images import ImageCache, ImageError
from pmxbot.users import User
from pmxbot.music import MusicLibrary
from pmxbot.web import viewer, auth
from http.cookies import SimpleCookie

ORIGINAL_GENERATE = ImageCache.generate
ORIGINAL_UPLOAD = ImageCache.upload
with io.BytesIO() as output:
    Image.new('RGB', (256, 128), 'blue').save(output, format='PNG')
    PNG = output.getvalue()


@pytest.fixture
def page(tmp_path, monkeypatch, request):
    base = getattr(request, 'param', '/bot')
    config = pmxbot.core.ConfigDict(
        database=f'sqlite:{tmp_path / "bot.sqlite"}',
        images_directory=str(tmp_path / 'images'),
        web_base=base,
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
    config.web_session_secure = False
    app = cherrypy.Application(
        viewer.PmxbotPages(), base, {'/': auth.session_config(config)}
    )
    cookies = SimpleCookie()

    def request(path='/albums/42', method='GET', data=None, accept=None):
        if method == 'POST' and data and 'csrf_token' not in data:
            initial = BeautifulSoup(request(path)['body'], 'html.parser')
            token = initial.select_one('[name=csrf_token]')
            data = dict(data, csrf_token=token['value'] if token else '')
        payload = urlencode(data or {}).encode()
        path, _, query = path.partition('?')
        env = {
            'REQUEST_METHOD': method,
            'SCRIPT_NAME': base,
            'PATH_INFO': path,
            'QUERY_STRING': query,
            'SERVER_NAME': 'localhost',
            'HTTP_HOST': 'localhost',
            'HTTP_COOKIE': '; '.join(
                f'{name}={cookie.value}' for name, cookie in cookies.items()
            ),
            'CONTENT_LENGTH': str(len(payload)),
            'CONTENT_TYPE': 'application/x-www-form-urlencoded',
            'SERVER_PORT': '80',
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'wsgi.version': (1, 0),
            'wsgi.url_scheme': 'http',
            'wsgi.input': io.BytesIO(payload),
            'wsgi.errors': io.StringIO(),
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
        }
        if accept:
            env['HTTP_ACCEPT'] = accept
        response = {}

        def start(status, headers, exc_info=None):
            response.update(status=int(status.split()[0]), headers=dict(headers))
            for name, value in headers:
                if name.lower() == 'set-cookie':
                    cookies.load(value)

        output = app(env, start)
        try:
            response['body'] = b''.join(output).decode()
        finally:
            output.close()
        return response

    return cache, request


def insert(cache, url='https://albums.example/art.png?x=1&y=2', image_id=42):
    with closing(cache.connect()) as db:
        db.execute(
            '''INSERT INTO image_cache
            (id, cache_key, prompt, normalized_prompt, settings_json, local_filename,
             hosted_url, generation_metadata_json, requested_by, channel, created_at)
            VALUES (?, 'key', ?, 'normalized', ?, '/private/secret.png', ?, ?, ?, ?, '2026-01-02 03:04:05')''',
            (
                image_id,
                '  exact <script> & "prompt"\nsecond line  ',
                '{"model":"gpt-image-1","quality":"low","secret":"hidden"}',
                url,
                '{"usage":{"total_tokens":7},"revised_prompt":"<b>revision</b>"}',
                '<alice>',
                '#<channel>',
            ),
        )

    library = MusicLibrary(cache.database)
    album = library.create_album('<Band>', '<Album>')
    with sqlite3.connect(cache.database) as db:
        db.execute('UPDATE albums SET id = 42 WHERE id = ?', (album['id'],))
    library.record_image(42, 'key')


def test_render_and_read_only(page):
    cache, request = page
    insert(cache)
    library = MusicLibrary(cache.database)
    album = library.create_album('<Band>', '<Album>')
    library.record_image(album['id'], 'key')
    before = cache.database.read_bytes()
    response = request()
    assert response['status'] == 200
    body = response['body']
    soup = BeautifulSoup(body, 'html.parser')
    assert soup.title.get_text() == 'Album #42'
    assert soup.select_one('main > h1').get_text() == '<Band> — <Album>'
    assert soup.select_one('meta[name=viewport]')['content'] == (
        'width=device-width, initial-scale=1'
    )
    artwork = soup.select_one('section[aria-label="Artwork"]')
    assert artwork is not None
    preview = artwork.select_one('a.artwork-preview')
    assert (
        preview['href']
        == preview.img['src']
        == ('https://albums.example/art.png?x=1&y=2')
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
    assert "document.addEventListener('click'" in script
    assert 'event.preventDefault()' in script
    assert 'dialog.showModal()' in script
    assert 'dialog.close()' in script
    assert [
        section.h2.get_text()
        for section in soup.select('.image-content section[aria-labelledby]')
    ] == [
        'Artwork history',
        'Original prompt',
        'Image details',
        'Settings',
        'Generation metadata',
    ]
    for section in soup.select('.image-content section[aria-labelledby]'):
        assert section['aria-labelledby'] == section.h2['id']
    assert soup.select_one('#prompt-heading').parent.pre.get_text() == (
        '  exact <script> & "prompt"\nsecond line  '
    )
    assert soup.select_one('form')['method'] == 'POST'
    assert not soup.select('[name=prompt], [name=artist_name], [name=title]')
    assert soup.select_one('#album-band').get_text() == '<Band>'
    assert soup.select_one('#album-title').get_text() == '<Album>'
    for name, choices in (
        ('format', pmxbot.albums.formats),
        ('format_description', pmxbot.albums.format_desc),
        ('genre', set(pmxbot.albums.genres).union(*pmxbot.albums.genres.values())),
        (
            'artist_genre',
            set(pmxbot.albums.genres).union(*pmxbot.albums.genres.values()),
        ),
    ):
        select = soup.select_one(f'select[name="{name}"]')
        assert {option['value'] for option in select.select('option')} == choices | {''}
        assert select.select_one('option[selected]')['value'] == ''
    assert 'Album #42' in body
    assert 'src="https://albums.example/art.png?x=1&amp;y=2"' in body
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
    ):
        assert value in body
    assert '/private/secret.png' not in body
    assert 'hidden' not in soup.select_one('#metadata-heading-1').parent.get_text()
    assert '<script> & ' not in body
    assert request(method='HEAD')['body'] == ''
    assert request(method='HEAD')['status'] == 200
    assert cache.database.read_bytes() == before
    assert not any(word in body for word in ('Customize', 'Delete', 'Rate'))


@pytest.mark.parametrize(
    'mode,metadata,available,reason',
    [
        (
            'responses',
            {'api': 'responses', 'response_id': 'resp_selected', 'store': True},
            True,
            'subject to OpenAI context retention',
        ),
        (
            'images',
            {'api': 'responses', 'response_id': 'resp_selected', 'store': True},
            False,
            'configure image_api',
        ),
        ('responses', {'api': 'images'}, False, 'Images API'),
        ('responses', {}, False, 'legacy image'),
        (
            'responses',
            {'api': 'responses', 'store': True},
            False,
            'no stored response context',
        ),
        (
            'responses',
            {'api': 'responses', 'response_id': 'resp_selected', 'store': False},
            False,
            'no stored response context',
        ),
    ],
)
def test_selected_image_continuation_capability(
    page, mode, metadata, available, reason
):
    cache, request = page
    insert(cache)
    pmxbot.config.update(image_api=mode, responses_model='gpt-5')
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE image_cache SET generation_metadata_json = ? WHERE id = 42',
            (json.dumps(metadata),),
        )
        db.execute(
            """INSERT INTO image_cache
            (id, cache_key, prompt, normalized_prompt, settings_json,
             local_filename, generation_metadata_json, created_at)
            VALUES (43, 'newer', 'newer', 'newer', '{}', 'new.png', '{}', '2099-01-01')"""
        )
    MusicLibrary(cache.database).record_image(42, 'newer')
    before = cache.database.read_bytes()
    soup = BeautifulSoup(
        request('/albums/42?source_image_id=42')['body'], 'html.parser'
    )
    configured_cache = ImageCache(pmxbot.config)
    status, message = configured_cache.continuation_capability(cache.get_image(42))
    assert status is available
    assert reason in message
    assert soup.select_one('[name=source_image_id]')['value'] == '42'
    assert soup.select_one('button[type=submit][value=edit]').get_text() == 'Edit'
    assert 'Generate a new version' not in soup.get_text()
    latest = BeautifulSoup(request()['body'], 'html.parser')
    assert latest.select_one('[name=source_image_id]')['value'] == '43'
    assert configured_cache.continuation_capability(cache.get_image(43))[0] is False
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize(
    'path',
    [
        '/albums',
        '/albums/',
        '/albums/no',
        '/albums/0',
        '/albums/-1',
        '/albums/1.0',
        '/albums/４２',
        '/albums/42/extra',
        '/albums/9223372036854775808',
        '/albums/' + '9' * 5000,
        '/albums/999',
    ],
)
def test_invalid_and_unknown_do_not_create_storage(page, path):
    cache, request = page
    assert request(path)['status'] == 404
    assert not cache.database.exists()
    assert not cache.directory.exists()


@pytest.mark.parametrize('method', ['PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def test_methods_rejected_before_lookup(page, method):
    cache, request = page
    response = request(method=method)
    assert response['status'] == 405
    assert response['headers']['Allow'] == 'GET, HEAD, POST'
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
    assert not soup.select('.album-page .artwork-preview, .album-page img')
    assert soup.select_one('.site-brand img')['alt'] == 'bapudi album image generator'
    assert soup.select_one('#details-heading')
    assert soup.select_one('#metadata-heading-2')
    assert soup.select_one('#album-heading')


def test_album_and_band_properties(page):
    cache, request = page
    insert(cache)
    library = MusicLibrary(cache.database)
    album = library.create_album(
        '<Band>',
        '<Album>',
        genre='Album genre',
        format='Vinyl',
        format_description='Limited <edition>',
        description='First line\n<script>album description</script>',
    )
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE albums SET description = ? WHERE id = ?',
            ('First line\n<script>album description</script>', album['id']),
        )
    library.record_image(album['id'], 'key')
    other = library.create_album('Other band', 'Other title')
    library.record_image(other['id'], 'other-key')
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE artists SET genre = ?, description = ? WHERE id = ?',
            ('Band genre', '<b>Band description</b>', album['artist_id']),
        )
    before = cache.database.read_bytes()
    soup = BeautifulSoup(request()['body'], 'html.parser')
    cards = soup.select('#album-heading ~ .album-details')
    assert len(cards) == 1
    details = {}
    for term in cards[0].select('dt'):
        value = term.find_next_sibling('dd')
        control = value.select_one('option[selected]')
        details[term.get_text()] = control['value'] if control else value.get_text()
    assert details == {
        'Title': '<Album>',
        'Format': 'Vinyl',
        'Format description': 'Limited <edition>',
        'Album genre': 'Album genre',
        'Album description': 'First line\n<script>album description</script>',
        'Band': '<Band>',
        'Band genre': 'Band genre',
        'Band description': '<b>Band description</b>',
    }
    assert not cards[0].select('script, b, edition')
    assert cards[0].select_one('button[type=submit][value=edit]').get_text() == 'Edit'
    assert cache.database.read_bytes() == before


def test_album_navigation_and_columns(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    first = library.create_album('Band', 'First')
    removed = library.create_album('Band', 'Removed')
    last = library.create_album('Band', 'Last')
    with sqlite3.connect(cache.database) as db:
        db.execute('DELETE FROM albums WHERE id = ?', (removed['id'],))
    before = cache.database.read_bytes()
    # Direct links use newest-first collection order, including deterministic ties.
    with sqlite3.connect(cache.database) as db:
        db.execute("UPDATE albums SET created_at = '2026-01-01'")
    before = cache.database.read_bytes()
    soup = BeautifulSoup(request(f'/albums/{last["id"]}')['body'], 'html.parser')
    main = soup.select_one('.image-main')
    assert main.select_one('section[aria-label="Artwork"]') is not None
    assert [h.get_text() for h in main.select('h2')] == [
        'Artwork history',
        'Original prompt',
    ]
    navigation = soup.select_one('main > .album-navigation')
    assert (
        navigation.select_one('button[disabled] span').get_text() == '← Previous album'
    )
    assert (
        navigation.select_one('button[disabled]')['aria-keyshortcuts']
        == 'Shift+ArrowLeft'
    )
    assert (
        navigation.select_one('a[rel=next]')['aria-keyshortcuts'] == 'Shift+ArrowRight'
    )
    assert navigation.select_one('a[rel=next] kbd').get_text() == 'Shift+→'
    assert navigation.select_one('a[rel=next]')['href'] == f'/bot/albums/{first["id"]}'
    assert soup.select_one('main > h1').find_next('nav') == navigation
    soup = BeautifulSoup(request(f'/albums/{first["id"]}')['body'], 'html.parser')
    assert soup.select_one('a[rel=prev]')['href'] == f'/bot/albums/{last["id"]}'
    assert (
        soup.select_one('.album-navigation button[disabled] span').get_text()
        == 'Next album →'
    )
    assert cache.database.read_bytes() == before


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
    assert [
        section.h2.get_text()
        for section in soup.select('.image-content section[aria-labelledby]')
    ] == [
        'Artwork history',
        'Original prompt',
        'Image details',
    ]


@pytest.mark.parametrize('method', ['PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def test_existing_image_rejects_writes(page, method):
    cache, request = page
    insert(cache)
    before = cache.database.read_bytes()
    response = request(method=method)
    assert response['status'] == 405
    assert response['headers']['Allow'] == 'GET, HEAD, POST'
    assert cache.database.read_bytes() == before


def test_optional_details_and_attribute_escaping(page):
    cache, request = page
    url = 'https://albums.example/art.png?caption="<art>"&other=1'
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
    artwork_image = soup.select_one('.artwork-preview img')
    assert artwork_image['src'] == url
    assert set(artwork_image.attrs) == {'src', 'alt', 'referrerpolicy'}
    assert [dt.get_text() for dt in soup.select('#details-heading ~ dl dt')] == [
        'Image ID',
        'Created',
        'Uploaded',
        'Image host',
    ]
    assert soup.select_one('#details-heading ~ dl').get_text().endswith('<host>\n')
    assert not soup.select('host, art')
    assert cache.database.read_bytes() == before


def test_storage_failure_is_clean(page):
    cache, request = page
    cache.database.write_text('not sqlite')
    response = request()
    assert response['status'] == 503
    assert str(cache.database) not in response['body']


def test_route_uses_album_id_and_newest_image(page):
    cache, request = page
    insert(cache, image_id=100)
    with sqlite3.connect(cache.database) as db:
        db.execute(
            '''INSERT INTO image_cache
            (id, cache_key, prompt, normalized_prompt, settings_json,
             local_filename, generation_metadata_json, created_at)
            VALUES (101, 'newer', 'new prompt', 'new prompt', '{}', '/private/newer.png', '{}',
                    '2026-10-05 12:00:00')'''
        )
    MusicLibrary(cache.database).record_image(42, 'newer')
    before = cache.database.read_bytes()
    response = request('/albums/42')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert soup.title.get_text() == 'Album #42'
    assert soup.select_one('#prompt-heading ~ pre').get_text() == 'new prompt'
    assert soup.select_one('.image-cache-key').get_text() == 'Cache key: newer'
    assert request('/albums/100')['status'] == 404
    assert request('/images/100')['status'] == 404
    assert cache.database.read_bytes() == before


def test_album_without_artwork(page):
    cache, request = page
    album = MusicLibrary(cache.database).create_album('Band', 'Unillustrated')
    before = cache.database.read_bytes()
    response = request(f'/albums/{album["id"]}')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert soup.select_one('h1').get_text() == 'Band — Unillustrated'
    assert soup.select_one('#album-heading')
    assert not soup.select('.album-page img, #details-heading, .image-cache-key')
    assert soup.select_one('.site-brand img')['alt'] == 'bapudi album image generator'
    assert cache.database.read_bytes() == before


def test_album_selectable_image_history_updates_prompt_and_metadata(page):
    cache, request = page
    insert(cache)
    with closing(cache.connect()) as db:
        db.execute(
            '''INSERT INTO image_cache
            (id, cache_key, prompt, normalized_prompt, settings_json,
             local_filename, hosted_url, generation_metadata_json,
             requested_by, channel, created_at)
            VALUES (43, 'newer', 'new prompt', 'new prompt',
             '{"model":"gpt-image-2"}', '/private/newer.png',
             'https://albums.example/newer.png', '{"revised_prompt":"new details"}',
             'bob', '#new', '2026-01-03 03:04:05')'''
        )
        db.execute(
            '''INSERT INTO image_cache
            (id, cache_key, prompt, normalized_prompt, settings_json,
             local_filename, hosted_url, generation_metadata_json, created_at)
            VALUES (44, 'unhosted', 'unhosted prompt', 'unhosted prompt', '{}',
             '/private/unhosted.png', NULL, '{}', '2026-01-04 03:04:05')'''
        )
    library = MusicLibrary(cache.database)
    library.record_image(42, 'newer')
    library.record_image(42, 'unhosted')
    before = cache.database.read_bytes()

    response = request()
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    entries = soup.select('#image-history-heading ~ ul li')
    assert len(entries) == 3
    assert entries[0].select_one('.history-label').get_text() == 'Image 3'
    assert entries[0].select_one('.selection-indicator').get_text() == 'Selected'
    assert not entries[0].select_one('.selection-indicator').has_attr('hidden')
    assert entries[0].select_one('img') is None
    assert entries[1].select_one('a')['href'] == ('/bot/albums/42?source_image_id=43')
    assert entries[1].select_one('img')['src'] == ('https://albums.example/newer.png')
    assert entries[2].select_one('a')['href'] == ('/bot/albums/42?source_image_id=42')
    assert cache.database.read_bytes() == before

    selected = request('/albums/42?source_image_id=43')
    selected_soup = BeautifulSoup(selected['body'], 'html.parser')
    assert selected['status'] == 200
    assert selected_soup.select_one('#prompt-heading ~ pre').get_text() == (
        'new prompt'
    )
    assert (
        '"revised_prompt": "new details"'
        in selected_soup.select_one('#metadata-heading-2')
        .find_parent('details')
        .pre.get_text()
    )
    assert (
        selected_soup.select_one(
            '#image-history-heading ~ ul li .current-image'
        ).get_text(strip=True)
        == 'Image 2Selected'
    )
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize('with_artwork', [False, True])
def test_album_failure_history_is_escaped_and_read_only(page, with_artwork):
    cache, request = page
    library = MusicLibrary(cache.database)
    if with_artwork:
        insert(cache)
        album_id = 42
    else:
        album_id = library.create_album('Band', 'Album')['id']
    library.record_image_failure(
        album_id,
        '<script>prompt</script>',
        '<alice>',
        '#test',
        ImageError('<b>Failed</b>'),
    )
    library.record_image_failure(
        album_id, 'retry prompt', 'bob', '#test', ImageError('Failed again')
    )
    before = cache.database.read_bytes()
    response = request(f'/albums/{album_id}')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    history = soup.select_one('#failures-heading').parent
    assert 'Failed again' in history.select('li')[0].get_text()
    assert '<b>Failed</b>' in history.get_text()
    assert '<alice>' in history.get_text()
    assert history.pre.get_text() == 'retry prompt'
    assert not history.select('script, b, alice')
    assert bool(soup.select_one('.artwork-preview')) == with_artwork
    assert cache.database.read_bytes() == before


def test_album_page_before_failure_schema_is_read_only(page):
    cache, request = page
    insert(cache)
    with sqlite3.connect(cache.database) as db:
        db.execute('DROP TABLE album_image_failures')
    before = cache.database.read_bytes()
    response = request()
    assert response['status'] == 200
    assert 'failures-heading' not in response['body']
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize(
    'path',
    [
        '/channel/test',
        '/day/test/2026-10-06',
        '/karma',
        '/karma/test',
        '/search',
        '/search/test',
        '/help',
        '/legacy/test/2026-10-06',
        '/legacy/forward/test/2026-10-06/12.00.00.nick',
    ],
)
def test_other_viewer_pages_are_disabled(page, path):
    cache, request = page
    assert request(path)['status'] == 404
    assert not cache.database.exists()
    assert not cache.directory.exists()


def test_homepage_lists_all_albums(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    last = library.create_album('Zulu', 'Last')
    first = library.create_album('<Band>', '<Album>')
    second = library.create_album('<Band>', 'Second')
    before = cache.database.read_bytes()
    response = request('/')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    links = soup.select('.catalog-album a')
    assert [link.get_text() for link in links] == [
        'Second',
        '<Album>',
        'Last',
    ]
    assert [urlsplit(link['href']).path for link in links] == [
        f'/bot/albums/{album["id"]}' for album in (second, first, last)
    ]
    assert not soup.select('band, album')
    for link in links:
        assert link['href'].startswith('/bot/')
        assert request(link['href'][len('/bot') :])['status'] == 200
    assert request('/', method='HEAD')['body'] == ''
    assert cache.database.read_bytes() == before


def test_generation_leaderboard(page):
    cache, request = page
    insert(cache)
    library = MusicLibrary(cache.database)
    # Simulate historical duplicate links without schema migration on reads.
    other = library.create_album('Other', 'Album')
    with sqlite3.connect(cache.database) as db:
        db.execute('DROP INDEX album_images_unique_cache_key')
        db.execute('INSERT INTO album_images VALUES (?, ?)', (other['id'], 'key'))
        for index, nick in enumerate(
            [' alice ', 'alice', 'Bob', 'bob', None, '', ' \t\n', 'unlinked'], 100
        ):
            key = str(index)
            db.execute(
                '''INSERT INTO image_cache
                (cache_key, prompt, normalized_prompt, settings_json,
                local_filename, generation_metadata_json, requested_by)
                VALUES (?, '', '', '{}', 'image.png', '{}', ?)''',
                (key, nick),
            )
            if nick != 'unlinked':
                db.execute('INSERT INTO album_images VALUES (42, ?)', (key,))
        library_failure = (
            42,
            'failed prompt',
            'unlinked',
            'ImageError',
            'generation failed',
        )
        db.execute(
            '''INSERT INTO album_image_failures
            (album_id, prompt, requested_by, error_type, error)
            VALUES (?, ?, ?, ?, ?)''',
            library_failure,
        )
    before = cache.database.read_bytes()
    expected = [
        {'username': 'alice', 'image_count': 2},
        {'username': '<alice>', 'image_count': 1},
        {'username': 'Bob', 'image_count': 1},
        {'username': 'bob', 'image_count': 1},
    ]
    assert cache.generation_leaderboard() == expected
    assert cache.generation_leaderboard(limit=2) == expected[:2]
    response = request('/')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    section = soup.select_one('.leaderboard')
    assert section.h2.get_text() == 'Top album image generators'
    assert [
        [cell.get_text() for cell in row.select('td')]
        for row in section.select('tbody tr')
    ] == [['alice', '2'], ['<alice>', '1'], ['Bob', '1'], ['bob', '1']]
    assert '&lt;alice&gt;' in response['body']
    assert soup.select_one('a[data-album-link][href^="/bot/albums/42?"]') is not None
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize('storage', ['missing', 'legacy', 'empty', 'unattributed'])
def test_empty_generation_leaderboard(page, storage):
    cache, request = page
    if storage == 'legacy':
        with sqlite3.connect(cache.database) as db:
            db.execute('CREATE TABLE legacy (id INTEGER)')
    elif storage == 'empty':
        MusicLibrary(cache.database).create_album('Band', 'Album')
        with closing(cache.connect()):
            pass
    elif storage == 'unattributed':
        insert(cache)
        with sqlite3.connect(cache.database) as db:
            db.execute('UPDATE image_cache SET requested_by = NULL')
    assert cache.generation_leaderboard() == []
    response = request('/')
    assert response['status'] == 200
    assert 'leaderboard-heading' not in response['body']


def test_empty_homepage_does_not_create_storage(page):
    cache, request = page
    response = request('/')
    assert response['status'] == 200
    assert 'No albums yet.' in response['body']
    assert not cache.database.exists()
    assert not cache.directory.exists()


def test_homepage_storage_failure_is_clean(page):
    cache, request = page
    cache.database.write_text('not sqlite')
    response = request('/')
    assert response['status'] == 503
    assert str(cache.database) not in response['body']


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def test_homepage_rejects_writes(page, method):
    cache, request = page
    assert request('/', method=method)['status'] == 405
    assert not cache.database.exists()


def test_gallery_pagination_and_album_links(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    albums = [library.create_album('<Band>', f'Album {i}') for i in range(25)]
    with closing(cache.connect()) as db:
        db.executemany(
            '''INSERT INTO image_cache
            (cache_key, prompt, normalized_prompt, settings_json, local_filename,
             hosted_url, generation_metadata_json)
            VALUES (?, 'prompt', 'prompt', '{}', 'private.png', ?, '{}')''',
            [
                (str(album['id']), f'https://example.com/{album["id"]}.png')
                for album in albums
            ],
        )
    for album in albums:
        library.record_image(album['id'], str(album['id']))
    before = cache.database.read_bytes()
    response = request('/gallery')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    cards = soup.select('.gallery-card')
    assert len(cards) == 24
    assert [urlsplit(card['href']).path for card in cards] == [
        f'/bot/albums/{album["id"]}' for album in reversed(albums[1:])
    ]
    assert len(soup.select('.gallery img')) == 24
    assert cards[0].img['loading'] == 'lazy'
    assert cards[0].img['referrerpolicy'] == 'no-referrer'
    assert '<Band>' in cards[0].parent.get_text()
    assert not soup.select('band')
    assert (
        soup.select_one('a[rel=next]')['href']
        == '/bot/gallery?sort=chronology&order=desc&page=2'
    )
    assert not soup.select_one('a[rel=prev]')
    last = BeautifulSoup(request('/gallery?page=2')['body'], 'html.parser')
    assert len(last.select('.gallery-card')) == 1
    assert (
        urlsplit(last.select_one('.gallery-card')['href']).path
        == f'/bot/albums/{albums[0]["id"]}'
    )
    assert (
        last.select_one('a[rel=prev]')['href']
        == '/bot/gallery?sort=chronology&order=desc&page=1'
    )
    assert not last.select_one('a[rel=next]')
    assert request('/gallery?page=3')['status'] == 404
    assert request('/gallery', method='HEAD')['body'] == ''
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize(
    'url', [None, 'javascript:alert(1)', 'https://example.com/art.png']
)
@pytest.mark.parametrize(
    'thumb_url', [None, 'javascript:alert(1)', 'https://example.com/thumb.jpg']
)
def test_gallery_artwork_safety_and_latest_image(page, url, thumb_url):
    cache, request = page
    insert(cache)
    with closing(cache.connect()) as db:
        db.execute(
            '''INSERT INTO image_cache
            (cache_key, prompt, normalized_prompt, settings_json, local_filename,
             hosted_url, thumb_url, generation_metadata_json, created_at)
            VALUES ('latest', 'prompt', 'prompt', '{}', 'private.png', ?, ?, '{}', '2099-01-01')''',
            (url, thumb_url),
        )
    library = MusicLibrary(cache.database)
    library.record_image(42, 'latest')
    missing = library.create_album('Band', 'No artwork')
    before = cache.database.read_bytes()
    soup = BeautifulSoup(request('/gallery')['body'], 'html.parser')
    card = soup.select_one('a[data-album-link][href^="/bot/albums/42?"]')
    expected = viewer.safe_image_url(thumb_url) or viewer.safe_image_url(url)
    assert bool(card.img) == bool(expected)
    if card.img:
        assert card.img['src'] == expected
    else:
        assert 'Artwork unavailable' in card.get_text()
    assert not soup.select_one(
        f'a[data-album-link][href^="/bot/albums/{missing["id"]}?"]'
    ).img
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize(
    'value', ['0', '-1', '1.5', 'no', '', '１', '9' * 100, '1&page=2']
)
def test_gallery_invalid_page_does_not_create_storage(page, value):
    cache, request = page
    assert request(f'/gallery?page={value}')['status'] == 400
    assert not cache.database.exists()
    assert not cache.directory.exists()


def test_empty_gallery_does_not_create_storage(page):
    cache, request = page
    response = request('/gallery')
    assert response['status'] == 200
    assert 'No albums yet.' in response['body']
    assert request('/gallery?page=2')['status'] == 404
    assert not cache.database.exists()
    assert not cache.directory.exists()


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def test_gallery_rejects_writes(page, method):
    cache, request = page
    assert request('/gallery', method=method)['status'] == 405
    assert not cache.database.exists()


def test_gallery_without_image_schema_and_storage_error(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    library.create_album('Band', 'Unillustrated')
    before = cache.database.read_bytes()
    assert 'Artwork unavailable' in request('/gallery')['body']
    assert cache.database.read_bytes() == before
    cache.database.write_text('not sqlite')
    response = request('/gallery')
    assert response['status'] == 503
    assert str(cache.database) not in response['body']


@pytest.mark.parametrize(
    ('sort', 'expected'),
    [
        ('band', [2, 3, 1, 4]),
        ('album', [1, 3, 4, 2]),
        ('date_asc', [4, 1, 2, 3]),
        ('date_desc', [3, 2, 1, 4]),
        ('genre', [3, 2, 1, 4]),
    ],
)
def test_gallery_sorting(page, sort, expected):
    cache, request = page
    library = MusicLibrary(cache.database)
    for band, title, genre in (
        ('Zulu', 'Album', 'Rock'),
        ('alpha', 'Zebra', 'rock'),
        ('alpha', 'apple', 'Jazz'),
        ('Zulu', 'Other', None),
    ):
        library.create_album(band, title, genre=genre)
    with sqlite3.connect(cache.database) as db:
        for album_id, timestamp in enumerate(
            ['2026-01-02', '2026-01-03', '2026-01-03', '2026-01-01'], start=1
        ):
            db.execute(
                'UPDATE albums SET created_at = ? WHERE id = ?', (timestamp, album_id)
            )
    before = cache.database.read_bytes()
    response = request(f'/gallery?sort={sort}')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert [urlsplit(card['href']).path for card in soup.select('.gallery-card')] == [
        f'/bot/albums/{album_id}' for album_id in expected
    ]
    select = soup.select_one('select[name=sort]')
    assert select.select_one('option[selected]')['value'] == ':'.join(
        viewer.GALLERY_SORTS.get(sort, (sort, 'asc'))
    )
    assert [option.get_text() for option in select.select('option')] == [
        label for _, label in viewer.SORT_OPTIONS
    ]
    form = soup.select_one('form.catalog-controls')
    assert form['method'] == 'get'
    assert form['action'] == '/bot/gallery'
    assert not form.select('input[name=page]')
    artwork = soup.select('.gallery-artwork')
    if sort == 'genre':
        assert [thumbnail['title'] for thumbnail in artwork] == [
            'Genre: Jazz',
            'Genre: rock',
            'Genre: Rock',
            'Genre: No genre',
        ]
    else:
        assert all('title' not in thumbnail.attrs for thumbnail in artwork)
    assert cache.database.read_bytes() == before


def test_gallery_sort_preserved_between_pages(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    for i in range(25):
        library.create_album(f'Band {i:02}', 'Album')
    first = BeautifulSoup(request('/gallery?sort=band')['body'], 'html.parser')
    next_url = first.select_one('a[rel=next]')['href']
    assert next_url == '/bot/gallery?sort=artist&order=asc&page=2'
    second = BeautifulSoup(request(next_url[len('/bot') :])['body'], 'html.parser')
    assert second.select_one('option[selected]')['value'] == 'artist:asc'
    assert urlsplit(second.select_one('.gallery-card')['href']).path == '/bot/albums/25'
    assert (
        second.select_one('a[rel=prev]')['href']
        == '/bot/gallery?sort=artist&order=asc&page=1'
    )


@pytest.mark.parametrize(
    'sort', ['', 'invalid', 'genre&sort=band', 'id%3BDROP+TABLE+albums']
)
def test_gallery_invalid_sort_does_not_create_storage(page, sort):
    cache, request = page
    assert request(f'/gallery?sort={sort}')['status'] == 400
    assert not cache.database.exists()
    assert not cache.directory.exists()


def test_preparation_is_immutable_and_has_no_generation(page, monkeypatch):
    monkeypatch.setattr(
        auth,
        'current_user',
        Mock(return_value=User(1, 'Alice', 'alice', None, '2026', True, False)),
    )
    cache, request = page
    insert(cache)
    before = cache.database.read_bytes()
    values = {
        'source_image_id': '42',
        'format': '',
        'format_description': '',
        'genre': '',
        'artist_genre': '',
        'description': '<b>new interpretation</b>',
        'artist_description': 'Band interpretation',
    }
    response = request(method='POST', data=values)
    assert response['status'] == 409
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert 'disabled' in soup.select_one('[role=alert]').get_text()
    assert soup.select_one('[name=description]').get_text() == values['description']
    assert not soup.select('form pre, form [name=prompt], form b')
    assert cache.database.read_bytes() == before
    for changes in (
        {'genre': 'invented choice'},
        {'title': 'rename'},
        {'source_image_id': '999'},
        {'description': 'x' * 10001},
    ):
        assert request(method='POST', data=dict(values, **changes))['status'] in (
            400,
            404,
        )
    assert request(method='POST')['status'] == 400
    assert cache.database.read_bytes() == before


def test_shared_generation_inputs(page):
    from pmxbot.music import GenerationInputs

    cache, _ = page
    insert(cache)
    album, image = cache.get_album_page(42)
    source = GenerationInputs.from_source(album, image)
    values = {field: getattr(source, field) for field in source.creative_fields}
    draft = source.prepare(dict(values, artist_description='Artist visual cue'), {})
    assert draft.source_image_id == 42
    assert draft.album_id == album['id']
    assert draft.artist_id == album['artist_id']
    assert source.artist_description == ''
    assert draft.album_properties()['artist_description'] == 'Artist visual cue'
    with pytest.raises(ValueError):
        source.prepare(dict(values, description=['duplicate']), {})


def test_legacy_values_prepare_and_selected_source_stays_fixed(page, monkeypatch):
    monkeypatch.setattr(
        auth,
        'current_user',
        Mock(return_value=User(1, 'Alice', 'alice', None, '2026', True, False)),
    )
    cache, request = page
    insert(cache)
    with sqlite3.connect(cache.database) as db:
        db.execute("UPDATE albums SET format = 'Retired format' WHERE id = 42")
        db.execute("UPDATE artists SET genre = 'Retired genre'")
        db.execute(
            """INSERT INTO image_cache
            (id, cache_key, prompt, normalized_prompt, settings_json,
             local_filename, generation_metadata_json, created_at)
            VALUES (43, 'newer', 'do not parse', 'normalized', '{}', 'new.png', '{}', '2099-01-01')"""
        )
    MusicLibrary(cache.database).record_image(42, 'newer')
    before = cache.database.read_bytes()
    soup = BeautifulSoup(
        request('/albums/42?source_image_id=42')['body'], 'html.parser'
    )
    form = soup.select_one('#variation-form')
    values = {
        control['name']: control.get('value', control.get_text())
        for control in form.select('input, textarea')
    }
    for control in form.select('select'):
        values[control['name']] = control.select_one('option[selected]')['value']
    assert values['source_image_id'] == '42'
    assert values['format'] == 'Retired format'
    assert values['artist_genre'] == 'Retired genre'
    response = request(method='POST', data=values)
    assert response['status'] == 409
    assert (
        BeautifulSoup(response['body'], 'html.parser').select_one(
            '[name=source_image_id]'
        )['value']
        == '42'
    )
    assert 'disabled' in response['body']
    assert cache.database.read_bytes() == before


def test_selected_variation_restores_generation_input_defaults(page):
    cache, request = page
    insert(cache)
    generation_inputs = {
        'format': 'Vinyl',
        'format_description': 'Remastered',
        'genre': 'Jazz',
        'artist_genre': 'Alternative',
        'description': 'Original album description',
        'artist_description': 'Original band description',
    }
    with sqlite3.connect(cache.database) as db:
        db.execute(
            '''UPDATE image_cache SET generation_metadata_json = ?
            WHERE id = 42''',
            (json.dumps({'generation_inputs': generation_inputs}),),
        )
        db.execute(
            """UPDATE albums SET format = 'CD', format_description = 'Album',
            genre = 'Pop', description = 'Current album description' WHERE id = 42"""
        )
        db.execute(
            """UPDATE artists SET genre = 'Rock', description = 'Current band description'"""
        )

    response = request('/albums/42?source_image_id=42')
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    for name in ('format', 'format_description', 'genre', 'artist_genre'):
        selected = soup.select_one(f'select[name="{name}"] option[selected]')
        assert selected['value'] == generation_inputs[name]
    assert soup.select_one('[name="description"]').get_text() == (
        generation_inputs['description']
    )
    assert soup.select_one('[name="artist_description"]').get_text() == (
        generation_inputs['artist_description']
    )


@pytest.mark.parametrize(
    ('sort', 'order', 'expected'),
    [
        ('chronology', 'desc', [3, 2, 1]),
        ('chronology', 'asc', [1, 2, 3]),
        ('artist', 'asc', [2, 3, 1]),
        ('artist', 'desc', [1, 3, 2]),
        ('album', 'asc', [1, 3, 2]),
        ('album', 'desc', [2, 3, 1]),
    ],
)
def test_catalog_sort(page, sort, order, expected):
    cache, request = page
    library = MusicLibrary(cache.database)
    for artist, title in [('Zulu', 'Apple'), ('alpha', 'Zoo'), ('alpha', 'Apple')]:
        library.create_album(artist, title)
    with sqlite3.connect(cache.database) as db:
        db.execute("UPDATE albums SET created_at = '2026-01-01'")
    soup = BeautifulSoup(request(f'/?sort={sort}&order={order}')['body'], 'html.parser')
    assert [
        urlsplit(link['href']).path for link in soup.select('.catalog-album a')
    ] == [f'/bot/albums/{value}' for value in expected]


@pytest.mark.parametrize(
    ('query', 'expected'),
    [
        ('q=APPLE', [3, 1]),
        ('q=ALPHA', [3, 2]),
        ('q=+++', [3, 2, 1]),
        ('album_title=Apple', [3, 1]),
        ('artist=alpha', [3, 2]),
        ('artist=Alpha', [3, 2]),
        ('q=%25', []),
        ('q=%27+OR+1%3D1', []),
    ],
)
def test_catalog_filters(page, query, expected):
    cache, request = page
    library = MusicLibrary(cache.database)
    for artist, title in [('Zulu', 'Apple'), ('alpha', 'Zoo'), ('alpha', 'Apple')]:
        library.create_album(artist, title)
    soup = BeautifulSoup(request('/?' + query)['body'], 'html.parser')
    assert [
        urlsplit(link['href']).path for link in soup.select('.catalog-album a')
    ] == [f'/bot/albums/{value}' for value in expected]
    if not expected:
        assert 'No albums match' in soup.get_text()


def test_catalog_filter_links_and_pagination(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    artist, title = '<B & / + ü>', '<A & / + ü>'
    for index in range(25):
        library.create_album(artist, title + str(index))
    library.create_album('Other', title + '0')
    soup = BeautifulSoup(request('/albums/1')['body'], 'html.parser')
    links = soup.select('h1 a')
    assert [link['href'] for link in links] == [
        '/bot/?' + urlencode({'artist': artist}),
        '/bot/?' + urlencode({'album_title': title + '0'}),
    ]
    title_results = BeautifulSoup(request(links[1]['href'][4:])['body'], 'html.parser')
    assert len(title_results.select('.catalog-album')) == 2
    query = urlencode(dict(q='<A', artist=artist, sort='album', order='asc'))
    first = BeautifulSoup(request('/?' + query)['body'], 'html.parser')
    assert len(first.select('.catalog-album')) == 24
    next_url = first.select_one('[rel=next]')['href']
    assert next_url.startswith('/bot/?')

    assert parse_qs(urlsplit(next_url).query) == dict(
        q=['<A'], artist=[artist], sort=['album'], order=['asc'], page=['2']
    )
    second = BeautifulSoup(request(next_url[4:])['body'], 'html.parser')
    assert len(second.select('.catalog-album')) == 1
    assert not second.select_one('[rel=next]')
    assert second.select_one('[rel=prev]')
    assert not first.select('b, aü')
    assert first.select_one('[name=artist]')['value'] == artist
    assert first.select_one('form.catalog-controls')['action'] == '/bot/'
    assert request('/?' + query + '&page=3')['status'] == 404


@pytest.mark.parametrize(
    'query',
    [
        'page=0',
        'page=-1',
        'page=abc',
        'page=',
        'page=1&page=2',
        'page=' + '9' * 100,
        'sort=bad',
        'order=bad',
        'q=a&q=b',
        'unknown=a',
    ],
)
def test_catalog_invalid_parameters(page, query):
    cache, request = page
    assert request('/?' + query)['status'] == 400
    assert not cache.database.exists()


def test_catalog_artwork_and_unicode(page):
    cache, request = page
    insert(cache)
    library = MusicLibrary(cache.database)
    library.create_album('Straße', 'No artwork')
    library.create_album('Other', 'No artwork')
    with closing(cache.connect()) as db:
        db.execute(
            """INSERT INTO image_cache
            (cache_key, prompt, normalized_prompt, settings_json, local_filename,
             hosted_url, generation_metadata_json, created_at)
            VALUES ('new', '', '', '{}', 'private.png', 'javascript:bad', '{}', '2099')"""
        )
    library.record_image(42, 'new')
    before = cache.database.read_bytes()
    soup = BeautifulSoup(request('/')['body'], 'html.parser')
    assert len(soup.select('.catalog-album')) == 3
    assert not soup.select('.catalog img')
    assert 'Straße' in request('/?q=STRASSE')['body']
    assert cache.database.read_bytes() == before


def test_catalog_existing_artwork(page):
    cache, request = page
    insert(cache)
    soup = BeautifulSoup(request('/')['body'], 'html.parser')
    assert len(soup.select('.catalog-album')) == 1
    assert not soup.select('.catalog img')
    assert soup.select_one('.catalog-variations').get_text() == '1'


@pytest.mark.parametrize('order', ['asc', 'desc'])
def test_catalog_tied_pagination_and_query_count(page, monkeypatch, order):
    cache, request = page
    library = MusicLibrary(cache.database)
    albums = [library.create_album('Band', f'Title {index}') for index in range(49)]
    with sqlite3.connect(cache.database) as db:
        db.execute("UPDATE albums SET created_at = '2026-01-01'")
    statements = []
    original = ImageCache.read_connection

    def traced_connection(self):
        db = original(self)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(ImageCache, 'read_connection', traced_connection)
    ids = []
    for number in range(1, 4):
        soup = BeautifulSoup(
            request(f'/?order={order}&page={number}')['body'], 'html.parser'
        )
        rows = soup.select('.catalog-album a')
        assert len(rows) == (24 if number < 3 else 1)
        ids.extend(int(urlsplit(row['href']).path.rsplit('/', 1)[1]) for row in rows)
    expected = [album['id'] for album in albums]
    assert ids == (expected if order == 'asc' else expected[::-1])
    # Schema, count, result and leaderboard schema queries: constant per page.
    assert len([sql for sql in statements if sql.startswith('SELECT')]) == 12


def test_catalog_creation_dates_precede_ids(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    library.create_album('Band', 'Newest')
    library.create_album('Band', 'Oldest')
    with sqlite3.connect(cache.database) as db:
        db.execute(
            "UPDATE albums SET created_at = CASE id WHEN 1 THEN '2026-02-01' ELSE '2026-01-01' END"
        )
    soup = BeautifulSoup(request('/')['body'], 'html.parser')
    assert [urlsplit(row['href']).path for row in soup.select('.catalog-album a')] == [
        '/bot/albums/1',
        '/bot/albums/2',
    ]


@pytest.fixture
def variation(page, monkeypatch, tmp_path):
    monkeypatch.setattr(
        auth,
        'current_user',
        Mock(return_value=User(1, 'Alice', 'alice', None, '2026', True, False)),
    )
    import base64
    from pmxbot import images

    cache, request = page
    insert(cache)
    pmxbot.config.update(
        images_enabled=True,
        openai_api_key='test',
        r2_endpoint_url='https://r2.example.com',
        r2_public_url='https://images.example.com',
        r2_bucket='images',
        r2_access_key_id='test',
        r2_secret_access_key='test',
    )
    source = tmp_path / 'source.png'
    source.write_bytes(PNG)
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE image_cache SET local_filename = ? WHERE id = 42', (str(source),)
        )
    monkeypatch.setattr(ImageCache, 'generate', ORIGINAL_GENERATE)
    monkeypatch.setattr(ImageCache, 'upload', ORIGINAL_UPLOAD)
    response = Mock()
    response.json.return_value = {
        'data': [{'b64_json': base64.b64encode(PNG).decode()}]
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(images.requests, 'post', post)
    client = Mock()
    client.put_object.return_value = {'ETag': 'test'}
    monkeypatch.setattr(images.boto3, 'client', Mock(return_value=client))
    values = dict(
        source_image_id='42',
        format='',
        format_description='',
        genre='',
        artist_genre='',
        description='new <art>\nexact cue',
        artist_description='Band cue',
    )
    return cache, request, post, client, values, source


def enable_responses(cache, post):
    import base64

    pmxbot.config.update(image_api='responses', responses_model='gpt-5')
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE image_cache SET generation_metadata_json = ? WHERE id = 42',
            (json.dumps(dict(api='responses', response_id='resp_source', store=True)),),
        )
    post.return_value.json.return_value = dict(
        id='resp_child',
        status='completed',
        model='gpt-5',
        store=True,
        output=[
            dict(
                type='image_generation_call',
                id='ig_child',
                status='completed',
                result=base64.b64encode(PNG).decode(),
            )
        ],
    )


def complete_variation(variation, data=None):
    from pmxbot.jobs import JobStore, process_job

    cache, request, post, client, values, source = variation
    calls = post.call_count, client.put_object.call_count
    response = request(method='POST', data=data or values)
    assert response['status'] == 303
    assert calls == (post.call_count, client.put_object.call_count)
    identifier = response['headers']['Location'].rsplit('/', 1)[1]
    store = JobStore(cache.database)
    job = store.claim()
    assert job['id'] == identifier
    process_job(store, job, pmxbot.config)
    result = store.get(identifier)
    assert result['status'] == 'succeeded'
    response['headers'][
        'Location'
    ] = f"/bot/albums/42?source_image_id={result['result_image_id']}"
    return response


@pytest.mark.parametrize('api', ['images', 'responses'])
def test_generate_variation_prg_and_provenance(variation, api):
    cache, request, post, client, values, source = variation
    if api == 'responses':
        enable_responses(cache, post)
        source.unlink()  # Conversation continuation never opens the source bitmap.
    else:

        def image_post(*args, **kwargs):
            assert kwargs['files']['image[]'][1].read() == source.read_bytes()
            return post.return_value

        post.side_effect = image_post
    original = cache.get_image(42)
    with sqlite3.connect(cache.database) as db:
        canonical = (
            db.execute('SELECT * FROM albums').fetchall(),
            db.execute('SELECT * FROM artists').fetchall(),
        )
    capability = BeautifulSoup(request()['body'], 'html.parser')
    assert not capability.select_one(
        '#variation-form button[type=submit][value=edit]'
    ).has_attr('disabled')
    result = complete_variation(variation)
    assert result['status'] == 303
    target = result['headers']['Location']
    child_id = int(target.rsplit('=', 1)[1])
    assert child_id != 42
    child = cache.get_image(child_id)
    sent = post.call_args.kwargs['json' if api == 'responses' else 'data']
    assert (sent['tools'][0] if api == 'responses' else sent)['moderation'] == 'low'
    assert child['prompt'] == sent['input' if api == 'responses' else 'prompt']
    assert values['description'] in child['prompt']
    assert 'Band cue' in child['prompt']
    assert (
        'Do not literally add the album description as text to the image.'
        in child['prompt']
    )
    assert child['parent_image_id'] == 42
    meta = json.loads(child['generation_metadata_json'])
    assert meta['generation_inputs']['description'] == values['description']
    assert meta['generation_inputs']['source_image_id'] == 42
    assert meta['api'] == api
    if api == 'responses':
        assert sent['previous_response_id'] == 'resp_source'
        assert 'files' not in post.call_args.kwargs
        assert meta['response_id'] == 'resp_child'
    else:
        assert post.call_args.args[0].endswith('/images/edits')
    assert cache.get_image(42) == original
    with sqlite3.connect(cache.database) as db:
        assert canonical == (
            db.execute('SELECT * FROM albums').fetchall(),
            db.execute('SELECT * FROM artists').fetchall(),
        )
        assert db.execute(
            'SELECT album_id FROM album_images WHERE cache_key = ?',
            (child['cache_key'],),
        ).fetchone() == (42,)
    for _ in range(2):
        page = BeautifulSoup(request(target[len('/bot') :])['body'], 'html.parser')
        assert page.select_one('[name=description]').get_text() == values['description']
        assert child['prompt'] in page.get_text()
        assert str(child_id) in page.get_text()
    assert post.call_count == 1
    assert client.put_object.call_count == 2


@pytest.mark.parametrize(
    'case', ['disabled', 'legacy', 'missing_file', 'invalid', 'csrf', 'mismatch']
)
def test_variation_rejected_without_provider(variation, case):
    cache, request, post, client, values, source = variation
    if case == 'disabled':
        pmxbot.config['images_enabled'] = False
    elif case == 'legacy':
        pmxbot.config.update(image_api='responses', responses_model='gpt-5')
    elif case == 'missing_file':
        source.unlink()
    elif case == 'invalid':
        values['genre'] = 'not a configured genre'
    elif case == 'csrf':
        values['csrf_token'] = 'invalid'
    else:
        values['source_image_id'] = '999'
    original = cache.get_image(42)
    result = request(method='POST', data=values)
    assert result['status'] in (400, 403, 404, 409)
    post.assert_not_called()
    client.put_object.assert_not_called()
    assert cache.get_image(42) == original


@pytest.mark.parametrize('failure', ['api', 'upload', 'storage', 'expired_context'])
def test_variation_failures_redirect_and_preserve_source(variation, failure):
    from pmxbot import images

    cache, request, post, client, values, source = variation
    original = cache.get_image(42)
    if failure == 'expired_context':
        enable_responses(cache, post)
        original = cache.get_image(42)
        post.side_effect = images.requests.exceptions.RequestException(
            'secret provider detail'
        )
    elif failure == 'api':
        post.side_effect = images.requests.exceptions.RequestException(
            'secret provider detail'
        )
    elif failure == 'upload':
        client.put_object.side_effect = images.BotoCoreError()
    else:
        with sqlite3.connect(cache.database) as db:
            db.execute(
                "CREATE TRIGGER fail_link BEFORE INSERT ON album_images BEGIN SELECT RAISE(ABORT, 'secret storage detail'); END"
            )
    from pmxbot.jobs import JobStore, process_job

    result = request(method='POST', data=values)
    assert result['status'] == 303
    target = result['headers']['Location']
    store = JobStore(cache.database)
    process_job(store, store.claim(), pmxbot.config)
    job = store.get(target.rsplit('/', 1)[1])
    assert job['status'] == 'failed'
    assert job['result_image_id'] is None
    page = request(target[len('/bot') :])
    assert 'Generation failed' in page['body']
    assert 'secret ' not in page['body']
    assert cache.get_image(42) == original
    with sqlite3.connect(cache.database) as db:
        assert db.execute('SELECT count(*) FROM image_cache').fetchone()[0] == (
            2 if failure == 'upload' else 1
        )
        assert db.execute('SELECT count(*) FROM album_images').fetchone()[0] == (
            2 if failure == 'upload' else 1
        )
    request('/albums/42?source_image_id=42')
    assert post.call_count == 1
    assert client.put_object.call_count == (1 if failure == 'upload' else 0)


def test_responses_variations_branch_from_selected_image(variation):
    cache, request, post, client, values, source = variation
    enable_responses(cache, post)
    first = complete_variation(variation)
    first_id = int(first['headers']['Location'].rsplit('=', 1)[1])
    post.return_value.json.return_value['id'] = 'resp_second'
    second = complete_variation(variation, dict(values, source_image_id=str(first_id)))
    second_id = int(second['headers']['Location'].rsplit('=', 1)[1])
    assert post.call_args.kwargs['json']['previous_response_id'] == 'resp_child'
    post.return_value.json.return_value['id'] = 'resp_branch'
    branch = complete_variation(variation)
    branch_id = int(branch['headers']['Location'].rsplit('=', 1)[1])
    assert post.call_args.kwargs['json']['previous_response_id'] == 'resp_source'
    assert len({42, first_id, second_id, branch_id}) == 4
    assert cache.get_image(second_id)['parent_image_id'] == first_id
    assert cache.get_image(branch_id)['parent_image_id'] == 42
    assert all('files' not in call.kwargs for call in post.call_args_list)


def test_variation_source_must_belong_to_album(variation):
    cache, request, post, client, values, source = variation
    other = MusicLibrary(cache.database).create_album('Other', 'Release')
    assert (
        request(f"/albums/{other['id']}", method='POST', data=values)['status'] == 404
    )
    post.assert_not_called()


def test_images_variation_hosting_retry_never_regenerates(variation):
    from pmxbot import images

    cache, request, post, client, values, source = variation
    client.put_object.side_effect = images.BotoCoreError()
    assert request(method='POST', data=values)['status'] == 303
    from pmxbot.jobs import JobStore, process_job

    store = JobStore(cache.database)
    process_job(store, store.claim(), pmxbot.config)
    _, child = cache.get_album_page(42)
    assert child['id'] != 42
    client.put_object.side_effect = None
    ready = ImageCache(pmxbot.config).retry_upload(child['id'])
    assert ready['hosted_url']
    assert ready['prompt'] == child['prompt']
    assert ready['generation_metadata_json'] == child['generation_metadata_json']
    assert post.call_count == 1
    from pathlib import Path

    Path(ready['local_filename']).unlink()
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE image_cache SET hosted_url = NULL WHERE id = ?', (child['id'],)
        )
    with pytest.raises(ImageError, match='immutable artwork'):
        ImageCache(pmxbot.config).retry_upload(child['id'])
    assert post.call_count == 1


@pytest.mark.parametrize(
    'case', ['unstored', 'missing_response_id', 'unsupported_model']
)
def test_variation_capability_disables_unsupported_source(variation, case):
    cache, request, post, client, values, source = variation
    if case == 'unsupported_model':
        pmxbot.config['images_model'] = 'dall-e-3'
    else:
        enable_responses(cache, post)
        metadata = dict(api='responses', store=case != 'unstored')
        if case == 'unstored':
            metadata['response_id'] = 'resp_source'
        with sqlite3.connect(cache.database) as db:
            db.execute(
                'UPDATE image_cache SET generation_metadata_json = ? WHERE id = 42',
                (json.dumps(metadata),),
            )
    soup = BeautifulSoup(request()['body'], 'html.parser')
    assert soup.select_one('#variation-form button[type=submit][value=edit]').has_attr(
        'disabled'
    )
    assert request(method='POST', data=values)['status'] == 409
    post.assert_not_called()


def test_variation_file_save_failure_is_safe(variation, monkeypatch):
    cache, request, post, client, values, source = variation
    before = cache.get_image(42)
    monkeypatch.setattr(
        ImageCache, 'save', Mock(side_effect=OSError('secret disk detail'))
    )
    from pmxbot.jobs import JobStore, process_job

    submitted = request(method='POST', data=values)
    assert submitted['status'] == 303
    store = JobStore(cache.database)
    process_job(store, store.claim(), pmxbot.config)
    result = request(submitted['headers']['Location'][len('/bot') :])
    assert 'Generation failed' in result['body']
    assert 'secret disk detail' not in result['body']
    assert cache.get_image(42) == before
    with sqlite3.connect(cache.database) as db:
        assert db.execute('SELECT count(*) FROM image_cache').fetchone() == (1,)
        assert db.execute('SELECT count(*) FROM album_images').fetchone() == (1,)
    client.put_object.assert_not_called()


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_artwork_selection_fragments(page):
    cache, request = page
    insert(cache, url='javascript:alert(1)')
    before = cache.database.read_bytes()
    response = request('/albums/42?source_image_id=42', accept='application/json')
    assert response['status'] == 200
    data = json.loads(response['body'])
    assert data['image_id'] == '42'
    parts = data['fragments']
    assert set(parts) == {'artwork', 'prompt', 'metadata', 'cache_key'}
    assert 'javascript:' not in parts['artwork']
    assert 'Hosted artwork is unavailable' in parts['artwork']
    assert '<script>' not in parts['prompt']
    assert BeautifulSoup(parts['prompt'], 'html.parser').get_text() == (
        '  exact <script> & "prompt"\nsecond line  '
    )
    assert set(data['properties']) == set(viewer.GenerationInputs.creative_fields)
    assert data['variation_available'] is False
    assert '/private/secret' not in response['body']
    assert cache.database.read_bytes() == before
    assert (
        request('/albums/42?source_image_id=999', accept='application/json')['status']
        == 404
    )


@pytest.mark.parametrize('identifier', [42, 2**53 + 1, 2**63 - 1])
def test_artwork_selection_preserves_image_id(page, identifier):
    cache, request = page
    insert(cache, image_id=identifier)
    response = request(
        '/albums/42?source_image_id=' + str(identifier), accept='application/json'
    )
    assert response['status'] == 200
    assert json.loads(response['body'])['image_id'] == str(identifier)


@pytest.mark.parametrize('sort', viewer.COLLECTION_SORTS)
@pytest.mark.parametrize('order', ['asc', 'desc'])
def test_collection_round_trip(page, sort, order):
    cache, request = page
    library = MusicLibrary(cache.database)
    for number in range(30):
        library.create_album('Mazel Vatos', f'Release {number:02}', genre='Jazz')
    library.create_album('Other', 'Release 99')
    query = urlencode(dict(q='Release', artist='vAt', sort=sort, order=order, page=2))
    catalog = BeautifulSoup(request('/?' + query)['body'], 'html.parser')
    gallery_url = catalog.select_one('.view-switch a[href*="/gallery"]')['href']
    gallery = BeautifulSoup(request(gallery_url[4:])['body'], 'html.parser')
    assert len(gallery.select('.gallery-card')) == 6
    assert [urlsplit(a['href']).path for a in catalog.select('.catalog-album a')] == [
        urlsplit(a['href']).path for a in gallery.select('.gallery-card')
    ]
    assert gallery.select_one('.view-switch [aria-current]').get_text() == 'Gallery'
    list_url = gallery.select_one('.view-switch a:not([aria-current])')['href']
    assert parse_qs(urlsplit(list_url).query) == parse_qs(query)
    assert parse_qs(
        urlsplit(gallery.select_one('.pagination [rel=prev]')['href']).query
    )['artist'] == ['vAt']
    assert not gallery.select('form.catalog-controls input[name=page]')
    assert parse_qs(urlsplit(catalog.select_one('.catalog th a')['href']).query)[
        'page'
    ] == ['1']


@pytest.mark.parametrize('view', ['/', '/gallery'])
def test_collection_chips_and_header(page, view):
    cache, request = page
    insert(cache)
    query = urlencode(
        dict(q='Band', artist='Band', album_title='Album', sort='album', order='asc')
    )
    soup = BeautifulSoup(request(view + '?' + query)['body'], 'html.parser')
    chips = soup.select('.filter-chips a')
    assert len(chips) == 4
    for field, chip in zip(('q', 'artist', 'album_title'), chips):
        values = parse_qs(urlsplit(chip['href']).query)
        assert field not in values
        assert values['page'] == ['1']
        assert values['sort'] == ['album']
        assert values['order'] == ['asc']
    assert not set(parse_qs(urlsplit(chips[-1]['href']).query)) & {
        'q',
        'artist',
        'album_title',
    }
    assert not soup.select('.signin-card, input[name=password]')
    header = soup.select_one('.site-header')
    assert header.select_one('.site-brand a')['href'] == '/bot/'
    sign_in = header.select_one('a[href*="/login?"]')['href']
    assert parse_qs(urlsplit(sign_in).query)['return_to'] == [
        '/bot' + view + '?' + query
    ]
    if view == '/':
        assert soup.select_one('.catalog').find_next('section', class_='leaderboard')


@pytest.mark.parametrize('page', ['', '/bot'], indirect=True)
def test_album_return_context_and_fallback(page):
    cache, request = page
    insert(cache)
    base = pmxbot.config.web_base
    target = (
        base
        + '/gallery?'
        + urlencode(
            dict(
                q='Mazel Vatos ü',
                artist='Mazel Vatos',
                album_title='Album',
                sort='album',
                order='desc',
                page=2,
            )
        )
        + '#scroll=850'
    )
    response = request('/albums/42?' + urlencode({'return_to': target}))
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert soup.select_one('#back-to-results')['href'] == target
    assert soup.select_one('.results-navigation a:last-child')['href'] == base + '/'
    assert parse_qs(
        urlsplit(soup.select_one('.image-history a[data-image-id]')['href']).query
    )['return_to'] == [target]
    sign_in = soup.select_one('.site-header a[href*="/login?"]')['href']
    login = BeautifulSoup(request(sign_in[len(base) :])['body'], 'html.parser')
    assert login.select_one('input[name=return_to]')[
        'value'
    ] == base + '/albums/42?' + urlencode({'return_to': target})
    direct = BeautifulSoup(request('/albums/42')['body'], 'html.parser')
    assert direct.select_one('#back-to-results')['href'] == base + '/'


@pytest.mark.parametrize(
    'target',
    [
        'https://evil.example/',
        '//evil.example/',
        '/bot/../gallery',
        '/bot/%2fgallery',
        '/bot/gallery/extra',
        '/bot/login',
        '/bot/gallery?unknown=1',
        '/bot/gallery?page=0',
        '/bot/gallery?sort=bad',
        '/bot/gallery?q=x&q=y',
        '/bot/gallery#scroll=evil',
        '/bot/gallery\r\n',
        '/bot/gallery?order=bad',
        '/bot/gallery?page=1&page=2',
        '/bot/gallery?sort=band&order=asc#scroll=123456789',
        '/bot/\\evil',
    ],
)
def test_album_unsafe_results_return(page, target):
    cache, request = page
    insert(cache)
    soup = BeautifulSoup(
        request('/albums/42?' + urlencode({'return_to': target}))['body'], 'html.parser'
    )
    assert soup.select_one('#back-to-results')['href'] == '/bot/'


@pytest.mark.parametrize('path', ['/', '/gallery', '/albums/42'])
def test_shared_signed_in_header(page, monkeypatch, path):
    cache, request = page
    insert(cache)
    monkeypatch.setattr(
        auth,
        'current_user',
        Mock(return_value=User(1, 'Alice', 'alice', None, '2026', True, False)),
    )
    soup = BeautifulSoup(request(path)['body'], 'html.parser')
    header = soup.select_one('.site-header')
    assert 'Hi, Alice.' in header.get_text()
    assert header.select_one('a[href="/bot/"]').get_text().strip()
    assert header.select_one('form[action="/bot/logout"] input[name=csrf_token]')
    assert not header.select('a[href*="/login?"]')


def test_collection_without_thumbnail_column(page):
    cache, request = page
    insert(cache)
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'CREATE TABLE legacy_images AS SELECT id, cache_key, hosted_url, created_at, requested_by FROM image_cache'
        )
        db.execute('DROP TABLE image_cache')
        db.execute('ALTER TABLE legacy_images RENAME TO image_cache')
    before = cache.database.read_bytes()
    assert request('/')['status'] == 200
    soup = BeautifulSoup(request('/gallery')['body'], 'html.parser')
    assert soup.select_one('.gallery-card img')['src'].startswith(
        'https://albums.example/'
    )
    assert cache.database.read_bytes() == before


def test_collection_all_filters_propagate(page):
    cache, request = page
    insert(cache)
    query = urlencode(
        dict(
            q='band',
            artist='<Band>',
            album_title='<Album>',
            sort='variations',
            order='desc',
        )
    )
    catalog = BeautifulSoup(request('/?' + query)['body'], 'html.parser')
    gallery_url = catalog.select_one('.view-switch a:not([aria-current])')['href']
    gallery = BeautifulSoup(request(gallery_url[4:])['body'], 'html.parser')
    assert len(gallery.select('.gallery-card')) == 1
    assert parse_qs(
        urlsplit(gallery.select_one('.view-switch a:not([aria-current])')['href']).query
    ) == dict(parse_qs(query), page=['1'])


@pytest.mark.parametrize(
    'view, selector', [('/', '.catalog-album a'), ('/gallery', '.gallery-card')]
)
@pytest.mark.parametrize(
    'filters, expected',
    [
        ({'artist': 'AZEL'}, [2, 1]),
        ({'album_title': 'NIGHT'}, [5, 4, 2, 1]),
        ({'artist': 'azel', 'album_title': 'MOV'}, [1]),
        ({'artist': 'STRASS'}, [4]),
        ({'album_title': '_100%'}, [4]),
        ({'artist': '%'}, []),
        ({'album_title': "' OR 1=1"}, []),
        ({'artist': '  vAtOs  '}, [1]),
        ({'album_title': '  moves  '}, [1]),
        ({'artist': '   ', 'album_title': '  '}, [5, 4, 3, 2, 1]),
    ],
)
def test_collection_partial_artist_and_title_search(
    page, view, selector, filters, expected
):
    cache, request = page
    library = MusicLibrary(cache.database)
    for artist, title in [
        ('Mazel Vatos', 'Night Moves'),
        ('Mazel Orchestra', 'Midnight Blues'),
        ('Other', 'Mazel Diary'),
        ('Straße', 'Night_100%'),
        ('Else', 'NightX100Z'),
    ]:
        library.create_album(artist, title)
    with sqlite3.connect(cache.database) as db:
        db.execute("UPDATE albums SET created_at = '2026-01-01'")
    response = request(view + '?' + urlencode(filters))
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert [urlsplit(a['href']).path for a in soup.select(selector)] == [
        f'/bot/albums/{identifier}' for identifier in expected
    ]
    for field, value in filters.items():
        assert soup.select_one(f'[name={field}]')['value'] == value.strip()


@pytest.mark.parametrize('view', ['/', '/gallery'])
@pytest.mark.parametrize('sort', viewer.COLLECTION_SORTS)
@pytest.mark.parametrize('order', ['asc', 'desc'])
def test_album_navigation_follows_filtered_collection(page, view, sort, order):
    cache, request = page
    insert(cache)
    library = MusicLibrary(cache.database)
    for number in range(30):
        album = library.create_album(
            f'Mazel Vatos {number % 2}',
            f'Release {29-number:02}',
            genre='Jazz' if number % 2 else 'Rock',
        )
        with sqlite3.connect(cache.database) as db:
            db.execute(
                'UPDATE albums SET created_at = ? WHERE id = ?',
                (f'2026-01-{number % 9 + 1:02}', album['id']),
            )
        if number % 3:
            key = f'navigation-{number}'
            with sqlite3.connect(cache.database) as db:
                db.execute(
                    "INSERT INTO image_cache (cache_key, prompt, normalized_prompt, settings_json, local_filename, generation_metadata_json) VALUES (?, '', '', '{}', '', '{}')",
                    (key,),
                )
            library.record_image(album['id'], key)
    library.create_album('Other', 'Release excluded')
    values = dict(
        q='Release', artist='vAtOs', album_title='lease', sort=sort, order=order
    )
    selector = '.catalog-album a' if view == '/' else '.gallery-card'
    first = BeautifulSoup(
        request(view + '?' + urlencode(values))['body'], 'html.parser'
    )
    second = BeautifulSoup(
        request(view + '?' + urlencode(dict(values, page=2)))['body'], 'html.parser'
    )
    links = first.select(selector) + second.select(selector)
    assert len(links) == 30
    before = cache.database.read_bytes()
    for index in (0, 12, 23, 24, 29):
        link = links[index]['href']
        soup = BeautifulSoup(request(link[4:])['body'], 'html.parser')
        navigation = soup.select_one('main > .album-navigation')
        assert navigation
        assert (
            soup.select_one('.album-order').get_text()
            == 'Filtered results · ' + dict(viewer.SORT_OPTIONS)[f'{sort}:{order}']
        )
        return_to = parse_qs(urlsplit(link).query)['return_to'][0]
        assert soup.select_one('#back-to-results')['href'] == return_to
        for relation, adjacent in [('prev', index - 1), ('next', index + 1)]:
            neighbor = navigation.select_one(f'a[rel={relation}]')
            if 0 <= adjacent < len(links):
                assert (
                    urlsplit(neighbor['href']).path
                    == urlsplit(links[adjacent]['href']).path
                )
                assert parse_qs(urlsplit(neighbor['href']).query)['return_to'] == [
                    return_to
                ]
            else:
                assert not neighbor
                assert navigation.select_one(
                    f'button[disabled][aria-label="{"Previous" if relation == "prev" else "Next"} album"]'
                )
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize('view', ['/', '/gallery'])
@pytest.mark.parametrize('total', [0, 1, 24, 25, 48, 49])
def test_collection_pagination_ranges_and_state(page, view, total):
    cache, request = page
    library = MusicLibrary(cache.database)
    for number in range(total):
        library.create_album('Mazel Vatos', f'Night Release {number:02}')
    library.create_album('Other', 'Excluded')
    values = dict(
        q='Night', artist='Mazel', album_title='Release', sort='album', order='asc'
    )
    count = max(
        1, (total + viewer.COLLECTION_PAGE_SIZE - 1) // viewer.COLLECTION_PAGE_SIZE
    )
    response = request(view + '?' + urlencode(dict(values, page=count)))
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    start = (count - 1) * viewer.COLLECTION_PAGE_SIZE + 1 if total else 0
    assert (
        soup.select_one('.result-range').get_text()
        == f'Showing {start}–{total} of {total}'
    )
    controls = soup.select('nav.pagination')
    assert len(controls) == (2 if count > 1 else 0)
    for position, navigation in zip(('top', 'bottom'), controls):
        assert navigation['aria-label'] == f'Album pages ({position})'
        assert (
            navigation.select_one('.page-count').get_text()
            == f'Page {count} of {count}'
        )
        assert not navigation.select_one('a[rel=next]')
        assert navigation.select_one('[aria-disabled=true]').get_text() == 'Next →'
        previous = navigation.select_one('a[rel=prev]')['href']
        assert parse_qs(urlsplit(previous).query) == {
            key: [str(value)] for key, value in dict(values, page=count - 1).items()
        }
        form = navigation.select_one('form')
        assert form['action'] == '/bot' + view
        assert {
            field['name']: field['value'] for field in form.select('input[type=hidden]')
        } == values
        number = form.select_one('input[name=page]')
        assert (number['min'], number['max'], number['value']) == (
            '1',
            str(count),
            str(count),
        )
        jump = request(view + '?' + urlencode(dict(values, page=1)))
        first = BeautifulSoup(jump['body'], 'html.parser')
        assert len(first.select('nav.pagination')) == 2
        assert not first.select_one('.pagination a[rel=prev]')
        assert (
            first.select_one('.pagination [aria-disabled=true]').get_text()
            == '← Previous'
        )
        assert parse_qs(
            urlsplit(first.select_one('.pagination a[rel=next]')['href']).query
        )['page'] == ['2']
    if count > 1:
        results = soup.select_one('.catalog' if view == '/' else '.gallery')
        assert controls[0].find_next('table' if view == '/' else 'ul') == results
        assert results.find_next('nav', class_='pagination') == controls[1]
    assert not soup.select_one('form.catalog-controls [name=page]')
    assert (
        request(view + '?' + urlencode(dict(values, page=count + 1)))['status'] == 404
    )


@pytest.mark.parametrize('view', ['/', '/gallery'])
@pytest.mark.parametrize('selection,label', viewer.SORT_OPTIONS)
def test_plain_sort_labels_and_reset_page(page, view, selection, label):
    cache, request = page
    MusicLibrary(cache.database).create_album('Band', 'Album')
    soup = BeautifulSoup(
        request(view + '?' + urlencode({'sort': selection}))['body'], 'html.parser'
    )
    form = soup.select_one('form.catalog-controls')
    assert form.select_one('select[name=sort] option[selected]').get_text() == label
    assert form.select_one('select[name=sort] option[selected]')['value'] == selection
    assert not form.select('[name=page], [name=order]')
    switched = soup.select_one('.view-switch a:not([aria-current])')['href']
    other = BeautifulSoup(request(switched[4:])['body'], 'html.parser')
    assert other.select_one('select[name=sort] option[selected]').get_text() == label


@pytest.mark.parametrize(
    'target',
    ['/bot/gallery?artist=absent', '/bot/?q=absent', '/bot/gallery?album_title=absent'],
)
def test_album_outside_filtered_results_has_no_neighbors(page, target):
    cache, request = page
    insert(cache)
    soup = BeautifulSoup(
        request('/albums/42?' + urlencode({'return_to': target + '#scroll=200'}))[
            'body'
        ],
        'html.parser',
    )
    assert not soup.select('.album-navigation a')
    assert len(soup.select('.album-navigation button[disabled]')) == 2
    assert soup.select_one('#back-to-results')['href'] == target + '#scroll=200'


def test_default_album_navigation_uses_dates_before_ids(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    for title in ('Middle', 'Newest', 'Oldest'):
        library.create_album('Band', title)
    with sqlite3.connect(cache.database) as db:
        db.execute(
            "UPDATE albums SET created_at = CASE id WHEN 1 THEN '2026-02-01' WHEN 2 THEN '2026-03-01' ELSE '2026-01-01' END"
        )
    soup = BeautifulSoup(request('/albums/1')['body'], 'html.parser')
    assert soup.select_one('.album-navigation [rel=prev]')['href'] == '/bot/albums/2'
    assert soup.select_one('.album-navigation [rel=next]')['href'] == '/bot/albums/3'
    assert soup.select_one('.album-order').get_text() == 'All albums · Newest first'


@pytest.mark.parametrize('view', ['/', '/gallery'])
def test_result_ranges_use_configured_page_size(page, monkeypatch, view):
    cache, request = page
    monkeypatch.setattr(viewer, 'COLLECTION_PAGE_SIZE', 3)
    library = MusicLibrary(cache.database)
    for number in range(8):
        library.create_album('Band', f'Album {number}')
    for number, expected in [(2, 'Showing 4–6 of 8'), (3, 'Showing 7–8 of 8')]:
        soup = BeautifulSoup(request(view + f'?page={number}')['body'], 'html.parser')
        assert soup.select_one('.result-range').get_text() == expected
        assert soup.select_one('.page-count').get_text() == f'Page {number} of 3'


def test_album_neighbors_keep_original_page_and_scroll(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    for number in range(50):
        library.create_album(
            'Mazel Vatos' if number < 49 else 'Other', f'Release {number}'
        )
    with sqlite3.connect(cache.database) as db:
        db.execute("UPDATE albums SET created_at = '2026-01-01'")
    target = '/bot/gallery?q=Release&artist=vat&sort=chronology:desc&page=2#scroll=600'
    soup = BeautifulSoup(
        request('/albums/25?' + urlencode({'return_to': target}))['body'], 'html.parser'
    )
    previous = soup.select_one('.album-navigation [rel=prev]')['href']
    assert urlsplit(previous).path == '/bot/albums/26'
    assert parse_qs(urlsplit(previous).query)['return_to'] == [target]
    neighbor = BeautifulSoup(request(previous[4:])['body'], 'html.parser')
    assert neighbor.select_one('#back-to-results')['href'] == target
    assert (
        urlsplit(neighbor.select_one('.album-navigation [rel=next]')['href']).path
        == '/bot/albums/25'
    )


@pytest.mark.parametrize('view', ['/', '/gallery'])
def test_artist_discovery_links_preserve_view_without_nested_links(page, view):
    cache, request = page
    library = MusicLibrary(cache.database)
    artist = 'Mazel Vatos & <Co> ü / +'
    for number in range(25):
        library.create_album(artist, f'Night {number:02}')
    library.create_album('Other', 'Night 99')
    values = dict(
        q='Night', artist='vat', album_title='Night', sort='album', order='asc', page=2
    )
    soup = BeautifulSoup(request(view + '?' + urlencode(values))['body'], 'html.parser')
    artist_link = soup.select_one('.artist-link')
    assert artist_link.get_text() == artist
    destination = artist_link['href']
    assert urlsplit(destination).path == '/bot' + view
    assert parse_qs(urlsplit(destination).query) == dict(
        artist=[artist], sort=['album'], order=['asc'], page=['1']
    )
    discovered = BeautifulSoup(request(destination[4:])['body'], 'html.parser')
    assert discovered.select_one('[name=artist]')['value'] == artist
    assert (
        artist
        in discovered.select_one(
            '.filter-chips a[aria-label^="Remove Artist"]'
        ).get_text()
    )
    assert discovered.select_one('.result-range').get_text() == 'Showing 1–24 of 25'
    if view == '/gallery':
        card = soup.select_one('.gallery-item')
        assert len(card.select('a')) == 2
        assert not card.select('a a')
        album_link = card.select_one('a.gallery-card[data-album-link]')
        assert album_link.select_one('.gallery-artwork')
        assert album_link.select_one('strong').get_text() == 'Night 24'
        assert card.select_one('.gallery-artist') == artist_link
        assert not artist_link.has_attr('data-album-link')
        assert parse_qs(urlsplit(album_link['href']).query)['return_to'] == [
            '/bot/gallery?' + urlencode(values)
        ]


def test_gallery_search_controls_and_state(page):
    cache, request = page
    library = MusicLibrary(cache.database)
    for number in range(25):
        library.create_album('Mazel Vatos', f'Night {number:02}')
    library.create_album('Other', 'Night other')
    values = dict(
        q='NiGhT', artist='vAt', album_title='igh', sort='album', order='asc', page=2
    )
    soup = BeautifulSoup(
        request('/gallery?' + urlencode(values))['body'], 'html.parser'
    )
    form = soup.select_one('form.catalog-controls')
    assert (
        form.select_one('[name=q]').find_parent('label').get_text()
        == 'Search albums and artists'
    )
    assert form.select_one('button[type=submit]').get_text() == 'Search'
    assert not form.select('[name=page]')
    assert soup.select_one('.result-range').get_text() == 'Showing 25–25 of 25'
    assert len(soup.select('.gallery-card')) == 1
    previous = soup.select_one('.pagination [rel=prev]')['href']
    assert parse_qs(urlsplit(previous).query) == {
        key: [str(value)] for key, value in dict(values, page=1).items()
    }
    remove = soup.select_one('.filter-chips a[aria-label^="Remove Search"]')['href']
    assert parse_qs(urlsplit(remove).query) == dict(
        artist=['vAt'], album_title=['igh'], sort=['album'], order=['asc'], page=['1']
    )
    artist_remove = soup.select_one('.filter-chips a[aria-label^="Remove Artist"]')[
        'href'
    ]
    assert parse_qs(urlsplit(artist_remove).query)['q'] == ['NiGhT']
    list_url = soup.select_one('.view-switch a:not([aria-current])')['href']
    assert parse_qs(urlsplit(list_url).query) == {
        key: [str(value)] for key, value in values.items()
    }
    album_link = soup.select_one('.gallery-card')['href']
    album = BeautifulSoup(request(album_link[4:])['body'], 'html.parser')
    assert album.select_one('#back-to-results')['href'] == '/bot/gallery?' + urlencode(
        values
    )
    empty = BeautifulSoup(request('/gallery?q=missing')['body'], 'html.parser')
    assert empty.select_one('.result-range').get_text() == 'Showing 0–0 of 0'
    assert 'Try different keywords or clear filters.' in empty.get_text()
    assert empty.select_one('.filter-chips a[aria-label^="Remove Search"]')


def test_variation_section_follows_browsing_with_advanced_disclosure(page):
    cache, request = page
    insert(cache)
    soup = BeautifulSoup(request()['body'], 'html.parser')
    section = soup.select_one('main > .variation-section')
    assert section.h2.get_text() == 'Create a variation'
    assert (
        soup.select_one('.image-layout').find_next(
            'section', class_='variation-section'
        )
        == section
    )
    assert not soup.select_one('.image-layout #variation-form')
    assert 'Create New generates fresh artwork' in section.get_text()
    assert 'Edit uses the selected image' in section.get_text()
    assert 'Existing artwork is kept.' in section.get_text()
    form = section.select_one('form')
    advanced = form.select_one('details#variation-advanced')
    assert advanced.summary.get_text() == 'Advanced options'
    assert not advanced.has_attr('open')
    assert {field['name'] for field in advanced.select('select, textarea')} == {
        'format',
        'format_description',
        'artist_genre',
        'artist_description',
    }
    assert not form.select_one('[name=genre]').find_parent('details')
    assert not form.select_one('[name=description]').find_parent('details')
    assert {field['name'] for field in form.select('select, textarea')} == set(
        viewer.GenerationInputs.creative_fields
    )
    help_panel = soup.select_one('.keyboard-help')
    assert help_panel.summary.get_text() == 'Keyboard help'
    assert not help_panel.has_attr('open')
    assert 'Left/Right' in help_panel.get_text() and 'Home/End' in help_panel.get_text()


def test_artwork_history_labels_use_real_relationships_and_selection(page):
    cache, request = page
    insert(cache)
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE image_cache SET generation_metadata_json = ? WHERE id = 42',
            (json.dumps({'operation': 'create_new'}),),
        )
        for identifier, parent, operation in [
            (43, 42, 'edit'),
            (44, 43, 'edit'),
            (45, None, 'create_new'),
        ]:
            db.execute(
                """INSERT INTO image_cache (id, cache_key, prompt, normalized_prompt, settings_json, local_filename, hosted_url, generation_metadata_json, created_at, parent_image_id) VALUES (?, ?, '', '', '{}', '', ?, ?, ?, ?)""",
                (
                    identifier,
                    f'history-{identifier}',
                    f'https://history.example/{identifier}.png',
                    json.dumps({'operation': operation}),
                    f'2026-01-{identifier-40:02}',
                    parent,
                ),
            )
    library = MusicLibrary(cache.database)
    for identifier in (43, 44, 45):
        library.record_image(42, f'history-{identifier}')
    before = cache.database.read_bytes()
    soup = BeautifulSoup(
        request('/albums/42?source_image_id=43')['body'], 'html.parser'
    )
    links = {
        int(link['data-image-id']): link
        for link in soup.select('#image-history a[data-image-id]')
    }
    assert {
        identifier: link.select_one('.history-label').get_text()
        for identifier, link in links.items()
    } == {42: 'Original 1', 43: 'Variation 1', 44: 'Variation 2', 45: 'Original 2'}
    assert links[43]['aria-current'] == 'page'
    assert (
        links[43]['aria-label']
        == 'Variation 1, based on Original 1, image #43, Selected'
    )
    assert links[44]['aria-label'] == 'Variation 2, based on Variation 1, image #44'
    for identifier, link in links.items():
        assert link.select_one('.selection-indicator').has_attr('hidden') == (
            identifier != 43
        )
    assert cache.database.read_bytes() == before


def test_history_labels_do_not_invent_legacy_or_broken_relationships():
    history = [
        dict(
            id=identifier,
            created_at=f'2026-{identifier:02}',
            hosted_url=None,
            parent_image_id=parent,
            operation=operation,
        )
        for identifier, parent, operation in [
            (1, None, None),
            (2, None, 'edit'),
            (3, 99, 'edit'),
            (4, 4, 'edit'),
            (5, 6, 'edit'),
            (6, 5, 'edit'),
            (7, 4, 'edit'),
            (8, None, 'create_new'),
            (9, 1, 'edit'),
            (10, 3, 'edit'),
        ]
    ]
    described = viewer.describe_artwork_history(history[::-1])
    labels = {item['id']: item['history_label'] for item in described}
    assert labels == {
        1: 'Image 1',
        2: 'Image 2',
        3: 'Image 3',
        4: 'Image 4',
        5: 'Image 5',
        6: 'Image 6',
        7: 'Image 7',
        8: 'Original',
        9: 'Variation 1',
        10: 'Image 10',
    }
    assert [item['id'] for item in described] == list(range(10, 0, -1))
    assert (
        next(item for item in described if item['id'] == 9)['parent_label'] == 'Image 1'
    )
    assert viewer.describe_artwork_history([]) == []


@pytest.mark.parametrize(
    'thumbnail', [None, 'javascript:alert(1)', 'https://albums.example/art_thumb.jpg']
)
def test_history_uses_gallery_thumbnail_with_safe_full_size_fallback(page, thumbnail):
    cache, request = page
    original = 'https://albums.example/art.png'
    insert(cache, url=original)
    with sqlite3.connect(cache.database) as db:
        db.execute('UPDATE image_cache SET thumb_url = ?', (thumbnail,))
    before = cache.database.read_bytes()
    soup = BeautifulSoup(request()['body'], 'html.parser')
    assert soup.select_one('.image-history img')['src'] == (
        viewer.safe_image_url(thumbnail) or original
    )
    assert soup.select_one('.artwork-preview')['href'] == original
    assert soup.select_one('.artwork-preview img')['src'] == original
    assert cache.database.read_bytes() == before


def test_history_without_thumbnail_column_uses_original(page):
    cache, request = page
    insert(cache)
    with sqlite3.connect(cache.database) as db:
        columns = [
            row[1]
            for row in db.execute('PRAGMA table_info(image_cache)')
            if row[1] != 'thumb_url'
        ]
        db.execute(
            'CREATE TABLE legacy_images AS SELECT '
            + ', '.join(columns)
            + ' FROM image_cache'
        )
        db.execute('DROP TABLE image_cache')
        db.execute('ALTER TABLE legacy_images RENAME TO image_cache')
    before = cache.database.read_bytes()
    soup = BeautifulSoup(request()['body'], 'html.parser')
    assert (
        soup.select_one('.image-history img')['src']
        == cache.album_image_history(42)[0]['hosted_url']
    )
    assert cache.database.read_bytes() == before


def test_history_without_parent_column_is_neutral_and_read_only(page):
    cache, request = page
    insert(cache)
    with sqlite3.connect(cache.database) as db:
        columns = [
            row[1]
            for row in db.execute('PRAGMA table_info(image_cache)')
            if row[1] != 'parent_image_id'
        ]
        db.execute(
            'CREATE TABLE legacy_images AS SELECT '
            + ', '.join(columns)
            + ' FROM image_cache'
        )
        db.execute('DROP TABLE image_cache')
        db.execute('ALTER TABLE legacy_images RENAME TO image_cache')
    before = cache.database.read_bytes()
    history = cache.album_image_history(42)
    assert history[0]['parent_image_id'] is None
    assert viewer.describe_artwork_history(history)[0]['history_label'] == 'Image 1'
    assert cache.database.read_bytes() == before
