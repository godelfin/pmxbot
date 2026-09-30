import base64
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import pmxbot
from pmxbot import core, images, quotes
from pmxbot.music import MusicLibrary, album_prompt, generate_album_image

PNG = b'\x89PNG\r\n\x1a\nimage bytes'
URL = 'https://images.example.com/image.png'


def hosted_url(cache, prompt):
    return f"{cache.r2['public_url'].rstrip('/')}/{cache.cache_key(prompt)}.png"


@pytest.fixture
def music_store(config, monkeypatch):
    store = quotes.SQLiteQuotes(config['database'])
    monkeypatch.setattr(quotes.Quotes, 'store', store, raising=False)
    yield store
    store.close()


@pytest.mark.parametrize('mapped', [False, True])
@pytest.mark.parametrize('genre', ['Jazz', 'Fusion', 'Anime'])
def test_music_prompt_and_shared_worker(
    config, music_store, monkeypatch, mapped, genre
):
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
        return genre if genre in options else options[0]

    monkeypatch.setattr(images.random, 'choice', select)
    thread = Mock()
    monkeypatch.setattr(images.threading, 'Thread', thread)
    try:
        assert (
            images.music('#test', 'alice')
            == 'Looking up or generating your album cover... '
            'Band: Second Band; Album: First Album; '
            f'Format: Vinyl; Description: Remastered; Genre: {genre}'
        )
        thread.return_value.start.assert_called_once_with()
        kwargs = thread.call_args.kwargs
        assert kwargs['target'] is images._generate
        assert kwargs['kwargs'] == {'album_id': 1}
        cache, prompt, channel, nick = kwargs['args']
        assert isinstance(cache, images.ImageCache)
        assert prompt is None
        album = MusicLibrary(cache.database).get_album(1)
        assert album['cache_key'] is None
        assert (
            album_prompt(album)
            == 'an album cover for the band Second Band. the name of the album is First Album. '
            f'this is the Vinyl, Remastered edition. the genre of music is {genre}, '
            'but nowhere should the genre be mentioned.'
        )
        assert set(choices[-1]) == {'Jazz', 'Fusion', 'Anime'}
        assert (channel, nick) == ('#test', 'alice')
        assert choose.call_count == 2
        assert 'already running' in images.image('cat', '#test', 'bob')
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
    assert 'disabled' in images.music('#test', 'alice')
    start.assert_not_called()


def test_music_registered():
    assert next(core.Handler.find_matching('!music', '#test')).func is images.music


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = {
        'database': f'sqlite:{tmp_path / "pmxbot.sqlite"}',
        'images_enabled': True,
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
        return list(images.image_results())[1]

    first_result = result(first['id'])
    assert (
        first_result
        == f"alice: #{first['id']} {hosted_url(cache, album_prompt(first))}"
    )
    assert (
        result(second['id'])
        == f"alice: #{second['id']} {hosted_url(cache, album_prompt(second))}"
    )
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
    assert (
        first['cache_key']
        is first['image_created_at']
        is first['image_created_by']
        is None
    )
    restarted = MusicLibrary(cache.database)
    assert (
        restarted.create_album('  BÄND ', 'FIRST', genre='Rock', created_by='bob')
        == first
    )
    second = restarted.create_album('Bänd', 'Second')
    assert second['artist_id'] == first['artist_id']
    assert restarted.create_album('Other', 'First')['id'] != first['id']
    url = generate_album_image(restarted, cache, first['id'], 'bob', '#test')
    assert url == hosted_url(cache, album_prompt(first))
    saved = restarted.get_album(first['id'])
    assert saved['created_by'] == 'alice'
    assert saved['image_created_by'] == 'bob'
    assert saved['image_created_at']
    assert saved['cache_key'] == cache.cache_key(album_prompt(first))
    generate_album_image(restarted, cache, first['id'], 'carol', '#test')
    assert restarted.get_album(first['id']) == saved
    assert post.call_count == 1


def test_legacy_image_ids_are_reserved(config):
    cache = images.ImageCache(config)
    with sqlite3.connect(str(cache.database)) as db:
        db.execute(
            'CREATE TABLE music_image_ids (id INTEGER PRIMARY KEY, cache_key TEXT UNIQUE)'
        )
        db.execute("INSERT INTO music_image_ids VALUES (42, 'legacy')")
    library = MusicLibrary(cache.database)
    assert library.create_album('Band', 'Album')['id'] == 43
    assert library.create_album('Band', 'Album')['id'] == 43
    with sqlite3.connect(str(cache.database)) as db:
        assert db.execute('SELECT * FROM music_image_ids').fetchall() == [
            (42, 'legacy')
        ]


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
    assert 'SQLite main bot database' in images.image('cat', '#test', 'alice')
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


def test_command_runs_in_background_and_delivers_result(config, monkeypatch):
    started, finish = threading.Event(), threading.Event()

    def get(*args):
        started.set()
        assert finish.wait(5)
        return URL

    monkeypatch.setattr(images.ImageCache, 'get', get)
    assert 'Looking up' in images.image('cat', '#test', 'alice')
    try:
        assert started.wait(5)
        assert 'already running' in images.image('dog', '#test', 'bob')
        assert list(images.image_results()) == []
    finally:
        finish.set()
    item = images._results.get(timeout=5)
    images._results.put(item)
    output = list(images.image_results())
    assert isinstance(output[0], core.SwitchChannel)
    assert output == ['#test', f'alice: {URL}']
    assert not images._busy.locked()


def test_command_disabled_and_empty_prompt(config, post):
    assert images.image('  ', '#test', 'alice') == 'Usage: !image <prompt>'
    config['images_enabled'] = False
    assert 'disabled' in images.image('cat', '#test', 'alice')
    post.assert_not_called()


def test_command_registration():
    assert next(core.Handler.find_matching('!image cat', '#test')).func is images.image
    assert (
        next(core.Handler.find_matching('!imagine cat', '#test')).func is images.image
    )


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
    assert library.get_album(album['id'])['cache_key']
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
    assert album['cache_key'] is None


def test_concurrent_album_creation_reuses_pair(config):
    from concurrent.futures import ThreadPoolExecutor

    library = MusicLibrary(images.ImageCache(config).database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: library.create_album('Band', 'Album'), range(8)))
    assert all(row == rows[0] for row in rows)
