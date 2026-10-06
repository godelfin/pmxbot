import base64
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import pmxbot
from pmxbot import core, images, music, quotes
from pmxbot.music import MusicLibrary, album_prompt, generate_album_image

PNG = b'\x89PNG\r\n\x1a\nimage bytes'
URL = 'https://images.example.com/image.png'
ALBUMS_URL = 'https://bot.example.com/'


def hosted_url(cache, prompt):
    return f"{cache.r2['public_url'].rstrip('/')}/{cache.cache_key(prompt)}.png"


@pytest.fixture
def music_store(config, monkeypatch):
    store = quotes.SQLiteQuotes(config['database'])
    monkeypatch.setattr(quotes.Quotes, 'store', store, raising=False)
    yield store
    store.close()


@pytest.mark.parametrize('mapped', [False, True])
@pytest.mark.parametrize('existing', [False, True])
def test_music_prompt_and_shared_worker(
    config, music_store, monkeypatch, mapped, existing
):
    if existing:
        MusicLibrary(images.ImageCache(config).database).create_album(
            'Second Band', 'First Album', genre='Jazz', created_by='original'
        )
    genre = 'Jazz' if existing else None
    if mapped:
        config['quote_libraries'] = {'Band': 'artists', 'album': 'records'}
    band_library, album_library = (
        ('artists', 'records') if mapped else ('band', 'album')
    )
    music_store.db.executemany(
        'INSERT INTO quotes (library, quote) VALUES (?, ?)',
        [
            (band_library, 'First Band'),
            (band_library, 'Second Band'),
            (album_library, 'First Album'),
            (album_library, 'Second Album'),
        ],
    )
    choose = Mock(side_effect=[1, 0])
    monkeypatch.setattr(quotes.random, 'randrange', choose)
    monkeypatch.setattr(images.albums, 'formats', {'Vinyl'})
    monkeypatch.setattr(images.albums, 'format_desc', {'Remastered'})
    monkeypatch.setattr(images.albums, 'genres', {'Jazz': ['Fusion'], 'Anime': []})
    choices = []

    def select(options):
        choices.append(options)
        return options[0]

    monkeypatch.setattr(images.random, 'choice', select)
    thread = Mock()
    monkeypatch.setattr(images.threading, 'Thread', thread)
    try:
        assert (
            images.music('#test', 'alice')
            == 'Looking up or generating your album cover... '
            'Band: Second Band; Album: First Album'
        )
        thread.return_value.start.assert_called_once_with()
        kwargs = thread.call_args.kwargs
        assert kwargs['target'] is images._generate
        assert kwargs['kwargs'] == {'album_id': 1}
        cache, prompt, channel, nick = kwargs['args']
        assert isinstance(cache, images.ImageCache)
        assert prompt is None
        album = MusicLibrary(cache.database).get_album(1)
        assert album['images'] == []
        assert album['genre'] == genre
        assert album['created_by'] == ('original' if existing else 'alice')
        assert album_prompt(album) == (
            'an album cover for the band Second Band. the name of the album is First Album. '
            'this is the Vinyl, Remastered edition.'
        ) + (
            ' the genre of music is Jazz, but nowhere should the genre be mentioned.'
            if existing
            else ''
        )
        assert choices == [('Vinyl',), ('Remastered',)]
        assert (channel, nick) == ('#test', 'alice')
        assert choose.call_count == 2
        assert 'already running' in images._start_image(
            'cat', '#test', 'bob', 'Looking up your image…'
        )
    finally:
        images._busy.release()


@pytest.mark.parametrize('missing', ['band', 'album'])
def test_music_empty_library(config, music_store, monkeypatch, missing):
    if missing == 'album':
        music_store.db.execute(
            "INSERT INTO quotes (library, quote) VALUES ('band', 'A Band')"
        )
    start = Mock()
    monkeypatch.setattr(images, '_start_image', start)
    assert (
        images.music('#test', 'alice')
        == f'No {missing} entries found. Add one with !{missing} add: <text>.'
    )
    start.assert_not_called()


def test_music_disabled(config, monkeypatch):
    config['images_enabled'] = False
    start = Mock()
    monkeypatch.setattr(images, '_start_image', start)
    result = images.music('#test', 'alice')
    assert 'disabled' in result
    assert ALBUMS_URL not in result
    start.assert_not_called()


