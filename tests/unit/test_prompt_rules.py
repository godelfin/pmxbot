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
from pmxbot.prompt_rules import (
    PromptRuleStore,
    clarifications,
    evaluate,
    legacy_result,
    legacy_seeds,
)
from pmxbot.users import UserError, UserStore
from tests.unit.test_web_auth import web  # noqa: F401
from tests.unit.test_album_page import page, variation  # noqa: F401
from tests.unit.test_album_creation import creation, submit  # noqa: F401
from tests.unit.test_standalone_jobs import music_job
from tests.unit.test_images import config, post, r2  # noqa: F401


FIELDS = dict(
    scope='band',
    match_text='band',
    match_type='whole',
    instruction='Rule text.',
    enabled=True,
    priority=100,
)


DETAILS = {k: FIELDS[k] for k in ('instruction', 'priority', 'enabled')}


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
        dict(match_text='ＳＴＲＡＳＳＥ %_.*', instruction='First', priority=-1),
        dict(match_text='strasse', instruction='Second', priority=-1),
        dict(scope='album', match_text='été', instruction='Second', priority=10),
        dict(match_text='missing', instruction='Wrong'),
        dict(match_text='strasse', instruction='Disabled', enabled=False),
    ]
    ids = [rules.mutate(actor, 'create', **dict(FIELDS, **case)) for case in cases]
    matched, text = evaluate(rules.list_rules(), 'Straße %_.*', 'E\u0301té')
    assert [r['id'] for r in matched] == ids[:3]
    assert text == ' First Second Second'
    assert not evaluate(rules.list_rules(), 'strassexxx', 'x')[0]
    assert not evaluate(rules.list_rules(), 'Other', 'missing')[0]


