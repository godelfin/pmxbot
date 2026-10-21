"""Security boundaries and persistence for local user administration."""

# Imported pytest fixtures intentionally share test argument names.
# ruff: noqa: F811

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from bs4 import BeautifulSoup

import pmxbot
from pmxbot.users import UserError, UserStore, _hash_password
from pmxbot.users_cli import run
from tests.unit.test_web_auth import web  # noqa: F401


@pytest.mark.parametrize('existing_permission', [None, False, True])
def test_additive_migration(tmp_path, existing_permission):
    database = tmp_path / 'legacy.sqlite'
    password_hash = _hash_password('old password')
    with sqlite3.connect(database) as db:
        db.execute(
            '''CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, normalized_username TEXT,
            display_name TEXT, created_at TEXT, enabled INTEGER, can_pair_irc INTEGER)'''
        )
        db.execute("INSERT INTO users VALUES (42, 'Alice', 'alice', 'A', '2020', 1, 1)")
        db.execute(
            'CREATE TABLE user_passwords (user_id INTEGER PRIMARY KEY, password_hash TEXT)'
        )
        db.execute('INSERT INTO user_passwords VALUES (42, ?)', (password_hash,))
        if existing_permission is not None:
            db.execute(
                'ALTER TABLE users ADD COLUMN can_generate_images INTEGER NOT NULL DEFAULT 1'
            )
            db.execute(
                'UPDATE users SET can_generate_images = ?', (existing_permission,)
            )
    for _ in range(2):
        with closing(UserStore(str(database))) as store:
            user = store.authenticate('Alice', 'old password')
            assert user.id == 42 and user.can_pair_irc and user.enabled
            assert not user.can_manage_prompt_rules
            assert user.role == 'user' and not user.must_change_password
            assert user.can_generate_images is (existing_permission is not False)
            assert store.db.execute(
                'SELECT password_hash FROM user_passwords'
            ).fetchone() == (password_hash,)
            assert not store.create('new-user-' + str(_)).can_generate_images


def test_new_users_require_explicit_image_approval(tmp_path):
    database = str(tmp_path / 'bot.sqlite')
    with closing(UserStore(database)) as store:
        user = store.create('new-user', password='password')
        assert not user.can_generate_images
        assert not user.has_permission('generate_images')
        admin = store.bootstrap_admin(user.username)
        assert admin.is_admin and not admin.has_permission('generate_images')
        store.admin_update(
            admin, user.id, role='admin', enabled=True, can_generate_images=True
        )
    with closing(UserStore(database)) as store:
        assert store.get_by_id(user.id).has_permission('generate_images')
        assert not store.create('another-user').can_generate_images


def test_bootstrap_cli_and_persistent_audit(tmp_path, capsys):
    database = str(tmp_path / 'bot.sqlite')
    with closing(UserStore(database)) as store:
        alice = store.create('Alice', password='correct password')
        store.update(alice.id, can_generate_images=False)
        store.create('disabled', password='password', enabled=False)
        store.create('no-password')
        for name in ('disabled', 'no-password'):
            with pytest.raises(UserError):
                store.bootstrap_admin(name)
    run(['--database', database, 'Alice'])
    assert 'Administrator assigned: Alice' in capsys.readouterr().out
    with closing(UserStore(database)) as store:
        admin = store.get_by_id(alice.id)
        assert admin.is_admin and not admin.can_generate_images
        with pytest.raises(UserError, match='already exists'):
            store.bootstrap_admin('Alice')
        row = store.db.execute(
            'SELECT actor_id, target_id, action, timestamp, changes FROM user_admin_audit'
        ).fetchone()
        assert row[:3] == (None, alice.id, 'bootstrap_admin')
        assert row[3] and json.loads(row[4]) == {'role': 'admin'}


def promote(web):
    with closing(UserStore(pmxbot.config.database)) as store:
        return store.bootstrap_admin('Alice')


