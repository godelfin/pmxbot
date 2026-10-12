import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from pmxbot import jobs_cli
from pmxbot.jobs import JobStore
from tests.unit.test_album_page import page, variation  # noqa: F401
from tests.unit.test_jobs import draft


@pytest.fixture
def queue(variation, tmp_path):
    cache, *_ = variation
    store = JobStore(cache.database)
    identifiers = []
    for index in range(4):
        identifier = store.enqueue(replace(draft(cache), description=str(index)), cache)
        identifiers.append(identifier)
        if index < 3:
            store.claim()
            if index == 0:
                store.finish(identifier, image_id=123)
            elif index == 1:
                store.finish(identifier, error='Safe failure')
    config = tmp_path / 'config.yaml'
    # Inspection must work despite invalid provider settings and no credentials.
    config.write_text(
        f'database: sqlite:{cache.database}\nimage_api: responses\n', encoding='utf-8'
    )
    return store, identifiers, config


def test_list_counts_filters_and_read_only(queue, capsys):
    store, ids, config = queue
    before = store.database.read_bytes()
    jobs_cli.run([str(config)])
    output = capsys.readouterr().out
    assert 'queued: 1  running: 1  succeeded: 1  failed: 1' in output
    assert all(identifier[:12] in output for identifier in ids)
    jobs_cli.run([str(config), '--status', 'pending', '--json'])
    result = json.loads(capsys.readouterr().out)
    assert {row['status'] for row in result['jobs']} == {'running', 'queued'}
    assert result['counts']['succeeded'] == 1
    jobs_cli.run([str(config), '--status', 'failed', '--limit', '1', '--json'])
    result = json.loads(capsys.readouterr().out)
    assert len(result['jobs']) == 1 and result['jobs'][0]['error'] == 'Safe failure'
    assert store.database.read_bytes() == before
    assert store.get(ids[2])['status'] == 'running'  # No recovery or claiming.


def test_details_unique_prefix_and_safe_json(queue, capsys):
    store, ids, config = queue
    jobs_cli.run(['--database', str(store.database), '--job', ids[0][:12]])
    output = capsys.readouterr().out
    assert f'id: {ids[0]}' in output
    assert 'result_image_id: 123' in output
    assert 'image_api: images' in output
    jobs_cli.run([str(config), '--job', ids[1], '--json'])
    item = json.loads(capsys.readouterr().out)['jobs'][0]
    assert item['error'] == 'Safe failure'
    assert 'inputs_json' not in item and 'anonymous_owner' not in item
    assert 'openai_api_key' not in item['settings']


def test_kind_filter_and_terminal_controls(queue, capsys):
    store, ids, config = queue
    with closing(store.connect()) as db:
        db.execute(
            "UPDATE generation_jobs SET kind='music', requested_by=? WHERE id=?",
            ('alice\x1b[31m\nnext', ids[0]),
        )
    jobs_cli.run([str(config), '--kind', 'music'])
    output = capsys.readouterr().out
    assert ids[0][:12] in output and ids[1][:12] not in output
    assert '\x1b' not in output and 'alice [31m next' in output


@pytest.mark.parametrize(
    'options',
    [
        ['--limit', '0'],
        ['--limit', '1001'],
        ['--limit', 'bad'],
        ['--status', 'bad'],
        ['--job', 'bad'],
        ['--job', 'a' * 32, '--kind', 'music'],
    ],
)
def test_bad_arguments(queue, options):
    _, _, config = queue
    with pytest.raises(SystemExit) as error:
        jobs_cli.run([str(config), *options])
    assert error.value.code == 2


def test_unknown_ambiguous_and_missing_database(queue, capsys, tmp_path):
    store, ids, config = queue
    with pytest.raises(SystemExit) as error:
        jobs_cli.run([str(config), '--job', '0' * 32])
    assert error.value.code == 1
    assert 'Unknown generation job' in capsys.readouterr().err
    with closing(store.connect()) as db:
        for index, identifier in enumerate(ids[:2]):
            db.execute(
                'UPDATE generation_jobs SET id=? WHERE id=?',
                ('a' * 12 + str(index) * 20, identifier),
            )
    with pytest.raises(SystemExit):
        jobs_cli.run([str(config), '--job', 'a' * 12])
    assert 'ambiguous' in capsys.readouterr().err
    missing = tmp_path / 'missing.sqlite'
    with pytest.raises(SystemExit):
        jobs_cli.run(['--database', str(missing)])
    assert not missing.exists()


def test_empty_database_and_legacy_schema(tmp_path, capsys):
    database = tmp_path / 'old.sqlite'
    with sqlite3.connect(database):
        pass
    before = database.read_bytes()
    jobs_cli.run(['--database', str(database), '--json'])
    result = json.loads(capsys.readouterr().out)
    assert result['jobs'] == [] and result['counts']['queued'] == 0
    assert database.read_bytes() == before
    with sqlite3.connect(database) as db:
        db.execute(
            'CREATE TABLE generation_jobs (id TEXT, status TEXT, created_at TEXT, settings_json TEXT)'
        )
        db.execute(
            "INSERT INTO generation_jobs VALUES (?, 'queued', '2026', '{}')",
            ('b' * 32,),
        )
    before = database.read_bytes()
    jobs_cli.run(['--database', str(database), '--kind', 'variation', '--json'])
    assert json.loads(capsys.readouterr().out)['jobs'][0]['kind'] == 'variation'
    assert database.read_bytes() == before