@pytest.mark.parametrize(
    ('settings', 'expected'),
    [
        (
            {'albums_url': 'https://bot.example.com/bot/'},
            'https://bot.example.com/bot/albums/1',
        ),
        (
            {'logs URL': 'https://logs.example.com/bot/'},
            'https://logs.example.com/bot/albums/1',
        ),
        ({'web_base': '/bot/'}, 'http://bot.example.com/bot/albums/1'),
        ({}, 'http://bot.example.com/albums/1'),
    ],
)
def test_music_generated_album_link(config, post, monkeypatch, settings, expected):
    del config['albums_url']
    config.update(settings)
    monkeypatch.setattr(images.socket, 'getfqdn', lambda: 'bot.example.com')
    cache = images.ImageCache(config)
    album = MusicLibrary(cache.database).create_album('Band', 'Album')
    images._busy.acquire()
    images._generate(cache, None, '#test', 'alice', album['id'])
    output = list(images.image_results())
    assert output[2] == expected
    assert '\n' not in output[1]


def test_music_registered():
    assert next(core.Handler.find_matching('!music', '#test')).func is images.music


@pytest.mark.parametrize('argument', ['1', '#1', '  #1  '])
def test_music_lookup_latest_cached_image(config, post, r2, monkeypatch, argument):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album(
        'Band', 'Album', genre='Jazz', format='Vinyl', format_description='Remastered'
    )
    # Insert out of timestamp order, including an unhosted newer entry and
    # an unrelated image. Link order must not determine the selected image.
    for prompt, date, linked in [
        ('newest', '2026-02-01', True),
        ('oldest', '2026-01-01', True),
        ('unhosted', '2026-03-01', True),
        ('unrelated', '2026-04-01', False),
    ]:
        cache.get(prompt)
        key = cache.cache_key(prompt)
        if linked:
            library.record_image(album['id'], key)
        with sqlite3.connect(cache.database) as db:
            db.execute(
                'UPDATE image_cache SET created_at = ? WHERE cache_key = ?',
                (date, key),
            )
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE image_cache SET hosted_url = NULL WHERE cache_key = ?',
            (cache.cache_key('unhosted'),),
        )
    library.record_image(album['id'], 'missing-cache-entry')
    post.reset_mock()
    r2.reset_mock()
    monkeypatch.setattr(images, '_start_image', Mock())
    # Retrieval also works without credentials or the original model settings,
    # and while generation is busy.
    for name in list(config):
        if name.startswith(('r2_', 'openai_')):
            del config[name]
    config['images_model'] = 'different-model'
    images._busy.acquire()
    try:
        assert images.music('#test', 'bob', argument) == (
            'Band: Band; Album: Album; ' f"#1 {hosted_url(cache, 'newest')}"
        )
    finally:
        images._busy.release()
    post.assert_not_called()
    r2.put_object.assert_not_called()
    images._start_image.assert_not_called()
    with sqlite3.connect(cache.database) as db:
        assert (
            db.execute(
                'SELECT hit_count FROM image_cache WHERE cache_key = ?',
                (cache.cache_key('newest'),),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize('argument', ['1', '#1', '  #1  ', '001', '#001'])
def test_music_lookup_id_validation(config, monkeypatch, argument):
    library = Mock()
    library.get_album.return_value = {
        'artist_name': 'Band',
        'title': 'Album',
        'format': None,
        'format_description': None,
        'genre': None,
    }
    monkeypatch.setattr(images, 'MusicLibrary', Mock(return_value=library))
    lookup = Mock(return_value=URL)
    monkeypatch.setattr(images.ImageCache, 'latest_album_image', lookup)
    assert images.music('#test', 'alice', argument).endswith(f'#1 {URL}')
    library.get_album.assert_called_once_with(1)
    lookup.assert_called_once_with(1)


@pytest.mark.parametrize(
    'argument',
    ['abc', '0', '-1', '#', '1 extra', '##1', '1#', '#0', '#-1', '１', '#١'],
)
def test_music_lookup_invalid_id(config, argument):
    assert images.music('#test', 'alice', argument) == 'Usage: !music [album ID]'


def test_music_lookup_missing_image_or_album(config):
    assert images.music('#test', 'alice', '1') == 'Unknown album ID: #1.'
    library = MusicLibrary(images.ImageCache(config).database)
    library.create_album('Band', 'Album')
    assert images.music('#test', 'alice', '#1') == 'No cached image for album #1.'


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = {
        'database': f'sqlite:{tmp_path / "pmxbot.sqlite"}',
        'images_enabled': True,
        'albums_url': ALBUMS_URL,
        'images_directory': str(tmp_path / 'images'),
        'openai_api_key': 'openai-secret',
        'r2_endpoint_url': 'https://account.r2.cloudflarestorage.com',
        'r2_access_key_id': 'r2-access-secret',
        'r2_secret_access_key': 'r2-secret',
        'r2_bucket': 'bot-images',
        'r2_public_url': 'https://images.example.com',
    }
    monkeypatch.setattr(pmxbot, 'config', config, raising=False)
    return config


@pytest.fixture
def post(monkeypatch):
    generation = Mock()
    generation.json.return_value = {
        'created': 123,
        'usage': {'total_tokens': 42},
        'data': [{'b64_json': base64.b64encode(PNG).decode()}],
    }
    post = Mock(return_value=generation)
    monkeypatch.setattr(images.requests, 'post', post)
    return post


@pytest.fixture(autouse=True)
def r2(monkeypatch):
    client = Mock()
    client.put_object.return_value = {'ETag': 'image-etag'}
    monkeypatch.setattr(images.boto3, 'client', Mock(return_value=client))
    return client


def read_row(cache):
    with sqlite3.connect(str(cache.database)) as db:
        db.row_factory = sqlite3.Row
        return db.execute('SELECT * FROM image_cache').fetchone()


def test_music_result_ids_persist_and_distinguish_albums(config, post):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    first = library.create_album('Band', 'First', created_by='alice')
    second = library.create_album('Band', 'Second')

    def result(album_id):
        images._busy.acquire()
        images._generate(images.ImageCache(config), None, '#test', 'alice', album_id)
        return list(images.image_results())[1:]

    first_result = result(first['id'])
    assert first_result == [
        f"alice: #{first['id']} {hosted_url(cache, album_prompt(first))}",
        f"{ALBUMS_URL}albums/{first['id']}",
    ]
    assert result(second['id']) == [
        f"alice: #{second['id']} {hosted_url(cache, album_prompt(second))}",
        f"{ALBUMS_URL}albums/{second['id']}",
    ]
    assert result(first['id']) == first_result
    assert post.call_count == 2
    with sqlite3.connect(str(cache.database)) as db:
        row = db.execute(
            'SELECT local_filename FROM image_cache WHERE cache_key = ?',
            (cache.cache_key(album_prompt(first)),),
        ).fetchone()
        Path(row[0]).unlink()
        db.execute('UPDATE image_cache SET hosted_url = NULL')
    assert result(first['id']) == first_result
    assert post.call_count == 3


def test_music_strips_metadata_before_album_prompt(config, post, monkeypatch):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album(
        'Band',
        'Album',
        genre='Jazz',
        format='Vinyl',
        format_description='Remastered',
        description='Minimalist',
    )
    prompt = Mock(wraps=album_prompt)
    monkeypatch.setattr(music, 'album_prompt', prompt)
    images._busy.acquire()
    images._generate(cache, None, '#test', 'alice', album['id'])
    list(images.image_results())
    prompt.assert_called_once_with(
        dict(album, genre=None, description=None, format=None, format_description=None)
    )
    assert post.call_args.kwargs['json']['prompt'] == (
        'an album cover for the band Band. the name of the album is Album.'
    )
    saved = library.get_album(album['id'])
    for field in ('genre', 'description', 'format', 'format_description'):
        assert saved[field] == album[field]


def test_music_error_preserves_album_without_image(config, monkeypatch):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album')
    monkeypatch.setattr(cache, 'get', Mock(side_effect=images.ImageError('Failed')))
    images._busy.acquire()
    images._generate(cache, None, '#test', 'alice', album['id'])
    assert list(images.image_results()) == ['#test', 'alice: Failed']
    assert library.get_album(album['id']) == album


def test_album_persistence_is_independent_of_images(config, post, r2):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    first = library.create_album(
        'Bänd',
        'First',
        genre='Jazz',
        format='Vinyl',
        format_description='Remastered',
        description='Minimalist',
        created_by='alice',
    )
    post.assert_not_called()
    r2.put_object.assert_not_called()
    assert first['created_at']
    assert first['created_by'] == 'alice'
    assert first['description'] == 'Minimalist'
    assert first['images'] == []
    restarted = MusicLibrary(cache.database)
    assert (
        restarted.create_album('  BÄND ', 'FIRST', genre='Rock', created_by='bob')
        == first
    )
    second = restarted.create_album('Bänd', 'Second')
    assert second['artist_id'] == first['artist_id']
    assert restarted.create_album('Other', 'First')['id'] != first['id']
    cache.get(album_prompt(first), 'original-requester', '#original')
    url = generate_album_image(restarted, cache, first['id'], 'bob', '#test')
    assert post.call_args.kwargs['json']['prompt'] == (
        'an album cover for the band Bänd. the name of the album is First. '
        'this is the Vinyl, Remastered edition. '
        'the genre of music is Jazz, but nowhere should the genre be mentioned. Minimalist'
    )
    assert url == hosted_url(cache, album_prompt(first))
    saved = restarted.get_album(first['id'])
    assert saved['created_by'] == 'alice'
    assert read_row(cache)['requested_by'] == 'original-requester'
    assert read_row(cache)['created_at']
    assert saved['images'][0]['cache_key'] == cache.cache_key(album_prompt(first))
    generate_album_image(restarted, cache, first['id'], 'carol', '#test')
    assert restarted.get_album(first['id']) == saved
    assert post.call_count == 1


def test_existing_album_fills_only_missing_music_metadata(tmp_path):
    library = MusicLibrary(tmp_path / 'music.sqlite')
    original = library.create_album(
        'Band', 'Album', genre='Jazz', description='Original', created_by='alice'
    )
    filled = library.create_album(
        'BAND',
        'ALBUM',
        genre='Rock',
        format='Vinyl',
        format_description='Remastered',
        description='Replacement',
        created_by='bob',
    )
    assert filled == dict(original, format='Vinyl', format_description='Remastered')
    assert (
        library.create_album('Band', 'Album', format='CD', format_description='Live')
        == filled
    )


def test_unknown_album_does_not_generate(config, post):
    cache = images.ImageCache(config)
    with pytest.raises(LookupError):
        generate_album_image(MusicLibrary(cache.database), cache, 123)
    post.assert_not_called()


def test_generate_persist_and_reuse(config, post):
    cache = images.ImageCache(config)
    assert cache.get('  A CAFÉ\tcat ', 'alice', '#test') == hosted_url(
        cache, 'a café cat'
    )
    row = read_row(cache)
    assert Path(row['local_filename']).read_bytes() == PNG
    assert row['normalized_prompt'] == 'a café cat'
    assert row['requested_by'] == 'alice'
    assert row['channel'] == '#test'
    assert json.loads(row['generation_metadata_json'])['usage']['total_tokens'] == 42
    assert 'b64_json' not in row['generation_metadata_json']
    assert row['host'] == 'r2'
    assert json.loads(row['host_metadata_json'])['etag'] == 'image-etag'
    request = post.call_args_list[0].kwargs
    assert request['json']['prompt'] == '  A CAFÉ\tcat '
    assert request['json']['output_format'] == 'png'
    assert request['headers']['Authorization'] == 'Bearer openai-secret'
    # A new instance simulates a restart; credentials are not needed for a hit.
    config.update(openai_api_key='', r2_access_key_id='', r2_secret_access_key='')
    assert images.ImageCache(config).get('a cafe\u0301 CAT') == hosted_url(
        cache, 'a café cat'
    )
    assert post.call_count == 1
    assert read_row(cache)['hit_count'] == 1


def test_settings_change_cache_key(config):
    original = images.ImageCache(config).cache_key('cat')
    for setting, value in [
        ('images_model', 'gpt-image-1.5'),
        ('images_size', '1536x1024'),
        ('images_quality', 'high'),
    ]:
        assert (
            images.ImageCache(dict(config, **{setting: value})).cache_key('cat')
            != original
        )


def test_cache_uses_main_database(config, post):
    from pmxbot.storage import SQLiteStorage

    main = SQLiteStorage(config['database'])
    try:
        main.db.execute('CREATE TABLE existing_data (value TEXT)')
        main.db.execute("INSERT INTO existing_data VALUES ('keep me')")
        cache = images.ImageCache(config)
        assert cache.get('cat') == hosted_url(cache, 'cat')
        assert main.db.execute('SELECT hosted_url FROM image_cache').fetchone() == (
            hosted_url(cache, 'cat'),
        )
        assert main.db.execute('SELECT value FROM existing_data').fetchone() == (
            'keep me',
        )
        assert not (cache.directory / 'cache.sqlite').exists()
    finally:
        main.close()


@pytest.mark.parametrize('uri', ['sqlite:main.sqlite', 'main.sqlite'])
def test_database_path_matches_main_storage(config, tmp_path, monkeypatch, uri):
    monkeypatch.chdir(tmp_path)
    config['database'] = uri
    assert images.ImageCache(config).database == tmp_path / 'main.sqlite'
    del config['database']
    assert images.ImageCache(config).database == tmp_path / 'pmxbot.sqlite'


@pytest.mark.parametrize(
    'uri', ['mongodb://localhost/pmxbot', 'sqlite::memory:', 'sqlite:']
)
def test_unsupported_database_reports_error(config, post, uri):
    config['database'] = uri
    assert 'SQLite main bot database' in images._start_image(
        'cat', '#test', 'alice', 'Looking up your image…'
    )
    assert not images._busy.locked()
    post.assert_not_called()


def test_upload_failure_retries_only_upload(config, post, r2):
    r2.put_object.side_effect = [
        images.ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'secret'}}, 'PutObject'
        ),
        {'ETag': 'ok'},
    ]
    cache = images.ImageCache(config)
    with pytest.raises(images.ImageError, match='saved locally'):
        cache.get('cat')
    assert read_row(cache)['hosted_url'] is None
    assert read_row(cache)['last_error']
    assert 'secret' not in read_row(cache)['last_error']
    assert cache.get('CAT') == hosted_url(cache, 'cat')
    assert post.call_count == 1
    assert r2.put_object.call_count == 2
    assert read_row(cache)['last_error'] is None