def post(web, user_id, **overrides):
    data = {
        'user_id': str(user_id),
        'operation': 'update',
        'role': 'user',
        'enabled': '1',
        'can_generate_images': '1',
        'csrf_token': web.token('/admin/users'),
    }
    data.update(overrides)
    return web.request('/admin/users', 'POST', data)


def test_admin_access_navigation_and_csrf(web):
    assert web.request('/admin/users')['status'] == 401
    assert web.request('/admin/users', 'POST')['status'] == 401
    web.login()
    assert '/admin/users' not in web.request('/')['body']
    assert web.request('/admin/users')['status'] == 403
    assert web.request('/admin/users', 'POST')['status'] == 403
    promote(web)
    assert web.request('/protected')['status'] == 401
    web.login()
    response = web.request('/admin/users')
    assert response['status'] == 200
    assert web.base + '/admin/users' in response['body']
    assert 'password_hash' not in response['body']
    assert post(web, web.user_id, csrf_token='')['status'] == 403
    assert web.request('/admin/users', 'PUT')['status'] == 405
    assert web.request('/admin/users?operation=update', 'POST')['status'] == 400


def test_independent_roles_permissions_and_last_admin(web):
    promote(web)
    web.login()
    with closing(UserStore(pmxbot.config.database)) as store:
        target = store.get_by_username('no-password')
        store.set_password(target.id, 'correct password')
    assert post(web, target.id, role='admin', can_generate_images='0')['status'] == 200
    with closing(UserStore(pmxbot.config.database)) as store:
        target = store.get_by_id(target.id)
        assert target.is_admin and not target.has_permission('generate_images')
    assert post(web, target.id, role='admin', can_generate_images='1')['status'] == 200
    assert post(web, target.id, role='user', can_generate_images='1')['status'] == 200
    with closing(UserStore(pmxbot.config.database)) as store:
        target = store.get_by_id(target.id)
        assert target.role == 'user' and target.has_permission('generate_images')
    for settings in ({'role': 'user'}, {'role': 'admin', 'enabled': '0'}):
        assert post(web, web.user_id, **settings)['status'] == 400
    assert web.request('/admin/users')['status'] == 200


@pytest.mark.parametrize('change', ['password', 'role', 'permission', 'enabled'])
def test_security_changes_revoke_all_old_sessions(web, change):
    promote(web)
    web.login()
    old_cookie = web.cookie
    with closing(UserStore(pmxbot.config.database)) as store:
        other = store.create('other', password='correct password')
        actor = store.get_by_id(web.user_id)
        store.admin_update(
            actor, other.id, role='admin', enabled=True, can_generate_images=False
        )
        if change == 'password':
            store.set_password(actor.id, 'replacement password')
        else:
            kwargs = {'role': 'admin', 'enabled': True, 'can_generate_images': True}
            kwargs[
                {
                    'role': 'role',
                    'permission': 'can_generate_images',
                    'enabled': 'enabled',
                }[change]
            ] = (
                'user' if change == 'role' else change == 'permission'
            )
            store.admin_update(store.get_by_id(other.id), actor.id, **kwargs)
    assert web.request('/protected', cookie=old_cookie)['status'] == 401
    with closing(UserStore(pmxbot.config.database)) as store:
        store.update(web.user_id, enabled=True)
    assert web.request('/protected', cookie=old_cookie)['status'] == 401


