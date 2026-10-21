"""Rule persistence, migration, shared evaluation and real session boundaries."""

# ruff: noqa: F811
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import Mock

import pytest

import pmxbot
from pmxbot import images
from pmxbot.jobs import JobStore, job_prompt, process_job
from pmxbot.music import GenerationInputs, album_prompt, artwork_prompt
from pmxbot.prompt_rules import PromptRuleStore, clarifications, evaluate, legacy_result
from pmxbot.users import UserError, UserStore
from tests.unit.test_web_auth import web  # noqa: F401
from tests.unit.test_album_page import page, variation  # noqa: F401
from tests.unit.test_album_creation import creation, submit  # noqa: F401
from tests.unit.test_standalone_jobs import music_job
from tests.unit.test_images import config, post, r2  # noqa: F401


FIELDS = dict(
    artist_pattern='band',
    album_pattern='',
    instruction='Rule text.',
    enabled=True,
    priority=100,
)


@pytest.fixture
def store(tmp_path):
    with closing(PromptRuleStore(str(tmp_path / 'rules.sqlite'))) as rules:
        actor = rules.create('manager')
        actor = rules.update(actor.id, can_manage_prompt_rules=True)
        yield rules, actor


def add_rule(database, **values):
    with closing(PromptRuleStore(str(database))) as store:
        actor = store.create('manager')
        actor = store.update(actor.id, can_manage_prompt_rules=True)
        return store.mutate(actor, 'create', **dict(FIELDS, **values))


def test_matching_contract_and_ties(store):
    rules, actor = store
    cases = [
        dict(artist_pattern='ＳＴＲＡＳＳＥ %_.*', instruction='First', priority=-1),
        dict(
            artist_pattern='strasse',
            album_pattern='ÉTÉ',
            instruction='Second',
            priority=-1,
        ),
        dict(artist_pattern='', album_pattern='été', instruction='Second', priority=10),
        dict(artist_pattern='strasse', album_pattern='missing', instruction='Wrong'),
        dict(artist_pattern='strasse', instruction='Disabled', enabled=False),
    ]
    ids = [rules.mutate(actor, 'create', **dict(FIELDS, **case)) for case in cases]
    matched, text = evaluate(rules.list_rules(), 'Straße %_.*', 'E\u0301té')
    assert [r['id'] for r in matched] == ids[:3]
    assert text == ' First Second Second'
    assert not evaluate(rules.list_rules(), 'strasse xxx', 'x')[0]
    assert not evaluate(rules.list_rules(), 'Other', 'missing')[0]


@pytest.mark.parametrize(
    'values',
    [
        {'artist_pattern': '', 'album_pattern': ''},
        {'artist_pattern': ' \t'},
        {'instruction': ' \n'},
        {'artist_pattern': 'x' * 256},
        {'album_pattern': 'x' * 256},
        {'instruction': 'x' * 10001},
        {'instruction': '\x00'},
        {'enabled': '1'},
        {'priority': True},
        {'priority': '1.5'},
        {'priority': 2147483648},
        {'priority': -2147483649},
    ],
)
def test_validation_has_no_writes(store, values):
    rules, actor = store
    before = rules.db.execute('SELECT count(*) FROM prompt_rule_audit').fetchone()[0]
    with pytest.raises(UserError):
        rules.mutate(actor, 'create', **dict(FIELDS, **values))
    assert len(rules.list_rules()) == 7
    assert (
        rules.db.execute('SELECT count(*) FROM prompt_rule_audit').fetchone()[0]
        == before
    )


def test_seed_identity_survives_edits_deletes_and_concurrent_restarts(store):
    rules, actor = store
    seeds = rules.list_rules()
    first = seeds[0]
    rules.mutate(
        actor,
        'update',
        first['id'],
        **dict(
            FIELDS,
            artist_pattern=first['artist_pattern'],
            album_pattern=first['album_pattern'],
            instruction='Edited seed.',
            priority=-12,
            enabled=False,
        ),
    )
    rules.mutate(actor, 'delete', seeds[1]['id'])
    database = rules.filename

    def reopen(_):
        with closing(PromptRuleStore(database)) as independent:
            return independent.list_rules()

    with ThreadPoolExecutor(max_workers=5) as pool:
        snapshots = list(pool.map(reopen, range(10)))
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert len(snapshots[0]) == 6
    assert snapshots[0][0]['instruction'] == 'Edited seed.'
    assert not snapshots[0][0]['enabled']
    assert snapshots[0][0]['compatibility_json'] == first['compatibility_json']
    history = rules.db.execute(
        "SELECT before_json, after_json FROM prompt_rule_audit WHERE action = 'delete'"
    ).fetchone()
    assert json.loads(history[0])['id'] == seeds[1]['id']
    assert json.loads(history[1]) is None


