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
        assert album['format'] is None
        assert album['format_description'] is None
        assert album['created_by'] == ('original' if existing else 'alice')
        assert album_prompt(album) == (
            'an album cover for the band "Second Band". the name of the album is "First Album".'
        ) + (
            ' the genre of music is Jazz, but nowhere should the genre be mentioned.'
            if existing
            else ''
        )
        assert (channel, nick) == ('#test', 'alice')
        assert choose.call_count == 2
        assert 'queued' in images._start_image(
            'cat', '#test', 'bob', 'Looking up your image…'
        )
    finally:
        images._pending.get_nowait()
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
    # Most worker lifecycle tests use a single slot to exercise queue transitions.
    monkeypatch.setattr(images, '_busy', threading.Lock())
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
        'an album cover for the band "Band". the name of the album is "Album".'
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
    (failure,) = cache.album_image_failures(album['id'])
    assert failure['error'] == 'Failed'
    assert failure['requested_by'] == 'alice'
    assert failure['channel'] == '#test'


@pytest.mark.parametrize(
    'error',
    [images.ImageError('OpenAI request failed'), RuntimeError('secret credential')],
)
def test_album_generation_failure_is_persisted(config, monkeypatch, error):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album')
    other = library.create_album('Other', 'Album')
    monkeypatch.setattr(cache, 'generate', Mock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        generate_album_image(library, cache, album['id'], 'alice', '#test')
    assert caught.value is error
    restarted = images.ImageCache(config)
    (failure,) = restarted.album_image_failures(album['id'])
    assert failure['prompt'] == album_prompt(album)
    assert failure['error_type'] == type(error).__name__
    assert failure['created_at']
    assert 'secret credential' not in failure['error']
    assert restarted.album_image_failures(other['id']) == []
    assert library.get_album(album['id'])['images'] == []


def test_album_configuration_failure_is_persisted(config):
    cache = images.ImageCache(dict(config, r2_public_url=''))
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album')
    for _ in range(2):
        with pytest.raises(images.ImageError, match='R2_PUBLIC_URL'):
            generate_album_image(library, cache, album['id'])
    failures = cache.album_image_failures(album['id'])
    assert len(failures) == 2
    assert failures[0]['id'] > failures[1]['id']
    assert all('R2_PUBLIC_URL' in failure['error'] for failure in failures)


def test_failure_logging_storage_error_preserves_original(config, monkeypatch, caplog):
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album')
    error = images.ImageError('Generation failed')
    monkeypatch.setattr(cache, 'get', Mock(side_effect=error))
    monkeypatch.setattr(
        library, 'record_image_failure', Mock(side_effect=sqlite3.OperationalError)
    )
    with pytest.raises(images.ImageError) as caught:
        generate_album_image(library, cache, album['id'])
    assert caught.value is error
    assert 'Could not store album image failure' in caplog.text


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
        'an album cover for the band "Bänd". the name of the album is "First". '
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
        assert 'queued' in images._start_image(
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
    item = images._results.get(timeout=5)
    images._results.put(item)
    assert list(images.image_results()) == ['#test', f'bob: {URL}']
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


def test_result_delivery_respects_silent_mode(config):
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
    failures = cache.album_image_failures(album['id'])
    assert len(failures) == 1
    assert 'upload failed' in failures[0]['error']
    r2.put_object.side_effect = None
    generate_album_image(library, cache, album['id'], 'alice')
    assert library.get_album(album['id'])['images']
    assert post.call_count == 1
    assert cache.album_image_failures(album['id']) == failures


def test_full_queue_does_not_create_album(config, monkeypatch):
    monkeypatch.setattr(images, '_pending', images.queue.Queue(maxsize=1))
    images._pending.put({})
    images._busy.acquire()
    try:
        assert 'queue is full' in images._start_image(
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
            '2025-01-01', '2025-01-02', '2025-01-03', 7, 'upload error', NULL
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


@pytest.mark.parametrize(
    'status,code,message',
    [
        (400, 'invalid_request', 'Invalid image size.'),
        (429, 'rate_limit_exceeded', 'Rate limit reached.'),
        (500, None, 'Internal server error.'),
    ],
)
def test_openai_http_diagnostics(config, post, caplog, status, code, message):
    response = requests.Response()
    response.status_code = status
    response.headers.update(
        {'x-request-id': 'req_123', 'Retry-After': '30', 'Set-Cookie': 'cookie-secret'}
    )
    response._content = json.dumps(
        {
            'error': {
                'code': code,
                'type': 'api_error',
                'message': message,
                'payload': 'secret credential',
            },
            'raw': 'secret credential',
        }
    ).encode()
    original = requests.HTTPError('Authorization: secret credential', response=response)
    post.side_effect = original
    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album')
    with pytest.raises(images.ImageError) as caught:
        generate_album_image(library, cache, album['id'], 'alice')
    diagnostic = str(caught.value)
    assert f'HTTP {status}' in diagnostic
    assert 'request_id=req_123' in diagnostic
    assert 'retry_after=30' in diagnostic
    assert message in diagnostic
    if code:
        assert f'code={code}' in diagnostic
    assert caught.value.__cause__ is original
    assert cache.album_image_failures(album['id'])[0]['error'] == diagnostic
    assert diagnostic in caplog.text
    assert 'secret' not in diagnostic + caplog.text


@pytest.mark.parametrize(
    'exception',
    [requests.ConnectionError, requests.Timeout, requests.HTTPError, ValueError],
)
def test_openai_unstructured_failure(config, post, caplog, exception):
    original = exception('secret credential')
    post.side_effect = original
    with pytest.raises(images.ImageError) as caught:
        images.ImageCache(config).generate('cat')
    assert str(caught.value) == 'OpenAI request failed; please try again later.'
    assert caught.value.__cause__ is original
    assert 'secret' not in caplog.text


@pytest.mark.parametrize(
    'message',
    [
        'Incorrect key: openai-secret',
        'Authorization: Bearer abc',
        'sk-abcdef',
        'Cookie: abc',
        'data:image/png;base64,abc',
        'A' * 100,
        {'dump': 'secret'},
        'Traceback\nsecret',
    ],
)
def test_openai_sensitive_fields_excluded(config, post, caplog, message):
    response = requests.Response()
    response.status_code = 401
    response.headers['x-request-id'] = 'Bearer secret'
    response._content = json.dumps(
        {'error': {'message': message, 'code': 'sk-secret'}}
    ).encode()
    post.side_effect = requests.HTTPError('secret', response=response)
    with pytest.raises(images.ImageError) as caught:
        images.ImageCache(config).generate('cat')
    assert str(caught.value) == 'OpenAI image request failed (HTTP 401).'
    assert 'secret' not in caplog.text


@pytest.mark.parametrize(
    'body', [{'arbitrary': 'secret'}, ['secret'], {'error': 'secret'}, {'error': {}}]
)
def test_openai_arbitrary_body_excluded(body):
    response = Mock(status_code=None, headers={})
    response.json.return_value = body
    assert (
        images.openai_error_message(response)
        == 'OpenAI request failed; please try again later.'
    )


def test_openai_broken_response_diagnostics():
    class Broken:
        @property
        def status_code(self):
            raise RuntimeError('secret')

    assert (
        images.openai_error_message(Broken())
        == 'OpenAI request failed; please try again later.'
    )


def test_openai_transport_status_error(provider_http):
    provider_http(
        {'error': {'code': 'rate_limit_exceeded', 'message': 'Rate limit reached.'}},
        status=429,
    )
    with pytest.raises(
        images.ImageError, match='HTTP 429, code=rate_limit_exceeded'
    ) as caught:
        images.post_json('https://api.openai.com/v1/images/generations', 'OpenAI')
    assert isinstance(caught.value.__cause__, requests.HTTPError)


def test_openai_non_json_error_retains_headers():
    response = requests.Response()
    response.status_code = 502
    response.headers['x-request-id'] = 'req_502'
    response._content = b'<html>secret credential</html>'
    assert images.openai_error_message(response) == (
        'OpenAI image request failed (HTTP 502, request_id=req_502).'
    )


@pytest.mark.parametrize('startup_failure', [False, True])
def test_queue_drains_in_order_after_failure(config, monkeypatch, startup_failure):
    thread = Mock()
    monkeypatch.setattr(images.threading, 'Thread', thread)
    assert images._start_image('first', '#first', 'alice', 'Starting') == 'Starting'
    for prompt, channel, nick in [
        ('second', '#second', 'bob'),
        ('third', '#third', 'carol'),
    ]:
        assert 'queued' in images._start_image(prompt, channel, nick, 'Starting')
    assert thread.call_count == 1
    if startup_failure:
        thread.side_effect = [RuntimeError('secret'), thread.return_value]
    images._results.put(('#first', 'alice: Image request failed'))
    assert list(images.image_results()) == ['#first', 'alice: Image request failed']
    assert images._busy.locked()
    assert thread.call_args.kwargs['args'][1:] == ('second', '#second', 'bob')
    if not startup_failure:
        images._results.put(('#second', 'bob: Image request failed'))
    output = list(images.image_results())
    assert output[0] == '#second'
    assert 'secret' not in output[1]
    assert thread.call_args.kwargs['args'][1:] == ('third', '#third', 'carol')
    images._results.put(('#third', 'carol: done'))
    assert list(images.image_results()) == ['#third', 'carol: done']
    assert not images._busy.locked()
    assert images._pending.empty()


def test_invalid_queued_request_keeps_active_worker(config, monkeypatch):
    images._busy.acquire()
    try:
        monkeypatch.setattr(
            images, 'ImageCache', Mock(side_effect=images.ImageError('HTTPS required'))
        )
        assert 'HTTPS' in images._start_image('cat', '#test', 'alice', 'Starting')
        assert images._busy.locked()
        assert images._pending.empty()
    finally:
        images._busy.release()


def test_queued_music_keeps_album_metadata(config, monkeypatch):
    monkeypatch.setattr(images.threading, 'Thread', Mock())
    images._busy.acquire()
    try:
        result = images._start_image(
            None,
            '#music',
            'bob',
            None,
            album_data={'artist': 'Queued Band', 'title': 'Queued Album'},
        )
        assert 'queued (1 waiting)' in result
        assert 'Band: Queued Band; Album: Queued Album' in result
        job = images._pending.get_nowait()
        assert job['kwargs'] == {'album_id': 1}
        assert job['args'][2:] == ('#music', 'bob')
        album = MusicLibrary(job['args'][0].database).get_album(1)
        assert album['created_by'] == 'bob'
    finally:
        images._busy.release()


def test_three_concurrent_workers_and_queue(config, monkeypatch):
    slots = threading.BoundedSemaphore(3)
    monkeypatch.setattr(images, '_busy', slots)
    started = images.queue.Queue()
    finish = threading.Event()

    def get(cache, prompt, nick, channel):
        started.put(prompt)
        assert finish.wait(5)
        return URL

    monkeypatch.setattr(images.ImageCache, 'get', get)
    try:
        for prompt in ('first', 'second', 'third'):
            assert (
                images._start_image(prompt, '#test', prompt, 'Starting') == 'Starting'
            )
        assert {started.get(timeout=5) for _ in range(3)} == {
            'first',
            'second',
            'third',
        }
        assert not slots.acquire(blocking=False)
        assert 'queued (1 waiting)' in images._start_image(
            'fourth', '#fourth', 'bob', 'Starting'
        )
        assert started.empty()
        assert list(images.image_results()) == []
    finally:
        finish.set()
    outputs = []
    for _ in range(4):
        item = images._results.get(timeout=5)
        images._results.put(item)
        outputs.append(list(images.image_results()))
    assert started.get(timeout=5) == 'fourth'
    assert ['#fourth', f'bob: {URL}'] in outputs
    assert images._pending.empty()
    # Every slot is restored, including the one reused for the queued job.
    for _ in range(3):
        assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    for _ in range(3):
        slots.release()


@pytest.mark.parametrize('existing', [False, True])
def test_image_ancestry_initialization(config, existing):
    from contextlib import closing

    cache = images.ImageCache(config)
    with closing(sqlite3.connect(cache.database, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        if existing:
            # Construct the actual pre-F4 schema, including all metadata columns.
            images.initialize_image_records(db)
            for name in ('insert', 'update', 'delete', 'replace'):
                db.execute('DROP TRIGGER image_ancestry_' + name)
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'image_cache'"
            ).fetchone()[0]
            db.execute('DROP TABLE image_cache')
            db.execute(
                schema.replace(
                    ',\n            parent_image_id INTEGER REFERENCES image_cache(id)',
                    '',
                )
            )
            db.execute(
                """INSERT INTO image_cache (cache_key, prompt, normalized_prompt,
                settings_json, local_filename, generation_metadata_json, requested_by)
                VALUES ('old', 'original', 'original', '{}', 'old.png', '{\"usage\":42}', 'alice')"""
            )
            before = dict(db.execute('SELECT * FROM image_cache').fetchone())
        for _ in range(3):
            images.initialize_image_records(db)
        columns = {
            row['name']: row for row in db.execute('PRAGMA table_info(image_cache)')
        }
        assert columns['parent_image_id']['notnull'] == 0
        foreign_key = db.execute('PRAGMA foreign_key_list(image_cache)').fetchone()
        assert (foreign_key['table'], foreign_key['from'], foreign_key['to']) == (
            'image_cache',
            'parent_image_id',
            'id',
        )
        if existing:
            after = dict(db.execute('SELECT * FROM image_cache').fetchone())
            assert after.pop('parent_image_id') is None
            assert after == before


def test_image_ancestry_persistence_and_retries(config, post):
    from contextlib import closing

    cache = images.ImageCache(config)
    with closing(cache.connect()) as db:
        cache.persist_image(db, 'root', 'root', 'root.png', {})
        root = db.execute('SELECT * FROM image_cache').fetchone()
        assert root['parent_image_id'] is None
        cache.persist_image(
            db, 'child', 'child', 'child.png', {}, parent_image_id=root['id']
        )
        child = db.execute(
            "SELECT * FROM image_cache WHERE cache_key = 'child'"
        ).fetchone()
        cache.persist_image(
            db, 'grandchild', 'grandchild', 'g.png', {}, parent_image_id=child['id']
        )
        for parent in (None, root['id']):
            cache.persist_image(
                db, 'child', 'retry', 'child.png', {}, parent_image_id=parent
            )
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            cache.persist_image(
                db, 'child', 'retry', 'child.png', {}, parent_image_id=child['id']
            )
        with pytest.raises(sqlite3.IntegrityError, match='Invalid image parent'):
            cache.persist_image(
                db, 'invalid', 'invalid', 'x.png', {}, parent_image_id=99999
            )
        assert db.execute('SELECT COUNT(*) FROM image_cache').fetchone()[0] == 3
    assert cache.get_image(child['id'])['parent_image_id'] == root['id']
    post.assert_not_called()


@pytest.mark.parametrize('foreign_keys', [False, True])
def test_image_ancestry_sql_constraints(config, foreign_keys):
    from contextlib import closing

    cache = images.ImageCache(config)
    with closing(cache.connect()) as db:
        db.execute('PRAGMA foreign_keys = ' + str(int(foreign_keys)))
        cache.persist_image(db, 'root', 'root', 'root.png', {})
        cache.persist_image(db, 'child', 'child', 'child.png', {}, parent_image_id=1)
        # Self-parenting with an explicit ID and a two-node cycle cannot be inserted.
        for identifier, parent in [(3, 3), (3, 999), (3, 4), (4, 3)]:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO image_cache
                    (id, cache_key, prompt, normalized_prompt, settings_json,
                     local_filename, generation_metadata_json, parent_image_id)
                    VALUES (?, ?, 'x', 'x', '{}', 'x.png', '{}', ?)""",
                    (identifier, str(identifier), parent),
                )
        for identifier, parent in [(1, 2), (1, 1), (1, 999), (2, None)]:
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(
                    'UPDATE image_cache SET parent_image_id = ? WHERE id = ?',
                    (parent, identifier),
                )
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute('UPDATE image_cache SET id = 99 WHERE id = 1')
        with pytest.raises(sqlite3.IntegrityError, match='children'):
            db.execute('DELETE FROM image_cache WHERE id = 1')
        with pytest.raises(sqlite3.IntegrityError, match='already persisted'):
            db.execute(
                """INSERT OR REPLACE INTO image_cache
                (id, cache_key, prompt, normalized_prompt, settings_json,
                 local_filename, generation_metadata_json)
                VALUES (2, 'child', 'x', 'x', '{}', 'x.png', '{}')"""
            )
        db.execute('UPDATE image_cache SET parent_image_id = parent_image_id')
        assert (
            db.execute(
                'SELECT parent_image_id FROM image_cache WHERE id = 2'
            ).fetchone()[0]
            == 1
        )


@pytest.fixture
def responses_post(config, post):
    config.update(image_api='responses', responses_model='gpt-5')
    post.return_value.json.return_value = {
        'id': 'resp_root',
        'status': 'completed',
        'store': True,
        'model': 'gpt-5-snapshot',
        'created_at': 123,
        'usage': {'total_tokens': 42},
        'output': [
            {'type': 'message', 'content': []},
            {
                'type': 'image_generation_call',
                'id': 'ig_root',
                'status': 'completed',
                'result': base64.b64encode(PNG).decode(),
                'revised_prompt': 'A revised instruction',
                'quality': 'low',
            },
        ],
    }
    return post


@pytest.mark.parametrize('mode', [None, 'images', 'responses'])
def test_image_api_modes(config, post, mode):
    if mode:
        config['image_api'] = mode
    if mode == 'responses':
        config['responses_model'] = 'gpt-5'
    cache = images.ImageCache(config)
    assert cache.image_api == (mode or 'images')
    if mode != 'responses':
        cache.get('cat')
        assert post.call_args.args[0].endswith('/images/generations')
        assert post.call_args.kwargs['json'] == dict(cache.settings, prompt='cat', n=1)
        assert (
            json.loads(read_row(cache)['generation_metadata_json'])['api'] == 'images'
        )


@pytest.mark.parametrize(
    'settings',
    [
        {'image_api': 'invalid'},
        {'image_api': None},
        {'image_api': []},
        {'image_api': 'responses'},
        {'image_api': 'responses', 'responses_model': ''},
        {'image_api': 'responses', 'responses_model': 42},
        {
            'image_api': 'responses',
            'responses_model': 'gpt-5',
            'responses_store': 'false',
        },
    ],
)
def test_invalid_image_api_configuration(config, post, settings):
    with pytest.raises(images.ImageError):
        images.ImageCache(dict(config, **settings))
    post.assert_not_called()


def test_responses_generation_persistence_and_cache(config, responses_post):
    cache = images.ImageCache(config)
    original = '  Exact Café\n prompt  '
    cache.get(original, 'alice', '#test')
    saved = dict(read_row(cache))
    metadata = json.loads(saved['generation_metadata_json'])
    assert metadata['response_id'] == 'resp_root'
    assert metadata['image_generation_call_id'] == 'ig_root'
    assert metadata['model'] == 'gpt-5-snapshot'
    assert metadata['requested_model'] == 'gpt-5'
    assert metadata['tool'] == dict(cache.settings, type='image_generation')
    assert metadata['image_settings'] == {'quality': 'low'}
    assert metadata['revised_prompt'] == 'A revised instruction'
    assert metadata['usage'] == {'total_tokens': 42}
    assert metadata['store'] is True
    assert metadata['previous_response_id'] is None
    assert 'result' not in metadata and 'output' not in metadata
    assert saved['prompt'] == original
    assert saved['parent_image_id'] is None
    assert Path(saved['local_filename']).read_bytes() == PNG
    assert json.loads(saved['settings_json'])['responses_model'] == 'gpt-5'
    assert responses_post.call_args.args[0] == 'https://api.openai.com/v1/responses'
    assert responses_post.call_args.kwargs['json'] == {
        'model': 'gpt-5',
        'input': original,
        'tools': [dict(cache.settings, type='image_generation')],
        'tool_choice': {'type': 'image_generation'},
        'store': True,
    }
    restarted = images.ImageCache(config)
    restarted.get(original)
    assert responses_post.call_count == 1
    assert (
        restarted.get_image(saved['id'])['generation_metadata_json']
        == saved['generation_metadata_json']
    )
    assert cache.cache_key(original) != cache.cache_key(original.strip())
    assert cache.cache_key(original) != images.ImageCache(
        dict(config, image_api='images')
    ).cache_key(original)
    assert cache.cache_key(original) != images.ImageCache(
        dict(config, responses_model='other')
    ).cache_key(original)
    assert cache.cache_key(original) != images.ImageCache(
        dict(config, responses_store=False)
    ).cache_key(original)


def test_responses_continuation_and_branching_without_source_bitmap(
    config, responses_post, r2
):
    cache = images.ImageCache(config)
    cache.get('root')
    root = dict(read_row(cache))
    # Context continuation must work even when no local source bitmap exists.
    Path(root['local_filename']).unlink()
    responses_post.return_value.json.return_value['id'] = 'resp_child'
    child = cache.continue_image(root['id'], '  Make it blue\n ', 'bob', '#other')
    assert (
        responses_post.call_args.kwargs['json']['previous_response_id'] == 'resp_root'
    )
    assert (
        responses_post.call_args.kwargs['json']['input']
        == child['prompt']
        == '  Make it blue\n '
    )
    assert child['parent_image_id'] == root['id']
    assert child['requested_by'] == 'bob'
    assert child['channel'] == '#other'
    responses_post.return_value.json.return_value['id'] = 'resp_grandchild'
    grandchild = cache.continue_image(child['id'], 'Make it green')
    assert (
        responses_post.call_args.kwargs['json']['previous_response_id'] == 'resp_child'
    )
    assert grandchild['parent_image_id'] == child['id']
    responses_post.return_value.json.return_value['id'] = 'resp_branch'
    branch = cache.continue_image(root['id'], 'Make it green')
    assert (
        responses_post.call_args.kwargs['json']['previous_response_id'] == 'resp_root'
    )
    assert branch['parent_image_id'] == root['id']
    assert len({root['id'], child['id'], grandchild['id'], branch['id']}) == 4
    assert cache.get_image(root['id']) == root
    for call in responses_post.call_args_list:
        assert set(call.kwargs) == {'headers', 'json', 'timeout'}
        assert isinstance(call.kwargs['json']['input'], str)
        assert 'image' not in call.kwargs['json']
        assert 'files' not in call.kwargs
    assert responses_post.call_count == r2.put_object.call_count == 4


def test_structured_album_continuation_contract(config, responses_post):
    from dataclasses import asdict, replace
    from pmxbot.music import GenerationInputs, continue_album_image

    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album', genre='Jazz')
    generate_album_image(library, cache, album['id'])
    source = dict(read_row(cache))
    canonical = library.get_album(album['id'])
    draft = replace(
        GenerationInputs.from_source(canonical, source),
        description='Exact\n album description',
        artist_description='Band visual cue',
        artist_genre='Rock',
    )
    responses_post.return_value.json.return_value['id'] = 'resp_child'
    child = continue_album_image(library, cache, source['id'], draft, 'bob', '#test')
    assert child['parent_image_id'] == source['id']
    assert json.loads(child['generation_metadata_json'])['generation_inputs'] == asdict(
        draft
    )
    assert 'Exact\n album description' in child['prompt']
    assert 'Band visual cue' in child['prompt'] and 'Rock' in child['prompt']
    assert responses_post.call_args.kwargs['json']['input'] == child['prompt']
    saved = library.get_album(album['id'])
    assert saved == dict(
        canonical,
        images=sorted(
            canonical['images'] + [{'cache_key': child['cache_key']}],
            key=lambda item: item['cache_key'],
        ),
    )
    assert cache.get_image(source['id']) == source
    responses_post.reset_mock()
    for invalid in (
        replace(draft, title='Rename'),
        replace(draft, source_image_id=999),
    ):
        with pytest.raises(images.ImageError):
            continue_album_image(library, cache, source['id'], invalid)
    responses_post.assert_not_called()


@pytest.mark.parametrize(
    'metadata',
    [
        {},
        {'api': 'images', 'response_id': 'resp_fake', 'store': True},
        {'api': 'responses', 'store': True},
        {'api': 'responses', 'store': True, 'response_id': 'not-a-response'},
        {'api': 'responses', 'store': False, 'response_id': 'resp_root'},
        {'api': 'responses', 'store': True, 'response_id': 42},
        [],
    ],
)
def test_continuation_rejects_unsupported_context(config, post, metadata):
    cache = images.ImageCache(config)
    cache.get('root')
    source = dict(read_row(cache))
    with sqlite3.connect(cache.database) as db:
        db.execute(
            'UPDATE image_cache SET generation_metadata_json = ?',
            (json.dumps(metadata),),
        )
    config.update(image_api='responses', responses_model='gpt-5')
    post.reset_mock()
    with pytest.raises(images.ImageError, match='continuation unavailable'):
        images.ImageCache(config).continue_image(source['id'], 'blue')
    post.assert_not_called()


@pytest.mark.parametrize('store', [False, True])
def test_responses_store_context_policy(config, responses_post, store):
    config['responses_store'] = store
    # Account policy may override store=True (e.g. Zero Data Retention).
    responses_post.return_value.json.return_value['store'] = False
    cache = images.ImageCache(config)
    cache.get('root')
    assert responses_post.call_args.kwargs['json']['store'] is store
    source = dict(read_row(cache))
    assert images.response_reference(source) is None
    with pytest.raises(images.ImageError, match='no stored response context'):
        cache.continue_image(source['id'], 'blue')
    assert responses_post.call_count == 1


def test_switch_to_images_disables_continuation_without_mutation(
    config, responses_post
):
    cache = images.ImageCache(config)
    cache.get('root')
    source = dict(read_row(cache))
    switched = images.ImageCache(dict(config, image_api='images'))
    with pytest.raises(images.ImageError, match='configure image_api'):
        switched.continue_image(source['id'], 'blue')
    assert switched.get_image(source['id']) == source
    with pytest.raises(images.ImageError, match='Unknown source'):
        cache.continue_image(999, 'blue')
    assert responses_post.call_count == 1


@pytest.mark.parametrize(
    'change',
    [
        {'id': None},
        {'id': 'ig_wrong'},
        {'status': 'incomplete'},
        {'error': {'message': 'secret'}},
        {'output': None},
        {'output': []},
        {'output': [{'type': 'message'}]},
        {
            'output': [
                {
                    'type': 'image_generation_call',
                    'id': 'ig_x',
                    'status': 'failed',
                    'result': base64.b64encode(PNG).decode(),
                }
            ]
        },
        {
            'output': [
                {
                    'type': 'image_generation_call',
                    'id': 'ig_x',
                    'status': 'completed',
                    'result': 'invalid',
                }
            ]
        },
    ],
)
def test_invalid_responses_do_not_persist_or_upload(config, responses_post, r2, change):
    responses_post.return_value.json.return_value.update(change)
    cache = images.ImageCache(config)
    with pytest.raises(images.ImageError, match='single completed PNG'):
        cache.get('root')
    assert read_row(cache) is None
    r2.put_object.assert_not_called()


def test_responses_multiple_outputs_require_unambiguous_image(
    config, responses_post, r2
):
    data = responses_post.return_value.json.return_value
    data['output'].insert(
        0, {'type': 'image_generation_call', 'status': 'completed', 'result': 'bad'}
    )
    cache = images.ImageCache(config)
    cache.get('one image plus other outputs')
    data['output'].append(dict(data['output'][-1], id='ig_other'))
    with pytest.raises(images.ImageError, match='single completed PNG'):
        cache.get('multiple images')
    assert r2.put_object.call_count == 1


@pytest.mark.parametrize(
    'status,code',
    [
        (404, 'not_found'),
        (400, 'previous_response_not_found'),
        (403, 'permission_denied'),
        (429, 'rate_limit_exceeded'),
    ],
)
def test_unavailable_responses_context_is_safe_and_has_no_fallback(
    config, responses_post, r2, status, code
):
    cache = images.ImageCache(config)
    cache.get('root')
    source = dict(read_row(cache))
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(
        {'error': {'code': code, 'message': 'secret openai-secret'}}
    ).encode()
    responses_post.side_effect = requests.HTTPError(
        'secret credentials', response=response
    )
    with pytest.raises(images.ImageError) as caught:
        cache.continue_image(source['id'], 'blue')
    assert 'expired, deleted, or inaccessible' in str(caught.value)
    assert 'No source image was uploaded' in str(caught.value)
    assert 'secret' not in str(caught.value)
    assert cache.get_image(source['id']) == source
    with sqlite3.connect(cache.database) as db:
        assert db.execute('SELECT COUNT(*) FROM image_cache').fetchone()[0] == 1
    assert responses_post.call_count == 2
    assert r2.put_object.call_count == 1


def test_responses_upload_failure_keeps_id_and_context_for_retry(
    config, responses_post, r2
):
    cache = images.ImageCache(config)
    cache.get('root')
    source = dict(read_row(cache))
    responses_post.return_value.json.return_value['id'] = 'resp_child'
    r2.put_object.side_effect = images.BotoCoreError()
    with pytest.raises(images.ImageError, match='saved locally'):
        cache.continue_image(source['id'], 'blue')
    with sqlite3.connect(cache.database) as db:
        child_id = db.execute(
            'SELECT id FROM image_cache WHERE parent_image_id = ?', (source['id'],)
        ).fetchone()[0]
    child = cache.get_image(child_id)
    assert images.response_reference(child) == 'resp_child'
    assert child['hosted_url'] is None
    r2.put_object.side_effect = None
    retried = cache.retry_upload(child_id)
    assert retried['hosted_url'] and retried['id'] == child_id
    assert retried['generation_metadata_json'] == child['generation_metadata_json']
    assert responses_post.call_count == 2
    Path(retried['local_filename']).unlink()
    with sqlite3.connect(cache.database) as db:
        db.execute('UPDATE image_cache SET hosted_url = NULL WHERE id = ?', (child_id,))
    with pytest.raises(images.ImageError, match='refusing to replace'):
        cache.retry_upload(child_id)
    assert responses_post.call_count == 2


def test_continuation_storage_failure_is_safe(config, responses_post, monkeypatch):
    cache = images.ImageCache(config)
    cache.get('root')
    source = dict(read_row(cache))
    monkeypatch.setattr(
        cache,
        'persist_image',
        Mock(side_effect=sqlite3.OperationalError('secret path')),
    )
    with pytest.raises(images.ImageError, match='check image storage') as caught:
        cache.continue_image(source['id'], 'blue')
    assert 'secret' not in str(caught.value)
    assert cache.get_image(source['id']) == source
    with sqlite3.connect(cache.database) as db:
        assert db.execute('SELECT COUNT(*) FROM image_cache').fetchone()[0] == 1


def test_responses_records_refuse_replacement(config, responses_post):
    from contextlib import closing

    cache = images.ImageCache(config)
    cache.get('root')
    source = dict(read_row(cache))
    with closing(cache.connect()) as db:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            cache.persist_image(
                db, source['cache_key'], 'replacement', 'different.png', {}
            )
    assert cache.get_image(source['id']) == source


def test_album_continuation_link_failure_rolls_back_child(config, responses_post):
    from contextlib import closing
    from pmxbot.music import GenerationInputs, continue_album_image

    cache = images.ImageCache(config)
    library = MusicLibrary(cache.database)
    album = library.create_album('Band', 'Album')
    generate_album_image(library, cache, album['id'])
    source = dict(read_row(cache))
    draft = GenerationInputs.from_source(album, source)
    with closing(cache.connect()) as db:
        db.execute(
            '''CREATE TRIGGER reject_child_link BEFORE INSERT ON album_images
                      BEGIN SELECT RAISE(ABORT, 'secret storage detail'); END'''
        )
    with pytest.raises(images.ImageError, match='check image storage'):
        continue_album_image(library, cache, source['id'], draft)
    assert cache.get_image(source['id']) == source
    with sqlite3.connect(cache.database) as db:
        assert db.execute('SELECT COUNT(*) FROM image_cache').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM album_images').fetchone()[0] == 1
    failure = cache.album_image_failures(album['id'])[0]
    assert failure['prompt'] == responses_post.call_args.kwargs['json']['input']
    assert 'secret' not in failure['error']


@pytest.mark.parametrize('stage', ['read', 'save'])
def test_continuation_filesystem_failure_is_safe(
    config, responses_post, monkeypatch, r2, stage
):
    cache = images.ImageCache(config)
    cache.get('root')
    source = dict(read_row(cache))
    operation = 'get_image' if stage == 'read' else 'save'
    monkeypatch.setattr(cache, operation, Mock(side_effect=OSError('secret location')))
    with pytest.raises(images.ImageError, match='check image storage') as caught:
        cache.continue_image(source['id'], 'blue')
    assert 'secret' not in str(caught.value)
    assert r2.put_object.call_count == 1
    assert responses_post.call_count == (1 if stage == 'read' else 2)


@pytest.mark.parametrize('source_id', [None, True, '1', -1, 0, 2**63])
def test_continuation_source_id_validation(config, responses_post, source_id):
    cache = images.ImageCache(config)
    with pytest.raises(images.ImageError, match='Invalid source image ID'):
        cache.continue_image(source_id, 'blue')
    responses_post.assert_not_called()
