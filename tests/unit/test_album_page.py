import io
import sqlite3
from contextlib import closing
from unittest.mock import Mock
from urllib.parse import urlencode

import cherrypy
import pytest
from bs4 import BeautifulSoup

import pmxbot
from pmxbot.images import ImageCache, ImageError
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

    def request(path='/albums/42', method='GET', data=None):
        payload = urlencode(data or {}).encode()
        path, _, query = path.partition('?')
        env = {
            'REQUEST_METHOD': method,
            'SCRIPT_NAME': '/bot',
            'PATH_INFO': path,
            'QUERY_STRING': query,
            'SERVER_NAME': 'localhost',
            'HTTP_HOST': 'localhost',
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


def insert(cache, url='https://albums.example/art.png?x=1&y=2'):
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
    assert "preview.addEventListener('click'" in script
    assert 'event.preventDefault()' in script
    assert 'dialog.showModal()' in script
    assert 'dialog.close()' in script
    assert [
        section.h2.get_text()
        for section in soup.select('.image-content section[aria-labelledby]')
    ] == [
        'Artwork history',
        'Original prompt',
        'Prepare an image variation',
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
    assert not soup.select('.artwork-preview, img')
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
    assert not cards[0].select('a')
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
    soup = BeautifulSoup(request(f'/albums/{first["id"]}')['body'], 'html.parser')
    main = soup.select_one('.image-main')
    assert main.select_one('section[aria-label="Artwork"]') is not None
    assert [h.get_text() for h in main.select('h2')] == [
        'Original prompt',
        'Album details',
    ]
    navigation = soup.select_one('.image-sidebar nav')
    assert navigation.select_one('button[disabled]').get_text() == 'Prev'
    assert navigation.select_one('a[rel=next]')['href'] == f'/bot/albums/{last["id"]}'
    soup = BeautifulSoup(request(f'/albums/{last["id"]}')['body'], 'html.parser')
    assert soup.select_one('a[rel=prev]')['href'] == f'/bot/albums/{first["id"]}'
    assert soup.select_one('.album-navigation button[disabled]').get_text() == 'Next'
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
        'Prepare an image variation',
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
    assert soup.img['src'] == url
    assert set(soup.img.attrs) == {'src', 'alt', 'referrerpolicy'}
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
    insert(cache)
    with sqlite3.connect(cache.database) as db:
        db.execute('UPDATE image_cache SET id = 100 WHERE id = 42')
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
    assert not soup.select('img, #details-heading, .image-cache-key')
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
    assert 'Currently viewing image #44' in entries[0].get_text()
    assert entries[0].select_one('img') is None
    assert entries[1].select_one('a')['href'] == (
        '/bot/albums/42?source_image_id=43'
    )
    assert entries[1].select_one('img')['src'] == (
        'https://albums.example/newer.png'
    )
    assert entries[2].select_one('a')['href'] == (
        '/bot/albums/42?source_image_id=42'
    )
    assert cache.database.read_bytes() == before

    selected = request('/albums/42?source_image_id=43')
    selected_soup = BeautifulSoup(selected['body'], 'html.parser')
    assert selected['status'] == 200
    assert selected_soup.select_one('#prompt-heading ~ pre').get_text() == (
        'new prompt'
    )
    assert '"revised_prompt": "new details"' in selected_soup.select_one(
        '#metadata-heading-2 ~ pre'
    ).get_text()
    assert selected_soup.select_one(
        '#image-history-heading ~ ul li .current-image'
    ).get_text(strip=True) == 'Currently viewing image #43'
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
    links = soup.select('main li a')
    assert [link.get_text() for link in links] == [
        '<Band> — <Album>',
        '<Band> — Second',
        'Zulu — Last',
    ]
    assert [link['href'] for link in links] == [
        f'/bot/albums/{album["id"]}' for album in (first, second, last)
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
    assert soup.select_one('a[href="/bot/albums/42"]') is not None
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
    assert [card['href'] for card in cards] == [
        f'/bot/albums/{album["id"]}' for album in reversed(albums[1:])
    ]
    assert len(soup.select('.gallery img')) == 24
    assert cards[0].img['loading'] == 'lazy'
    assert cards[0].img['referrerpolicy'] == 'no-referrer'
    assert '<Band>' in cards[0].get_text()
    assert not soup.select('band')
    assert (
        soup.select_one('a[rel=next]')['href'] == '/bot/gallery?page=2&sort=date_desc'
    )
    assert not soup.select_one('a[rel=prev]')
    last = BeautifulSoup(request('/gallery?page=2')['body'], 'html.parser')
    assert len(last.select('.gallery-card')) == 1
    assert last.select_one('.gallery-card')['href'] == f'/bot/albums/{albums[0]["id"]}'
    assert (
        last.select_one('a[rel=prev]')['href'] == '/bot/gallery?page=1&sort=date_desc'
    )
    assert not last.select_one('a[rel=next]')
    assert request('/gallery?page=3')['status'] == 404
    assert request('/gallery', method='HEAD')['body'] == ''
    assert cache.database.read_bytes() == before


@pytest.mark.parametrize(
    'url', [None, 'javascript:alert(1)', 'https://example.com/art.png']
)
def test_gallery_artwork_safety_and_latest_image(page, url):
    cache, request = page
    insert(cache)
    with closing(cache.connect()) as db:
        db.execute(
            '''INSERT INTO image_cache
            (cache_key, prompt, normalized_prompt, settings_json, local_filename,
             hosted_url, generation_metadata_json, created_at)
            VALUES ('latest', 'prompt', 'prompt', '{}', 'private.png', ?, '{}', '2099-01-01')''',
            (url,),
        )
    library = MusicLibrary(cache.database)
    library.record_image(42, 'latest')
    missing = library.create_album('Band', 'No artwork')
    before = cache.database.read_bytes()
    soup = BeautifulSoup(request('/gallery')['body'], 'html.parser')
    card = soup.select_one('a[href="/bot/albums/42"]')
    assert bool(card.img) == bool(url and url.startswith('https://'))
    if card.img:
        assert card.img['src'] == url
    else:
        assert 'Artwork unavailable' in card.get_text()
    assert not soup.select_one(f'a[href="/bot/albums/{missing["id"]}"]').img
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
        ('band', [3, 2, 1, 4]),
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
    assert [card['href'] for card in soup.select('.gallery-card')] == [
        f'/bot/albums/{album_id}' for album_id in expected
    ]
    select = soup.select_one('select[name=sort]')
    assert select.select_one('option[selected]')['value'] == sort
    assert [option.get_text() for option in select.select('option')] == [
        'Alphabetical (Band)',
        'Alphabetical (Album)',
        'Date Ascending',
        'Date Descending',
        'Genre',
    ]
    form = soup.select_one('form.gallery-sort')
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
    assert next_url == '/bot/gallery?page=2&sort=band'
    second = BeautifulSoup(request(next_url[len('/bot') :])['body'], 'html.parser')
    assert second.select_one('option[selected]')['value'] == 'band'
    assert second.select_one('.gallery-card')['href'] == '/bot/albums/25'
    assert second.select_one('a[rel=prev]')['href'] == '/bot/gallery?page=1&sort=band'


@pytest.mark.parametrize(
    'sort', ['', 'invalid', 'genre&sort=band', 'id%3BDROP+TABLE+albums']
)
def test_gallery_invalid_sort_does_not_create_storage(page, sort):
    cache, request = page
    assert request(f'/gallery?sort={sort}')['status'] == 400
    assert not cache.database.exists()
    assert not cache.directory.exists()


def test_preparation_is_immutable_and_has_no_generation(page):
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
    assert response['status'] == 200
    soup = BeautifulSoup(response['body'], 'html.parser')
    assert 'validated' in soup.select_one('[role=status]').get_text()
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


def test_legacy_values_prepare_and_selected_source_stays_fixed(page):
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
    form = soup.form
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
    assert response['status'] == 200
    assert 'Source image #42' in response['body']
    assert 'validated' in response['body']
    assert cache.database.read_bytes() == before