def test_reset_forces_password_change_and_audits_without_secrets(web):
    actor = promote(web)
    web.login()
    with closing(UserStore(pmxbot.config.database)) as store:
        target = store.create('target', password='correct password')
    result = post(web, target.id, operation='reset_password')
    assert result['status'] == 200
    temporary = BeautifulSoup(result['body'], 'html.parser').select_one('code').text
    # Secret is never shown again on GET.
    assert temporary not in web.request('/admin/users')['body']
    web.cookie = ''
    response = web.login(username='target', password=temporary)
    assert response['headers']['Location'] == web.base + '/change_password'
    for path in ('/', '/protected', '/admin/users'):
        assert web.request(path)['headers']['Location'] == web.base + '/change_password'
    old_cookie = web.cookie
    token = web.token('/change_password')
    data = {
        'current_password': temporary,
        'password': 'NewValidPassword123',
        'password_confirmation': 'NewValidPassword123',
        'csrf_token': token,
    }
    assert (
        web.request('/change_password', 'POST', {**data, 'csrf_token': ''})['status']
        == 403
    )
    assert (
        web.request(
            '/change_password', 'POST', {**data, 'password_confirmation': 'wrong'}
        )['status']
        == 400
    )
    assert web.request('/change_password', 'POST', data)['status'] == 303
    assert web.request('/protected', cookie=old_cookie)['status'] == 401
    assert web.login(username='target', password=temporary)['status'] == 401
    assert web.login(username='target', password='NewValidPassword123')['status'] == 303
    assert web.request('/protected')['status'] == 200
    with closing(UserStore(pmxbot.config.database)) as store:
        assert not store.get_by_id(target.id).must_change_password
        logs = store.db.execute(
            'SELECT actor_id, target_id, action, changes FROM user_admin_audit'
        ).fetchall()
        assert logs[-1][:3] == (actor.id, target.id, 'reset_password')
        assert temporary not in repr(logs) and 'password_hash' not in repr(logs)


def test_concurrent_demotion_keeps_one_admin(tmp_path):
    database = str(tmp_path / 'bot.sqlite')
    with closing(UserStore(database)) as store:
        a = store.create('a', password='password')
        b = store.create('b', password='password')
        a = store.bootstrap_admin('a')
        store.admin_update(
            a, b.id, role='admin', enabled=True, can_generate_images=True
        )
        b = store.get_by_id(b.id)

    def demote(actor):
        with closing(UserStore(database)) as store:
            try:
                store.admin_update(
                    actor, actor.id, role='user', enabled=True, can_generate_images=True
                )
                return True
            except UserError:
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(demote, (a, b))) == [False, True]
    with closing(UserStore(database)) as store:
        assert sum(user.is_admin for user in store.list_users()) == 1


def test_activation_and_disabled_login(web):
    promote(web)
    web.login()
    with closing(UserStore(pmxbot.config.database)) as store:
        target = store.get_by_username('disabled')
    assert post(web, target.id, enabled='1', can_generate_images='0')['status'] == 200
    assert post(web, target.id, enabled='0', can_generate_images='0')['status'] == 200
    web.cookie = ''
    assert web.login(username='disabled')['status'] == 401
    with closing(UserStore(pmxbot.config.database)) as store:
        user = store.get_by_id(target.id)
        assert not user.enabled and not user.can_generate_images and user.role == 'user'
        logs = store.db.execute(
            "SELECT changes FROM user_admin_audit WHERE action = 'update_user' ORDER BY id"
        ).fetchall()
        assert json.loads(logs[0][0])['enabled'] == {'before': False, 'after': True}
        assert json.loads(logs[1][0])['enabled'] == {'before': True, 'after': False}


def test_audit_failure_rolls_back_and_stale_actor_is_rejected(tmp_path):
    with closing(UserStore(str(tmp_path / 'bot.sqlite'))) as store:
        a = store.create('a', password='password')
        b = store.create('b', password='password')
        a = store.bootstrap_admin('a')
        store.db.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON user_admin_audit BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
            store.admin_update(
                a,
                b.id,
                role='admin',
                enabled=False,
                can_generate_images=False,
                can_manage_prompt_rules=True,
            )
        assert store.get_by_id(b.id) == b
        with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
            store.admin_reset_password(a, b.id)
        assert store.authenticate('b', 'password') == b
        store.db.execute('DROP TRIGGER fail_audit')
        store.set_password(a.id, 'replacement')
        with pytest.raises(UserError, match='Administrator access'):
            store.admin_update(
                a, b.id, role='admin', enabled=True, can_generate_images=True
            )
        with pytest.raises(UserError, match='Administrator access'):
            store.admin_reset_password(b, a.id)