def test_concurrent_fresh_migration_and_failure_rollback(tmp_path, monkeypatch):
    database = str(tmp_path / 'fresh.sqlite')
    original = PromptRuleStore.audit

    def fail(*args):
        raise sqlite3.IntegrityError('audit unavailable')

    monkeypatch.setattr(PromptRuleStore, 'audit', fail)
    with pytest.raises(sqlite3.IntegrityError):
        PromptRuleStore(database)
    with sqlite3.connect(database) as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'prompt_rule_migrations'"
        ).fetchone()
    monkeypatch.setattr(PromptRuleStore, 'audit', original)

    def initialize(_):
        with closing(PromptRuleStore(database)) as store:
            return len(store.list_rules())

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert list(pool.map(initialize, range(10))) == [7] * 10
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT count(*) FROM prompt_rule_audit').fetchone()[0] == 7


def test_mutation_and_audit_atomicity_and_current_authority(store):
    rules, actor = store
    identifier = rules.mutate(actor, 'create', **FIELDS)
    for operation, values in [
        ('update', dict(FIELDS, instruction='Changed')),
        ('disable', {}),
        ('enable', {}),
        ('reorder', {'priority': -20}),
        ('delete', {}),
    ]:
        before = rules.get_rule(identifier)
        rules.db.execute(
            "CREATE TRIGGER fail_rule_audit BEFORE INSERT ON prompt_rule_audit BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            rules.mutate(actor, operation, identifier, **values)
        assert rules.get_rule(identifier) == before
        rules.db.execute('DROP TRIGGER fail_rule_audit')
        rules.mutate(actor, operation, identifier, **values)
    actions = [
        r[0]
        for r in rules.db.execute(
            'SELECT action FROM prompt_rule_audit WHERE rule_id = ?', (identifier,)
        )
    ]
    assert actions == ['create', 'update', 'disable', 'enable', 'reorder', 'delete']
    rules.update(actor.id, can_manage_prompt_rules=False)
    with pytest.raises(UserError, match='permission'):
        rules.mutate(actor, 'create', **FIELDS)


def test_independent_connection_and_process_updates(store):
    rules, actor = store
    identifier = rules.mutate(actor, 'create', **FIELDS)
    assert clarifications('BAND', 'x', rules.filename)[1] == ' Rule text.'
    rules.mutate(actor, 'update', identifier, **dict(FIELDS, instruction='Fresh'))
    code = 'import sys; from pmxbot.prompt_rules import clarifications; print(clarifications("BAND", "x", sys.argv[1])[1])'
    result = subprocess.run(
        [sys.executable, '-c', code, rules.filename],
        capture_output=True,
        text=True,
        encoding='utf-8',
        check=True,
    )
    assert result.stdout.strip() == 'Fresh'


def test_compatibility_grouping_and_condition_conversion(store):
    rules, actor = store
    result = evaluate(
        rules.list_rules(), 'Tortas Medve Sex', 'torta medve breasts assault nazis'
    )
    assert (
        result[1]
        == legacy_result('Tortas Medve Sex', 'torta medve breasts assault nazis')[1]
    )
    assert result[1].count('text labels') == 1
    assert result[1].count('metaphors') == 1
    assert result[1].count("Medve isn't") == 1
    assert 'Torta' not in evaluate(rules.list_rules(), 'Tortastic', 'Retorta')[1]
    first = rules.list_rules()[0]
    rules.mutate(
        actor,
        'update',
        first['id'],
        **dict(FIELDS, artist_pattern='', album_pattern='torta'),
    )
    assert rules.get_rule(first['id'])['compatibility_json'] == '{}'
    assert evaluate(rules.list_rules(), 'Band', 'Retorta')[1] == ' Rule text.'


def grant(web):
    with closing(UserStore(pmxbot.config.database)) as store:
        store.update(web.user_id, can_manage_prompt_rules=True)
    web.login()


def form(web, operation='create', **values):
    data = dict(csrf_token=web.token('/admin/prompt_rules'), operation=operation)
    if operation in ('create', 'update'):
        data.update(dict(FIELDS, enabled='1'))
    data.update(values)
    return web.request('/admin/prompt_rules', 'POST', data)


def test_web_permissions_methods_preview_and_escaping(web, monkeypatch):
    assert web.request('/admin/prompt_rules')['status'] == 401
    web.login()
    assert web.request('/admin/prompt_rules')['status'] == 403
    with closing(UserStore(pmxbot.config.database)) as store:
        store.bootstrap_admin('Alice')
    web.login()
    assert web.request('/admin/prompt_rules')['status'] == 403
    with closing(UserStore(pmxbot.config.database)) as store:
        # Ordinary manager, no image generation or pairing grant is required.
        store.create('other', password='password')
        store.update(store.get_by_username('other').id, role='admin')
        store.update(
            web.user_id, role='user', can_pair_irc=False, can_generate_images=False
        )
    grant(web)
    assert web.request('/admin/users')['status'] == 403
    assert web.request('/create_album')['status'] == 403
    assert '/admin/prompt_rules' in web.request('/')['body']
    assert web.request('/admin/prompt_rules', 'PUT')['status'] == 405
    assert form(web, csrf_token='')['status'] == 403
    assert web.request('/admin/prompt_rules?operation=delete', 'POST')['status'] == 400
    assert web.request('/admin/prompt_rules?operation=delete')['status'] == 400
    evil = '<script>alert(1)</script> %_.*\' --'
    assert form(web, artist_pattern=evil, instruction=evil)['status'] == 200
    assert evil not in web.request('/admin/prompt_rules')['body']
    assert '&lt;script&gt;' in web.request('/admin/prompt_rules')['body']
    assert form(web, artist_pattern=evil, instruction='')['status'] == 400
    assert (
        evil.replace('<', '&lt;').split('>')[0]
        in web.request('/admin/prompt_rules')['body']
    )
    for operation, values in [
        ('reorder', {'rule_id': '1', 'priority': 'bad'}),
        ('delete', {'rule_id': '1 OR 1=1'}),
        ('oops', {}),
    ]:
        assert form(web, operation, **values)['status'] == 400
    provider = Mock(side_effect=AssertionError('preview must never generate'))
    monkeypatch.setattr(images.ImageCache, 'get', provider)
    monkeypatch.setattr(JobStore, 'enqueue', provider)
    response = form(web, 'preview', artist=evil, album='x')
    assert response['status'] == 200
    assert '&lt;script&gt;' in response['body']
    assert 'Resulting prompt' in response['body']
    assert form(web, 'preview', artist='x' * 1001, album='x')['status'] == 400
    provider.assert_not_called()
    with closing(PromptRuleStore(pmxbot.config.database)) as store:
        assert store.db.execute('SELECT count(*) FROM albums').fetchone()[0] == 1
        assert not store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='generation_jobs'"
        ).fetchone()
    old_cookie = web.cookie
    with closing(UserStore(pmxbot.config.database)) as store:
        store.update(web.user_id, can_manage_prompt_rules=False)
    assert web.request('/admin/prompt_rules', cookie=old_cookie)['status'] == 401