def test_missing_local_file_regenerates_pending_upload(config, post, r2):
    r2.put_object.side_effect = [images.BotoCoreError(), {'ETag': 'ok'}]
    cache = images.ImageCache(config)
    with pytest.raises(images.ImageError):
        cache.get('cat')
    Path(read_row(cache)['local_filename']).unlink()
    assert cache.get('cat') == hosted_url(cache, 'cat')
    assert post.call_count == 2


@pytest.mark.parametrize(
    'data',
    [
        {},
        {'data': []},
        {'data': [{'b64_json': '!'}]},
        {'data': [{'b64_json': 'aGVsbG8='}]},
    ],
)
def test_invalid_generation_does_not_upload(config, post, data):
    response = Mock()
    response.json.return_value = data
    post.side_effect = [response]
    cache = images.ImageCache(config)
    with pytest.raises(images.ImageError, match='valid PNG'):
        cache.get('cat')
    assert read_row(cache) is None
    assert post.call_count == 1


def test_missing_credentials_prevents_generation(config, post, monkeypatch):
    config['r2_secret_access_key'] = ''
    monkeypatch.delenv('R2_SECRET_ACCESS_KEY', raising=False)
    with pytest.raises(images.ImageError, match='R2_SECRET_ACCESS_KEY'):
        images.ImageCache(config).get('cat')
    post.assert_not_called()


