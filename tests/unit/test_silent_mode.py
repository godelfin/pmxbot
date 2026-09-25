from unittest.mock import Mock

import pytest

import pmxbot
from pmxbot import core
from pmxbot.dictlib import ConfigDict


class RecordingBot(core.Bot):
    _nickname = "bot"

    def __init__(self):
        self.transmitted = []

    def transmit(self, channel, message):
        self.transmitted.append((channel, str(message)))
        return message


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(
        pmxbot,
        "config",
        {
            "silent_mode_disable_command": "secret-off",
            "silent_mode_enable_command": "secret-on",
        },
    )
    monkeypatch.setattr(core.Handler, "_registry", [])
    return RecordingBot()


def test_controls_are_silent_and_private(bot):
    logged = Mock()
    core.ContentHandler().decorate(logged)
    bot.handle_action("#test", "user", "!secret-off")
    assert bot.silent
    bot.handle_action("#test", "user", "!secret-on")
    assert not bot.silent
    assert bot.transmitted == []
    logged.assert_not_called()


def test_processing_logging_and_resume(bot):
    logged = []
    effects = []
    core.ContentHandler().decorate(lambda rest: logged.append(rest))

    @core.command()
    def example():
        effects.append("before")
        yield "first"
        effects.append("after")
        yield "/me second"

    bot.handle_action("#test", "user", "!secret-off")
    bot.handle_action("#test", "user", "!example")
    assert effects == ["before", "after"]
    assert logged == ["!example"]
    assert bot.transmitted == []
    bot.handle_action("#test", "user", "!secret-on")
    bot.handle_action("#test", "user", "!example")
    assert bot.transmitted == [("#test", "first"), ("#test", "/me second")]


@pytest.mark.parametrize("keyword", [None, "", "   "])
def test_empty_disable_defaults_off(bot, keyword):
    pmxbot.config["silent_mode_disable_command"] = keyword
    bot.handle_action("#test", "user", "!disable")
    assert not bot.silent
    bot.out("#test", "hello")
    assert bot.transmitted == [("#test", "hello")]


def test_missing_disable_and_instance_isolation(bot):
    del pmxbot.config["silent_mode_disable_command"]
    assert not bot._handle_silent_control("!disable")
    assert not bot.silent
    bot.silent = True
    assert not RecordingBot().silent


def test_exact_case_sensitive_matching(bot):
    for text in ["!secret-off extra", "!SECRET-OFF", "secret-off"]:
        assert not bot._handle_silent_control(text)
    assert not bot.silent
    assert bot._handle_silent_control(" !secret-off ")


def test_scheduled_and_redirected_output(bot):
    effects = []

    def scheduled():
        yield core.SwitchChannel("#other")
        yield "hidden"
        effects.append("completed")

    bot.silent = True
    bot.handle_scheduled(core.Handler(func=scheduled, channel="#test"))
    assert effects == ["completed"]
    assert not bot.transmitted


def test_errors_still_processed_silently(bot, capsys):
    @core.command()
    def broken():
        raise ValueError("example error")

    bot.silent = True
    bot.handle_action("#test", "user", "!broken")
    assert "example error" in capsys.readouterr().err
    assert not bot.transmitted


def test_environment_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PMXBOT_TEST_SECRET", "private-keyword")
    monkeypatch.delenv("PMXBOT_TEST_MISSING", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        "silent_mode_disable_command: !env PMXBOT_TEST_SECRET\n"
        "silent_mode_enable_command: !env PMXBOT_TEST_MISSING\n"
    )
    assert ConfigDict.from_yaml(config) == {
        "silent_mode_disable_command": "private-keyword",
        "silent_mode_enable_command": "",
    }