@pytest.mark.parametrize('state', ['disabled', 'temporary', 'stale'])
def test_denied_session_states(web, state):
    grant(web)
    with closing(UserStore(pmxbot.config.database)) as store:
        if state == 'temporary':
            store.db.execute(
                'UPDATE users SET must_change_password = 1 WHERE id = ?', (web.user_id,)
            )
        elif state == 'disabled':
            store.update(web.user_id, enabled=False)
        else:
            store.set_password(web.user_id, 'replacement')
    response = web.request('/admin/prompt_rules')
    assert response['status'] == (303 if state == 'temporary' else 401)


def test_admin_permission_grant_audit_and_independence(web):
    with closing(UserStore(pmxbot.config.database)) as store:
        store.bootstrap_admin('Alice')
        target = store.create('manager')
    web.login()
    for allowed in ('1', '0'):
        response = web.request(
            '/admin/users',
            'POST',
            dict(
                csrf_token=web.token('/admin/users'),
                user_id=target.id,
                operation='update',
                role='user',
                enabled='1',
                can_generate_images='0',
                can_manage_prompt_rules=allowed,
            ),
        )
        assert response['status'] == 200
        with closing(UserStore(pmxbot.config.database)) as store:
            user = store.get_by_id(target.id)
            assert user.can_manage_prompt_rules == (allowed == '1')
            assert (
                not user.is_admin
                and not user.can_generate_images
                and not user.can_pair_irc
            )
            changes = json.loads(
                store.db.execute(
                    'SELECT changes FROM user_admin_audit ORDER BY id DESC LIMIT 1'
                ).fetchone()[0]
            )
            assert changes['can_manage_prompt_rules']['after'] == (allowed == '1')