def test_provider_error_is_sanitized(config, post):
    post.side_effect = requests.HTTPError('Authorization: secret')
    with pytest.raises(images.ImageError, match='OpenAI request failed') as caught:
        images.ImageCache(config).get('cat')
    assert 'secret' not in str(caught.value)


def test_r2_upload_parameters(config, post, r2):
    cache = images.ImageCache(config)
    received = []

    def upload(**kwargs):
        received.append(kwargs['Body'].read())
        return {'ETag': 'etag'}

    r2.put_object.side_effect = upload
    cache.get('cat')
    assert received == [PNG]
    args = r2.put_object.call_args.kwargs
    assert args['Bucket'] == 'bot-images'
    assert args['Key'] == cache.cache_key('cat') + '.png'
    assert args['ContentType'] == 'image/png'
    client_args = images.boto3.client.call_args.kwargs
    assert client_args['region_name'] == 'auto'
    assert client_args['endpoint_url'] == config['r2_endpoint_url']
    assert client_args['aws_access_key_id'] == 'r2-access-secret'
    assert client_args['aws_secret_access_key'] == 'r2-secret'
    assert client_args['config'].signature_version == 's3v4'
    r2.close.assert_called_once()


def test_imgbb_entry_migrates_without_regeneration(config, post, r2):
    cache = images.ImageCache(config)
    cache.get('cat')
    with sqlite3.connect(str(cache.database)) as db:
        db.execute(
            "UPDATE image_cache SET host = 'imgbb', hosted_url = 'https://i.ibb.co/old.png', host_metadata_json = '{}' "
        )
    r2.put_object.side_effect = images.BotoCoreError()
    with pytest.raises(images.ImageError, match='saved locally'):
        cache.get('cat')
    assert read_row(cache)['host'] == 'imgbb'
    assert read_row(cache)['hosted_url'] == 'https://i.ibb.co/old.png'
    r2.put_object.side_effect = None
    assert cache.get('cat') == hosted_url(cache, 'cat')
    assert read_row(cache)['host'] == 'r2'
    assert post.call_count == 1
    assert r2.put_object.call_count == 3


