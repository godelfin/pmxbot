import base64
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import pmxbot
from pmxbot import core, images

PNG = b'\x89PNG\r\n\x1a\nimage bytes'
URL = 'https://i.ibb.co/example/image.png'


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = {
        'database': f'sqlite:{tmp_path / "pmxbot.sqlite"}',
        'images_enabled': True,
        'images_directory': str(tmp_path / 'images'),
        'openai_api_key': 'openai-secret',
        'imgbb_api_key': 'imgbb-secret',
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
    upload = Mock()
    upload.json.return_value = {
        'success': True,
        'data': {'url': URL, 'delete_url': 'https://ibb.co/delete'},
    }
    post = Mock(side_effect=[generation, upload])
    monkeypatch.setattr(images.requests, 'post', post)
    return post


def read_row(cache):
    with sqlite3.connect(str(cache.database)) as db:
        db.row_factory = sqlite3.Row
        return db.execute('SELECT * FROM image_cache').fetchone()


def test_generate_persist_and_reuse(config, post):
    cache = images.ImageCache(config)
    assert cache.get('  A CAFÉ\tcat ', 'alice', '#test') == URL
    row = read_row(cache)
    assert Path(row['local_filename']).read_bytes() == PNG
    assert row['normalized_prompt'] == 'a café cat'
    assert row['requested_by'] == 'alice'
    assert row['channel'] == '#test'
    assert json.loads(row['generation_metadata_json'])['usage']['total_tokens'] == 42
    assert 'b64_json' not in row['generation_metadata_json']
    assert json.loads(row['host_metadata_json'])['delete_url']
    request = post.call_args_list[0].kwargs
    assert request['json']['prompt'] == '  A CAFÉ\tcat '
    assert request['json']['output_format'] == 'png'
    assert request['headers']['Authorization'] == 'Bearer openai-secret'
    assert post.call_args_list[1].kwargs['data']['key'] == 'imgbb-secret'
    # A new instance simulates a restart; credentials are not needed for a hit.
    config.update(openai_api_key='', imgbb_api_key='')
    assert images.ImageCache(config).get('a cafe\u0301 CAT') == URL
    assert post.call_count == 2
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
        assert cache.get('cat') == URL
        assert main.db.execute('SELECT hosted_url FROM image_cache').fetchone() == (
            URL,
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


def test_upload_failure_retries_only_upload(config, post):
    generation, upload = list(post.side_effect)
    post.side_effect = [generation, requests.Timeout('secret'), upload]
    cache = images.ImageCache(config)
    with pytest.raises(images.ImageError, match='saved locally'):
        cache.get('cat')
    assert read_row(cache)['hosted_url'] is None
    assert read_row(cache)['last_error']
    assert cache.get('CAT') == URL
    assert post.call_count == 3
    assert post.call_args_list[2].args[0] == 'https://api.imgbb.com/1/upload'
    assert read_row(cache)['last_error'] is None


def test_missing_local_file_regenerates_pending_upload(config, post):
    generation, upload = list(post.side_effect)
    post.side_effect = [generation, requests.Timeout(), generation, upload]
    cache = images.ImageCache(config)
    with pytest.raises(images.ImageError):
        cache.get('cat')
    Path(read_row(cache)['local_filename']).unlink()
    assert cache.get('cat') == URL
    assert post.call_count == 4


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
    config['imgbb_api_key'] = ''
    monkeypatch.delenv('IMGBB_API_KEY', raising=False)
    with pytest.raises(images.ImageError, match='IMGBB_API_KEY'):
        images.ImageCache(config).get('cat')
    post.assert_not_called()


def test_provider_error_is_sanitized(config, post):
    post.side_effect = requests.HTTPError('Authorization: secret')
    with pytest.raises(images.ImageError, match='OpenAI request failed') as caught:
        images.ImageCache(config).get('cat')
    assert 'secret' not in str(caught.value)


def test_invalid_upload_preserves_local_file(config, post):
    generation, upload = list(post.side_effect)
    upload.json.return_value = {
        'success': True,
        'data': {'url': 'https://ibb.co/\r\ninject'},
    }
    post.side_effect = [generation, upload]
    cache = images.ImageCache(config)
    with pytest.raises(images.ImageError, match='saved locally'):
        cache.get('cat')
    assert Path(read_row(cache)['local_filename']).is_file()
    assert read_row(cache)['hosted_url'] is None


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
    monkeypatch.setattr(core, '_load_library_extensions', lambda: None)
    monkeypatch.setattr(core, '_load_bot_class', lambda: Mock())
    with caplog.at_level('INFO'):
        core.initialize(config)
    assert 'openai-secret' not in caplog.text
    assert 'imgbb-secret' not in caplog.text
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