def test_shared_web_variation_snapshot(variation):
    cache, request, provider, hosting, values, source = variation
    add_rule(cache.database, artist_pattern='', album_pattern='Album')
    inputs = GenerationInputs.from_source(*cache.get_album_page(42, 42))
    # A literal from the actual canonical name applies to every builder.
    with closing(PromptRuleStore(str(cache.database))) as rules:
        actor = rules.get_by_username('manager')
        identifier = rules.mutate(
            actor,
            'create',
            **dict(
                FIELDS, artist_pattern=inputs.artist_name, instruction='Frozen hint.'
            ),
        )
    assert artwork_prompt(inputs, database=cache.database).count('Frozen hint.') == 1
    response = request(method='POST', data=values)
    store = JobStore(cache.database)
    job = store.get(response['headers']['Location'].rsplit('/', 1)[1])
    assert job['prompt'].count('Frozen hint.') == 1
    with closing(PromptRuleStore(str(cache.database))) as rules:
        rules.mutate(rules.get_by_username('manager'), 'delete', identifier)
    process_job(store, store.claim(), pmxbot.config)
    done = store.get(job['id'])
    assert done['status'] == 'succeeded'
    assert cache.get_image(done['result_image_id'])['prompt'] == job['prompt']
    assert 'Frozen hint.' not in artwork_prompt(inputs, database=cache.database)


def test_creation_retry_keeps_exact_prompt(creation):
    cache, request, provider, hosting, values, user = creation
    identifier = add_rule(
        cache.database, artist_pattern='New Band', instruction='Original hint.'
    )
    store, job_id = submit(creation)
    frozen = store.get(job_id)['prompt']
    assert frozen.count('Original hint.') == 1
    with closing(PromptRuleStore(str(cache.database))) as rules:
        rules.mutate(rules.get_by_username('manager'), 'delete', identifier)
    store.claim()
    store.finish(job_id, error='failure')
    store.retry_creation(job_id, user)
    process_job(store, store.claim(), pmxbot.config)
    done = store.get(job_id)
    assert done['status'] == 'succeeded'
    assert cache.get_image(done['result_image_id'])['prompt'] == frozen


def test_irc_snapshot_and_safe_storage_failure(config, post, r2, monkeypatch):
    identifier = add_rule(images.ImageCache(config).database, instruction='IRC hint.')
    cache, store, job_id = music_job(config)
    frozen = store.get(job_id)['prompt']
    assert frozen.count('IRC hint.') == 1
    with closing(PromptRuleStore(str(cache.database))) as rules:
        rules.mutate(rules.get_by_username('manager'), 'delete', identifier)
    process_job(store, store.claim(('music',)), config)
    assert cache.get_image(store.get(job_id)['result_image_id'])['prompt'] == frozen

    def fail(*args):
        raise sqlite3.OperationalError('secret database diagnostics')

    monkeypatch.setattr(PromptRuleStore, 'list_rules', fail)
    with pytest.raises(images.ImageError, match='Prompt rules are unavailable'):
        album_prompt(
            dict(
                artist_name='Band',
                title='Album',
                format='',
                format_description='',
                genre='',
                description='',
            ),
            database=cache.database,
        )


def test_historical_job_uses_legacy_only(variation):
    cache, *_ = variation
    inputs = GenerationInputs.from_source(*cache.get_album_page(42, 42))
    add_rule(cache.database, artist_pattern=inputs.artist_name)
    old = dict(operation='edit', prompt=None)
    assert 'Rule text.' not in job_prompt(old, inputs)
    assert 'Rule text.' in artwork_prompt(inputs, database=cache.database)


@pytest.mark.parametrize('operation', ['create_new', 'edit'])
def test_direct_artwork_paths_share_evaluator(variation, monkeypatch, operation):
    from pmxbot.music import (
        MusicLibrary,
        generate_album_new,
        continue_album_image,
        generate_album_variation,
    )

    cache, *_ = variation
    inputs = GenerationInputs.from_source(*cache.get_album_page(42, 42))
    add_rule(
        cache.database, artist_pattern=inputs.artist_name, instruction='Shared hint.'
    )
    library = MusicLibrary(cache.database)
    operation_mock = Mock(return_value={'id': 101})
    if operation == 'create_new':
        monkeypatch.setattr(cache, 'create_image', operation_mock)
        generate_album_new(library, cache, inputs)
    else:
        monkeypatch.setattr(cache, 'continue_image', operation_mock)
        monkeypatch.setattr(cache, 'vary_image', operation_mock)
        continue_album_image(library, cache, 42, inputs)
        generate_album_variation(library, cache, 42, inputs)
    for call in operation_mock.call_args_list:
        prompt = call.args[0 if operation == 'create_new' else 1]
        assert prompt.count('Shared hint.') == 1