@pytest.mark.parametrize(
    'setting,value',
    [
        ('r2_bucket', 'another-bucket'),
        ('r2_endpoint_url', 'https://another.r2.cloudflarestorage.com'),
    ],
)
def test_changed_r2_destination_reuploads_local_image(config, post, r2, setting, value):
    images.ImageCache(config).get('cat')
    config[setting] = value
    cache = images.ImageCache(config)
    assert cache.get('cat') == hosted_url(cache, 'cat')
    assert post.call_count == 1
    assert r2.put_object.call_count == 2


def test_changed_public_url_reuses_object(config, post, r2):
    images.ImageCache(config).get('cat')
    config['r2_public_url'] = 'https://new.example.com/'
    cache = images.ImageCache(config)
    url = cache.get('cat')
    assert url == hosted_url(cache, 'cat')
    assert read_row(cache)['hosted_url'] == url
    assert post.call_count == 1
    assert r2.put_object.call_count == 1


@pytest.mark.parametrize('name', ['r2_endpoint_url', 'r2_public_url'])
@pytest.mark.parametrize(
    'url',
    [
        'http://images.example.com',
        'https://example.com/\r\ninject',
        'https://user:secret@example.com',
        'https://example.com?secret=x',
    ],
)
def test_invalid_r2_url_prevents_generation(config, post, name, url):
    config[name] = url
    with pytest.raises(images.ImageError, match='HTTPS URL'):
        images.ImageCache(config).get('cat')
    post.assert_not_called()


