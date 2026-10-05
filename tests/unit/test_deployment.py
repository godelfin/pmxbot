"""Exercise the deployment boundary without accessing Git, pip, or systemd."""

import os
import pathlib
import subprocess

import pytest


@pytest.fixture
def deployment(tmp_path):
    commands = tmp_path / 'commands'
    commands.mkdir()
    dispatcher = commands / 'dispatcher'
    dispatcher.write_text(
        '#!/bin/bash\n'
        'command=${0##*/}\n'
        'if [[ $command == id ]]; then echo "${TEST_USER:-deploy}"; exit; fi\n'
        'printf "%s" "$command" >> "$TEST_LOG"\n'
        'printf " <%s>" "$@" >> "$TEST_LOG"\n'
        'printf "\\n" >> "$TEST_LOG"\n'
        'if [[ $command == "${TEST_FAIL:-}" ]]; then exit 1; fi\n'
        'if [[ ${TEST_FAIL_CHECK:-} == yes && $command == python3 '
        '&& ${*: -1} == check ]]; then exit 1; fi\n',
        encoding='utf-8',
    )
    dispatcher.chmod(0o755)
    for command in ('id', 'flock', 'git', 'python3', 'sudo'):
        (commands / command).symlink_to(dispatcher)
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    source = pathlib.Path(__file__).parents[2] / 'deploy' / 'deploy-ircbot'
    # Substitute fixed production paths only in the temporary test copy.
    script = source.read_text(encoding='utf-8')
    script = script.replace('/usr/bin:/bin', f'{commands}:/usr/bin:/bin')
    script = script.replace('/home/deploy/.deploy-ircbot.lock', str(tmp_path / 'lock'))
    script = script.replace('/home/ircbot/pmxbot', str(checkout))
    script = script.replace('/home/ircbot/venv/bin/python3', str(commands / 'python3'))
    test_script = tmp_path / 'deploy-ircbot'
    test_script.write_text(script, encoding='utf-8')
    log = tmp_path / 'log'

    def run(*args, **environment):
        env = dict(os.environ, TEST_LOG=str(log), SSH_ORIGINAL_COMMAND='')
        env.update(environment)
        result = subprocess.run(
            ['/bin/bash', str(test_script), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, log.read_text(encoding='utf-8') if log.exists() else ''

    return run


def test_deployment_installs_viewer_before_restart(deployment):
    result, log = deployment()
    assert result.returncode == 0, result.stderr
    assert log.splitlines() == [
        'flock <-n> <9>',
        'git <fetch> <origin>',
        'git <reset> <--hard> <origin/main>',
        'python3 <-m> <pip> <install> <-c> <constraints.txt> <.[viewer]>',
        'python3 <-m> <pip> <check>',
        'sudo <-n> </usr/bin/systemctl> <restart> <ircbot.service>',
    ]


@pytest.mark.parametrize(
    'args,environment',
    [
        (['main'], {}),
        ([], {'SSH_ORIGINAL_COMMAND': 'bash'}),
        ([], {'TEST_USER': 'root'}),
    ],
)
def test_deployment_rejects_unexpected_invocations(deployment, args, environment):
    result, log = deployment(*args, **environment)
    assert result.returncode != 0
    assert not log


@pytest.mark.parametrize('command', ['flock', 'git', 'python3'])
def test_deployment_failure_prevents_restart(deployment, command):
    result, log = deployment(TEST_FAIL=command)
    assert result.returncode != 0
    assert 'sudo' not in log


def test_dependency_check_failure_prevents_restart(deployment):
    result, log = deployment(TEST_FAIL_CHECK='yes')
    assert result.returncode != 0
    assert 'python3 <-m> <pip> <check>' in log
    assert 'sudo' not in log