def test_local_irc_queue_freezes_rules_and_names(config, monkeypatch):
    add_rule(images.ImageCache(config).database, instruction='Local frozen hint.')
    thread = Mock()
    monkeypatch.setattr(images.threading, 'Thread', thread)
    started = images._busy.acquire()
    try:
        assert 'queued' in images._start_image(
            None,
            '#music',
            'alice',
            None,
            album_data={'artist': 'Band', 'title': 'Album'},
        )
        job = images._pending.get_nowait()
        with closing(PromptRuleStore(str(job['args'][0].database))) as store:
            actor = store.get_by_username('manager')
            store.mutate(actor, 'delete', store.list_rules()[-1]['id'])
            store.db.execute("UPDATE albums SET title = 'Changed'")
        provider = Mock(return_value='https://example.test/image')
        monkeypatch.setattr(job['args'][0], 'get', provider)
        job['target'](*job['args'], **job['kwargs'])
        assert provider.call_args.args[0].count('Local frozen hint.') == 1
        assert 'Changed' not in provider.call_args.args[0]
        images._results.get_nowait()
    finally:
        if started:
            images._busy.release()


@pytest.mark.parametrize(
    'artist,title,anatomy,acts,violence,hate',
    [
        ('Band', 'ＴＩＴＳ boobs genitals strippers', True, False, False, False),
        ('Band', 'goon gooner gooners orgies handjobs', False, True, False, False),
        ('Dick', 'sucking', True, True, False, False),
        ('Band', 'molestation raped raping', False, False, True, False),
        ('Band', 'fagged nazis', False, False, False, True),
        ('Bass classic', 'Medved Retorta goonerville', False, False, False, False),
    ],
)
def test_legacy_exact_context_text(store, artist, title, anatomy, acts, violence, hate):
    rules, _ = store
    instructions = [
        (
            'Keep the artwork non-explicit, with no nudity or sexual anatomy.'
            if anatomy
            else ''
        ),
        'Do not depict sexual acts or explicit sexual content.' if acts else '',
        'Do not depict sexual violence or abuse.' if violence else '',
        (
            'Do not depict hateful imagery, extremist symbols, or atrocities.'
            if hate
            else ''
        ),
    ]
    expected = ''
    if any(instructions):
        expected = (
            ' The quoted band and album names are text labels for a music release.'
        )
        if anatomy or acts or violence:
            expected += (
                ' The album and band names are only metaphors or figures of speech.'
            )
        expected += ''.join(' ' + text for text in instructions if text)
    assert evaluate(rules.list_rules(), artist, title)[1] == expected


@pytest.mark.parametrize(
    'metadata', ['[]', '{"mode": "unknown"}', '{"mode": "regex", "scope": "unknown"}']
)
def test_invalid_compatibility_fails_closed(store, metadata):
    rules, actor = store
    identifier = rules.mutate(actor, 'create', **FIELDS)
    rules.db.execute(
        'UPDATE prompt_rules SET compatibility_json = ? WHERE id = ?',
        (metadata, identifier),
    )
    with pytest.raises(images.ImageError, match='Prompt rules are unavailable'):
        clarifications('Band', 'Album', rules.filename)


def test_invalid_ids_fields_and_create_audit_rollback(store):
    rules, actor = store
    before = rules.list_rules()
    for operation, identifier, values in [
        ('create', '1', FIELDS),
        ('create', '', dict(FIELDS, compatibility_json='{}')),
        ('delete', '999', {}),
        ('delete', '1 OR 1=1', {}),
        ('reorder', '1', {'priority': 1, 'instruction': 'x'}),
        ('disable', '1', {'instruction': 'x'}),
    ]:
        with pytest.raises(UserError):
            rules.mutate(actor, operation, identifier, **values)
    rules.db.execute(
        "CREATE TRIGGER fail_create_audit BEFORE INSERT ON prompt_rule_audit BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        rules.mutate(actor, 'create', **FIELDS)
    assert rules.list_rules() == before
    assert rules.db.execute('SELECT count(*) FROM prompt_rule_audit').fetchone()[0] == 7