def test_worker_runs_in_background_and_delivers_result(config, monkeypatch):
    started, finish = threading.Event(), threading.Event()

    def get(*args):
        started.set()
        assert finish.wait(5)
        return URL

    monkeypatch.setattr(images.ImageCache, 'get', get)
    assert 'Looking up' in images._start_image(
        'cat', '#test', 'alice', 'Looking up your image…'
    )
    try:
        assert started.wait(5)
        assert 'already running' in images._start_image(
            'dog', '#test', 'bob', 'Looking up your image…'
        )
        assert list(images.image_results()) == []
    finally:
        finish.set()
    item = images._results.get(timeout=5)
    images._results.put(item)
    output = list(images.image_results())
    assert isinstance(output[0], core.SwitchChannel)
    assert output == ['#test', f'alice: {URL}']
    assert not images._busy.locked()


def test_worker_disabled(config, post):
    config['images_enabled'] = False
    assert 'disabled' in images._start_image('cat', '#test', 'alice', 'Starting')
    post.assert_not_called()


def test_image_commands_removed():
    for message in ('!image cat', '!imagine cat'):
        assert not list(core.Handler.find_matching(message, '#test'))


def test_keys_redacted_at_startup(config, monkeypatch, caplog):
    config['openai_admin_key'] = 'admin-secret'
    monkeypatch.setattr(core, '_load_library_extensions', lambda: None)
    monkeypatch.setattr(core, '_load_bot_class', lambda: Mock())
    with caplog.at_level('INFO'):
        core.initialize(config)
    assert 'openai-secret' not in caplog.text
    assert 'admin-secret' not in caplog.text
    assert 'r2-access-secret' not in caplog.text
    assert 'r2-secret' not in caplog.text
    assert '<redacted>' in caplog.text


def test_worker_storage_error_delivered_without_secrets(config, monkeypatch):
    monkeypatch.setattr(images.ImageCache, 'get', Mock(side_effect=OSError('secret')))
    images._busy.acquire()
    images._generate(images.ImageCache(config), 'cat', '#test', 'alice')
    output = list(images.image_results())
    assert 'secret' not in output[1]
    assert 'failed' in output[1]
    assert not images._busy.locked()


