"""Public IRC configuration mutation is no longer supported."""

import importlib_metadata

from pmxbot import core


def test_config_command_is_not_registered():
    extensions = importlib_metadata.entry_points(group='pmxbot_handlers')
    assert all(ep.value != 'pmxbot.config_' for ep in extensions)
    assert not list(core.CommandHandler.find_matching('!config foo=bar', '#test'))