@pytest.mark.parametrize(
    'values',
    [
        {'match_text': ''},
        {'match_text': ' \t'},
        {'instruction': ' \n'},
        {'match_text': 'x' * 256},
        {'scope': 'joined'},
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
            DETAILS,
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
        ('update', dict(DETAILS, instruction='Changed')),
        ('replace', dict(scope='either', match_text='band', match_type='whole')),
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
    assert actions == [
        'create',
        'update',
        'replace',
        'disable',
        'enable',
        'reorder',
        'delete',
    ]
    rules.update(actor.id, can_manage_prompt_rules=False)
    with pytest.raises(UserError, match='permission'):
        rules.mutate(actor, 'create', **FIELDS)


def test_independent_connection_and_process_updates(store):
    rules, actor = store
    identifier = rules.mutate(actor, 'create', **FIELDS)
    assert clarifications('BAND', 'x', rules.filename)[1] == ' Rule text.'
    rules.mutate(actor, 'update', identifier, **dict(DETAILS, instruction='Fresh'))
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
        'replace',
        first['id'],
        scope='album',
        match_text='torta',
        match_type='contains',
    )
    assert rules.get_rule(first['id'])['compatibility_json'] == '{}'
    assert (
        evaluate(rules.list_rules(), 'Band', 'Retorta')[1]
        == ' Torta is slang for \"thicc Latina\".'
    )


def grant(web):
    with closing(UserStore(pmxbot.config.database)) as store:
        store.update(web.user_id, can_manage_prompt_rules=True)
    web.login()


def form(web, operation='create', **values):
    data = dict(csrf_token=web.token('/admin/prompt_rules'), operation=operation)
    if operation in ('create', 'update'):
        data.update(dict(FIELDS if operation == 'create' else DETAILS, enabled='1'))
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
    assert form(web, match_text=evil, instruction=evil)['status'] == 200
    assert evil not in web.request('/admin/prompt_rules')['body']
    assert '&lt;script&gt;' in web.request('/admin/prompt_rules')['body']
    assert form(web, match_text=evil, instruction='')['status'] == 400
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
    add_rule(cache.database, scope='album', match_text='Album')
    inputs = GenerationInputs.from_source(*cache.get_album_page(42, 42))
    # A literal from the actual canonical name applies to every builder.
    with closing(PromptRuleStore(str(cache.database))) as rules:
        actor = rules.get_by_username('manager')
        identifier = rules.mutate(
            actor,
            'create',
            **dict(FIELDS, match_text=inputs.artist_name, instruction='Frozen hint.'),
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
        cache.database, match_text='New Band', instruction='Original hint.'
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
    add_rule(cache.database, match_text=inputs.artist_name)
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
    add_rule(cache.database, match_text=inputs.artist_name, instruction='Shared hint.')
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


@pytest.mark.parametrize(
    'needle,name,whole,contains',
    [
        ('ass', 'bass', False, True),
        ('ass', 'classic', False, True),
        ('ass', 'ass!', True, True),
        ('ass', 'big-ass-band', True, True),
        ('ass', 'ass_band', False, True),
        ('blue moon', 'blue moonlight', False, True),
        ('blue moon', 'the BLUE\t\nmoon!', True, True),
        ('blue moon', 'blue-moon', False, False),
        ('blue-moon', '(blue-moon)', True, True),
        ('blue-moon', 'blue-moonlight', False, True),
        ('ＳＴＲＡＳＳＥ', 'Straße', True, True),
        ('ÉTÉ', 'E\u0301té', True, True),
        ('a+b', 'aab', False, False),
        ('a+b', '(a+b)', True, True),
        ('[x].*', '[x].*', True, True),
        ('[x].*', 'xxx', False, False),
        (r'\bass\b', 'ass', False, False),
        (r'\bass\b', r'\bass\b', True, True),
    ],
)
def test_literal_word_phrase_and_substring(store, needle, name, whole, contains):
    rules, actor = store
    identifier = rules.mutate(actor, 'create', **dict(FIELDS, match_text=needle))
    for mode, expected in [('whole', whole), ('contains', contains)]:
        rules.mutate(
            actor,
            'replace',
            identifier,
            scope='band',
            match_text=needle,
            match_type=mode,
        )
        result = evaluate([rules.get_rule(identifier)], name, 'unrelated')
        assert bool(result[0]) is expected


@pytest.mark.parametrize('scope', ['band', 'album', 'either'])
def test_structured_scope_and_once_only(store, scope):
    rules, actor = store
    identifier = rules.mutate(
        actor, 'create', **dict(FIELDS, scope=scope, match_text='blue moon')
    )
    only = [rules.get_rule(identifier)]
    assert bool(evaluate(only, 'blue moon', 'other')[0]) == (
        scope in ('band', 'either')
    )
    assert bool(evaluate(only, 'other', 'blue moon')[0]) == (
        scope in ('album', 'either')
    )
    assert evaluate(only, 'blue', 'moon')[0] == []
    assert evaluate(only, 'blue moon', 'blue moon')[1] == ' Rule text.'


@pytest.mark.parametrize('seed', range(7))
def test_legacy_metadata_edits_preserve_matching(store, seed):
    rules, actor = store
    rule = rules.list_rules()[seed]
    before = rule.copy()
    rules.mutate(
        actor,
        'update',
        rule['id'],
        **dict(DETAILS, instruction=rule['instruction'], priority=rule['priority']),
    )
    after = rules.get_rule(rule['id'])
    for artist, album in [
        ('Tortas Medve Sex', 'torta breasts assault nazis'),
        ('Dick', 'sucking'),
        ('Bass', 'Retorta'),
    ]:
        old_result = evaluate([before], artist, album)
        new_result = evaluate([after], artist, album)
        assert [r['id'] for r in old_result[0]] == [r['id'] for r in new_result[0]]
        assert old_result[1] == new_result[1]
    assert after['compatibility_json'] == before['compatibility_json']
    assert after['matching_json'] is None


def old_ordinary_database(database):
    """Build a pre-v2 database, with edited/deleted seeds and ordinary AND."""
    # Build the historical schema directly. Besides modeling an actual upgrade,
    # this works with SQLite 3.27.2 in CI (DROP COLUMN needs SQLite 3.35+).
    with closing(UserStore(str(database))) as store:
        actor = store.create('manager')
        actor = store.update(actor.id, can_manage_prompt_rules=True)
        store.db.executescript(
            """CREATE TABLE prompt_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seed_key TEXT UNIQUE,
                artist_pattern TEXT NOT NULL DEFAULT '',
                album_pattern TEXT NOT NULL DEFAULT '',
                instruction TEXT NOT NULL CHECK(length(trim(instruction)) > 0),
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                priority INTEGER NOT NULL,
                compatibility_json TEXT NOT NULL DEFAULT '{}',
                created_by INTEGER REFERENCES users(id),
                updated_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(length(trim(artist_pattern)) + length(trim(album_pattern)) > 0)
            );
            CREATE TABLE prompt_rule_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER REFERENCES users(id),
                rule_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                before_json TEXT, after_json TEXT
            );
            CREATE TABLE prompt_rule_migrations (name TEXT PRIMARY KEY);
            INSERT INTO prompt_rule_migrations VALUES ('legacy-v1');
            """
        )
        store.db.row_factory = sqlite3.Row

        def get_rule(identifier):
            return dict(
                store.db.execute(
                    'SELECT * FROM prompt_rules WHERE id = ?', (identifier,)
                ).fetchone()
            )

        def audit(actor_id, identifier, action, before, after):
            store.db.execute(
                'INSERT INTO prompt_rule_audit (actor_id, rule_id, action, before_json, after_json) VALUES (?, ?, ?, ?, ?)',
                (actor_id, identifier, action, json.dumps(before), json.dumps(after)),
            )

        for priority, (key, artist, album, instruction, metadata) in enumerate(
            legacy_seeds()
        ):
            cursor = store.db.execute(
                'INSERT INTO prompt_rules (seed_key, artist_pattern, album_pattern, instruction, enabled, priority, compatibility_json) VALUES (?, ?, ?, ?, 1, ?, ?)',
                (
                    key,
                    artist,
                    album,
                    instruction,
                    priority,
                    json.dumps(dict(metadata, mode='regex')),
                ),
            )
            audit(None, cursor.lastrowid, 'migrate', None, get_rule(cursor.lastrowid))
        before = get_rule(1)
        store.db.execute(
            "UPDATE prompt_rules SET instruction = 'Edited seed.', priority = 100, updated_by = ? WHERE id = 1",
            (actor.id,),
        )
        audit(actor.id, 1, 'update', before, get_rule(1))
        before = get_rule(2)
        store.db.execute('DELETE FROM prompt_rules WHERE id = 2')
        audit(actor.id, 2, 'delete', before, None)
        for artist, album in [
            ('ＳＴＲＡＳＳＥ', 'moon'),
            ('', 'a+b'),
            ('band', ''),
            (' \t', 'moon'),
        ]:
            cursor = store.db.execute(
                'INSERT INTO prompt_rules (artist_pattern, album_pattern, instruction, priority, enabled, created_by, updated_by) VALUES (?, ?, ?, 100, 1, ?, ?)',
                (artist, album, 'Old ordinary.', actor.id, actor.id),
            )
            audit(
                actor.id,
                cursor.lastrowid,
                'create',
                None,
                get_rule(cursor.lastrowid),
            )
        # A representative historical job snapshot must remain byte-for-byte.
        store.db.execute('CREATE TABLE generation_jobs (id TEXT, prompt TEXT)')
        store.db.execute("INSERT INTO generation_jobs VALUES ('old', 'Frozen prompt.')")
        rules = [
            dict(row)
            for row in store.db.execute(
                'SELECT * FROM prompt_rules ORDER BY priority, id'
            )
        ]
        return rules, [
            tuple(row) for row in store.db.execute('SELECT * FROM prompt_rule_audit')
        ]


def test_structured_migration_restart_concurrency_and_and(tmp_path):
    database = str(tmp_path / 'old.sqlite')
    before, audit = old_ordinary_database(database)
    inputs = [
        ('Straße extension', 'moonlight'),
        ('Straße', 'other'),
        ('other', 'moon'),
        ('x', 'a+b'),
        ('bandstand', 'x'),
    ]
    expected = [evaluate(before, *names)[1] for names in inputs]

    def reopen(_):
        with closing(PromptRuleStore(database)) as store:
            return store.list_rules()

    with ThreadPoolExecutor(max_workers=5) as pool:
        snapshots = list(pool.map(reopen, range(10)))
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    after = snapshots[0]
    assert [evaluate(after, *names)[1] for names in inputs] == expected
    assert [
        dict((k, v) for k, v in rule.items() if k != 'matching_json') for rule in after
    ] == before
    combined = next(
        rule for rule in after if rule['artist_pattern'] == 'ＳＴＲＡＳＳＥ'
    )
    from pmxbot.prompt_rules import describe

    assert ' AND ' in describe(combined)['summary']
    assert len(json.loads(combined['matching_json'])) == 2
    assert not evaluate([combined], 'Straße', 'other')[0]
    with closing(PromptRuleStore(database)) as store:
        assert [
            tuple(row) for row in store.db.execute('SELECT * FROM prompt_rule_audit')
        ] == audit
        assert tuple(store.db.execute('SELECT * FROM generation_jobs').fetchone()) == (
            'old',
            'Frozen prompt.',
        )
        actor = store.get_by_username('manager')
        store.mutate(
            actor, 'update', combined['id'], **dict(DETAILS, instruction='Edited AND.')
        )
        assert not evaluate([store.get_rule(combined['id'])], 'Straße', 'other')[0]
        store.mutate(
            actor,
            'replace',
            combined['id'],
            scope='either',
            match_text='Straße',
            match_type='whole',
        )
        assert (
            evaluate([store.get_rule(combined['id'])], 'other', 'Straße')[1]
            == ' Edited AND.'
        )


def test_structured_migration_failure_rolls_back_schema_and_data(tmp_path):
    database = str(tmp_path / 'rollback.sqlite')
    before, audit = old_ordinary_database(database)
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TRIGGER stop_v2 BEFORE UPDATE ON prompt_rules BEGIN SELECT RAISE(ABORT, 'migration failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match='migration failed'):
        PromptRuleStore(database)
    with sqlite3.connect(database) as db:
        assert 'matching_json' not in [
            row[1] for row in db.execute('PRAGMA table_info(prompt_rules)')
        ]
        assert not db.execute(
            "SELECT 1 FROM prompt_rule_migrations WHERE name = 'structured-v2'"
        ).fetchone()
        db.row_factory = sqlite3.Row
        assert [
            dict(row)
            for row in db.execute('SELECT * FROM prompt_rules ORDER BY priority, id')
        ] == before
        assert [
            tuple(row) for row in db.execute('SELECT * FROM prompt_rule_audit')
        ] == audit
        db.execute('DROP TRIGGER stop_v2')
    with closing(PromptRuleStore(database)) as store:
        assert len(store.list_rules()) == len(before)


@pytest.mark.parametrize('operation', ['create', 'update', 'replace', 'preview'])
@pytest.mark.parametrize(
    'hidden',
    ['artist_pattern', 'album_pattern', 'compatibility_json', 'matching_json', 'regex'],
)
def test_http_hidden_conditions_rejected_without_reflection(web, operation, hidden):
    grant(web)
    values = {'rule_id': '1'} if operation in ('update', 'replace') else {}
    if operation == 'replace':
        values.update(scope='band', match_text='x', match_type='whole')
    if operation == 'preview':
        values.update(artist='Band', album='Album')
    secret = r'INTERNAL(?:secret|pattern)\b'
    response = form(web, operation, **values, **{hidden: secret})
    assert response['status'] == 400
    assert secret not in response['body']
    assert hidden not in response['body']
    with closing(PromptRuleStore(pmxbot.config.database)) as store:
        assert len(store.list_rules()) == 7
        assert (
            store.db.execute('SELECT count(*) FROM prompt_rule_audit').fetchone()[0]
            == 7
        )


def test_regex_free_rendering_preview_and_explicit_replacement(web):
    grant(web)
    with closing(PromptRuleStore(pmxbot.config.database)) as store:
        legacy = store.list_rules()
    response = web.request('/admin/prompt_rules')
    for rule in legacy:
        for field in ('artist_pattern', 'album_pattern'):
            if rule[field]:
                assert rule[field] not in response['body']
    assert 'plural' in response['body']
    assert 'Either name' in response['body']
    assert 'Combined band and album' in response['body']
    assert 'Shared context' in response['body']
    assert 'name="artist_pattern"' not in response['body']
    assert 'name="album_pattern"' not in response['body']
    assert form(web, 'update', rule_id='1', instruction='Changed.')['status'] == 200
    result = form(web, 'preview', artist='Band', album='Tortas')['body']
    assert 'Changed.' in result and 'whole word “torta” or “tortas”' in result
    assert r'\btortas?\b' not in result
    assert (
        form(
            web,
            'update',
            rule_id='1',
            scope='album',
            match_text='x',
            match_type='contains',
        )['status']
        == 400
    )
    assert (
        form(
            web,
            'replace',
            rule_id='1',
            scope='album',
            match_text='torta',
            match_type='whole',
        )['status']
        == 200
    )
    result = form(web, 'preview', artist='Band', album='Tortas')['body']
    assert '<li>No matches</li>' in result
    assert (
        form(web, 'preview', artist='Band', album='Torta')['body'].count('Changed.')
        >= 1
    )


@pytest.mark.parametrize(
    'values',
    [
        {'scope': ['band', 'either']},
        {'match_type': ['whole', 'contains']},
        {'match_text': ['x', 'y']},
        {'instruction': ['x', 'y']},
        {'enabled': ['0', '1']},
        {'priority': ['1', '2']},
        {'scope': 'joined'},
        {'match_type': 'regex'},
        {'match_type': 'unknown'},
    ],
)
def test_malformed_structured_fields_rejected(store, values):
    rules, actor = store
    before = rules.list_rules()
    with pytest.raises(UserError):
        rules.mutate(actor, 'create', **dict(FIELDS, **values))
    assert rules.list_rules() == before


def test_repeated_http_fields_cannot_bypass_validation(web):
    grant(web)
    for field in (
        'scope',
        'match_text',
        'match_type',
        'priority',
        'enabled',
        'operation',
    ):
        data = list(
            dict(
                FIELDS,
                enabled='1',
                operation='create',
                csrf_token=web.token('/admin/prompt_rules'),
            ).items()
        )
        data.append((field, 'duplicate'))
        assert web.request('/admin/prompt_rules', 'POST', data)['status'] == 400


@pytest.mark.parametrize('operation', ['create', 'update', 'replace'])
@pytest.mark.parametrize(
    'hidden',
    ['artist_pattern', 'album_pattern', 'compatibility_json', 'matching_json', 'regex'],
)
def test_public_api_cannot_accept_hidden_matching(store, operation, hidden):
    rules, actor = store
    fields = {
        'create': FIELDS,
        'update': DETAILS,
        'replace': dict(scope='band', match_text='band', match_type='whole'),
    }[operation]
    before = rules.list_rules()
    with pytest.raises(UserError, match='only the fields'):
        rules.mutate(
            actor,
            operation,
            '' if operation == 'create' else 1,
            **dict(fields, **{hidden: r'\b(?:secret)\b'}),
        )
    assert rules.list_rules() == before
    assert rules.db.execute('SELECT count(*) FROM prompt_rule_audit').fetchone()[0] == 7


def test_rendered_migrated_and_rule_preserves_conditions_on_detail_save(web):
    grant(web)
    with closing(PromptRuleStore(pmxbot.config.database)) as store:
        store.db.execute(
            "DELETE FROM prompt_rule_migrations WHERE name = 'structured-v2'"
        )
        identifier = store.db.execute(
            "INSERT INTO prompt_rules (artist_pattern, album_pattern, instruction, priority, enabled) VALUES ('Band', 'moon', 'Both names.', 100, 1)"
        ).lastrowid
    body = web.request('/admin/prompt_rules')['body']
    assert 'Band name contains text “Band” AND Album title contains text “moon”' in body
    assert (
        form(web, 'update', rule_id=str(identifier), instruction='Edited both names.')[
            'status'
        ]
        == 200
    )
    body = form(web, 'preview', artist='Band', album='Other')['body']
    assert '<li>No matches</li>' in body
    body = form(web, 'preview', artist='Bandstand', album='moonlight')['body']
    assert 'Edited both names.' in body
    assert 'Band name contains text “Band” AND Album title contains text “moon”' in body