def test_music_result_delivery_to_irc(config, post, monkeypatch):
    import irc.client

    from pmxbot.irc import LoggingCommandBot

    cache = images.ImageCache(config)
    album = MusicLibrary(cache.database).create_album('Band', 'Album')
    bot = LoggingCommandBot.__new__(LoggingCommandBot)
    bot._nickname = 'pmxbot'
    bot._conn = irc.client.ServerConnection(irc.client.Reactor())
    bot._conn.socket = Mock()
    monkeypatch.setattr(bot, 'allow', lambda channel, message: True)
    monkeypatch.setattr(core.ContentHandler, 'find_matching', Mock(return_value=[]))
    images._busy.acquire()
    images._generate(cache, None, '#test', 'alice', album['id'])
    handler = next(
        item for item in core.Scheduled._registry if item.func is images.image_results
    )
    bot.handle_scheduled(handler)
    assert [call.args[0] for call in bot._conn.socket.write.call_args_list] == [
        f'PRIVMSG #test :alice: #1 {hosted_url(cache, album_prompt(album))}\r\n'.encode(),
        f'PRIVMSG #test :{ALBUMS_URL}albums/1\r\n'.encode(),
    ]
    assert not images._busy.locked()


def test_result_delivery_respects_silent_mode():
    class Bot(core.Bot):
        def transmit(self, channel, message):
            raise AssertionError('Silent bot must not transmit')

    bot = Bot()
    bot.silent = True
    images._busy.acquire()
    images._results.put(('#test', URL))
    handler = next(
        item for item in core.Scheduled._registry if item.func is images.image_results
    )
    bot.handle_scheduled(handler)
    assert not images._busy.locked()


def test_album_upload_failure_can_be_retried_by_id(config, post, r2):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album')
    r2.put_object.side_effect = images.BotoCoreError()
    with pytest.raises(images.ImageError):
        generate_album_image(library, cache, album['id'], 'alice')
    assert library.get_album(album['id']) == album
    r2.put_object.side_effect = None
    generate_album_image(library, cache, album['id'], 'alice')
    assert library.get_album(album['id'])['images']
    assert post.call_count == 1


def test_busy_worker_does_not_create_album(config):
    images._busy.acquire()
    try:
        assert 'already running' in images._start_image(
            None,
            '#test',
            'alice',
            None,
            album_data={'artist': 'Band', 'title': 'Album'},
        )
        assert not images.ImageCache(config).database.exists()
    finally:
        images._busy.release()


def test_worker_start_failure_keeps_album(config, monkeypatch):
    monkeypatch.setattr(images.threading, 'Thread', Mock(side_effect=RuntimeError))
    assert 'Could not start' in images._start_image(
        None,
        '#test',
        'alice',
        None,
        album_data={'artist': 'Band', 'title': 'Album'},
    )
    assert not images._busy.locked()
    album = MusicLibrary(images.ImageCache(config).database).get_album(1)
    assert album['title'] == 'Album'
    assert album['images'] == []


def test_concurrent_album_creation_reuses_pair(config):
    from concurrent.futures import ThreadPoolExecutor

    library = MusicLibrary(images.ImageCache(config).database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: library.create_album('Band', 'Album'), range(8)))
    assert all(row == rows[0] for row in rows)


def test_album_images_single_owner_and_idempotent(tmp_path):
    library = MusicLibrary(tmp_path / 'music.sqlite')
    first = library.create_album('Band', 'First')['id']
    second = library.create_album('Band', 'Second')['id']
    library.record_image(first, 'a')
    library.record_image(first, 'b')
    with pytest.raises(sqlite3.IntegrityError, match='already linked to album'):
        library.record_image(second, 'a')
    library.record_image(first, 'a')
    saved = MusicLibrary(library.database).get_album(first)['images']
    assert [image['cache_key'] for image in saved] == ['a', 'b']
    assert saved == [{'cache_key': 'a'}, {'cache_key': 'b'}]
    assert library.get_album(second)['images'] == []
    with sqlite3.connect(library.database) as db:
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            db.execute(
                'INSERT INTO album_images (album_id, cache_key) VALUES (?, ?)',
                (second, 'a'),
            )
    with pytest.raises(sqlite3.IntegrityError):
        library.record_image(999, 'unused')


