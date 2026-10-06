import pytest
import yaml

from pmxbot.dictlib import ConfigDict


@pytest.mark.parametrize('key', ['PMXBOT_TEST_VALUE', '123', ''])
def test_environment_scalar(tmp_path, monkeypatch, key):
    monkeypatch.setenv('PMXBOT_TEST_VALUE', 'configured value')
    monkeypatch.delenv('123', raising=False)
    config = tmp_path / 'config.yaml'
    config.write_text(f'value: !env "{key}"\n', encoding='utf-8')
    expected = 'configured value' if key == 'PMXBOT_TEST_VALUE' else ''
    assert ConfigDict.from_yaml(config) == {'value': expected}


@pytest.mark.parametrize(
    ('value', 'node_type'), [('[]', 'sequence'), ('{}', 'mapping')]
)
def test_environment_rejects_non_scalar(tmp_path, value, node_type):
    config = tmp_path / 'config.yaml'
    config.write_text(f'value: !env {value}\n', encoding='utf-8')
    with pytest.raises(
        yaml.constructor.ConstructorError,
        match=f'expected a scalar node, but found {node_type}',
    ) as error:
        ConfigDict.from_yaml(config)
    assert error.value.problem_mark.line == 0