@pytest.mark.parametrize('conflicting', [False, True])
def test_upgrade_album_image_ownership(tmp_path, conflicting):
    library = MusicLibrary(tmp_path / 'music.sqlite')
    first = library.create_album('Band', 'First')['id']
    second = library.create_album('Band', 'Second')['id']
    library.record_image(first, 'a')
    with sqlite3.connect(library.database) as db:
        db.execute('DROP INDEX album_images_unique_cache_key')
        if conflicting:
            db.execute(
                'INSERT INTO album_images (album_id, cache_key) VALUES (?, ?)',
                (second, 'a'),
            )
    if conflicting:
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            library.get_album(first)
        with sqlite3.connect(library.database) as db:
            assert db.execute('SELECT COUNT(*) FROM album_images').fetchone()[0] == 2
    else:
        assert library.get_album(first)['images'] == [{'cache_key': 'a'}]
        with pytest.raises(sqlite3.IntegrityError):
            library.record_image(second, 'a')


def test_image_ids_survive_retry_cache_hit_and_reopen(config, post, r2):
    cache = images.ImageCache(config)
    r2.put_object.side_effect = images.BotoCoreError()
    with pytest.raises(images.ImageError):
        cache.get('cat', 'alice', '#test')
    first = dict(read_row(cache))
    assert isinstance(first['id'], int) and first['id'] > 0
    Path(first['local_filename']).unlink()
    r2.put_object.side_effect = None
    cache.get('CAT', 'bob', '#other')
    cache.get('cat')
    saved = images.ImageCache(config).get_image(first['id'])
    assert saved['cache_key'] == first['cache_key']
    assert saved['created_at'] == first['created_at']
    assert saved['requested_by'] == 'alice'
    assert saved['channel'] == '#test'
    cache.get('dog')
    from contextlib import closing

    with closing(cache.connect()) as db:
        dog_id = db.execute(
            'SELECT id FROM image_cache WHERE cache_key = ?',
            (cache.cache_key('dog'),),
        ).fetchone()['id']
    assert dog_id != first['id']
    assert cache.get_image(dog_id)['cache_key'] == cache.cache_key('dog')
    with pytest.raises(LookupError, match='Unknown image ID'):
        cache.get_image(99999)


def test_existing_image_records_preserve_every_field_and_album_links(config, post):
    from contextlib import closing

    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album')
    library.record_image(album['id'], 'old')
    with closing(cache.connect()) as db:
        db.execute(
            '''INSERT INTO image_cache VALUES (
            42, 'old', 'original prompt', 'original prompt', '{"quality":"low"}',
            '/missing/original.png', 'https://old.example/image.png', 'imgbb',
            '{"host":"original"}', '{"usage":42}', 'alice', '#test',
            '2025-01-01', '2025-01-02', '2025-01-03', 7, 'upload error'
        )'''
        )
        before = dict(db.execute('SELECT * FROM image_cache').fetchone())
        identifier = db.execute('SELECT id FROM image_cache').fetchone()[0]
    saved = cache.get_image(identifier)
    assert identifier == 42
    assert saved == before
    assert cache.latest_album_image(album['id']) == before['hosted_url']
    assert library.get_album(album['id'])['images'][0]['cache_key'] == 'old'
    assert images.ImageCache(config).get_image(identifier)['cache_key'] == 'old'
    with closing(cache.connect()) as db:
        db.execute('VACUUM')
    assert cache.get_image(identifier)['cache_key'] == 'old'
    post.assert_not_called()


def test_concurrent_image_connections_preserve_one_stable_id(config):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing

    cache = images.ImageCache(config)
    with closing(cache.connect()) as db:
        db.execute(
            '''INSERT INTO image_cache
            (cache_key, prompt, normalized_prompt, settings_json, local_filename,
             generation_metadata_json) VALUES ('key', 'x', 'x', '{}', 'x.png', '{}')'''
        )
        identifier = db.execute('SELECT id FROM image_cache').fetchone()[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: cache.get_image(identifier), range(8)))
    assert records[0]['cache_key'] == 'key'
    assert all(record == records[0] for record in records)


def test_concurrent_fresh_image_schema_creation(config):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing

    cache = images.ImageCache(config)

    def primary_key(_):
        with closing(cache.connect()) as db:
            return [
                row['name']
                for row in db.execute('PRAGMA table_info(image_cache)')
                if row['pk']
            ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(primary_key, range(8))) == [['id']] * 8
